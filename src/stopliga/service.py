"""Synchronization orchestration."""

from __future__ import annotations

import logging
from threading import Event
from uuid import uuid4

from .errors import (
    AuthenticationError,
    ConfigError,
    PartialUpdateError,
    ReconciliationRequiredError,
    StateError,
    StopLigaError,
    UnsupportedRouteShapeError,
)
from .feed import load_feed_snapshot
from .logging_utils import log_context, log_event
from .models import Config, FeedSnapshot, StateSnapshot, SyncResult
from .notifier import NotificationState, notification_targets, send_notifications, send_startup_notification
from .routers.factory import create_router_driver
from .routers.base import RouterDriver
from .state import StateStore, utcnow_iso
from .schedule import ActiveSchedule, inactive_snapshot


FATAL_LOOP_ERRORS = (
    AuthenticationError,
    ConfigError,
    ReconciliationRequiredError,
    StateError,
    UnsupportedRouteShapeError,
)


class StopLigaService:
    """End-to-end synchronization service."""

    def __init__(self, config: Config):
        self.config = config
        self.schedule = ActiveSchedule(config)
        self._driver: RouterDriver | None = None
        self._before_write = lambda: None
        self.logger = logging.getLogger("stopliga.service")
        self.state_store = StateStore(config.state_file)
        self.bootstrap_guard_store = StateStore(config.bootstrap_guard_file)

    def _load_runtime_state(self) -> dict[str, object]:
        try:
            return self.state_store.load()
        except ConfigError as exc:
            if self.config.dry_run:
                log_event(self.logger, logging.WARNING, "invalid_state_preserved_in_dry_run", error=exc)
                return {}
            try:
                bad_path = self.state_store.quarantine_invalid_file()
            except StateError as quarantine_exc:
                log_event(
                    self.logger, logging.WARNING, "state_quarantine_failed", error=quarantine_exc, original_error=exc
                )
            else:
                log_event(
                    self.logger,
                    logging.WARNING,
                    "state_file_quarantined",
                    path=self.config.state_file,
                    quarantined_to=bad_path,
                    error=exc,
                )
            return {}
        except StateError as exc:
            log_event(self.logger, logging.WARNING, "state_load_failed", error=exc)
            return {}

    def _load_bootstrap_guard(self, legacy_state: dict[str, object] | None = None) -> dict[str, object]:
        try:
            guard = self.bootstrap_guard_store.load()
        except ConfigError as exc:
            if self.config.dry_run:
                log_event(self.logger, logging.WARNING, "invalid_state_preserved_in_dry_run", error=exc)
                return {}
            try:
                bad_path = self.bootstrap_guard_store.quarantine_invalid_file()
            except StateError as quarantine_exc:
                log_event(
                    self.logger,
                    logging.WARNING,
                    "bootstrap_guard_quarantine_failed",
                    error=quarantine_exc,
                    original_error=exc,
                )
            else:
                log_event(
                    self.logger,
                    logging.WARNING,
                    "bootstrap_guard_quarantined",
                    path=self.config.bootstrap_guard_file,
                    quarantined_to=bad_path,
                    error=exc,
                )
            return {}
        except StateError as exc:
            log_event(self.logger, logging.WARNING, "bootstrap_guard_load_failed", error=exc)
            return {}
        if guard:
            return guard
        legacy = legacy_state if legacy_state is not None else self._load_runtime_state()
        if any(legacy.get(key) for key in ("bootstrap_source", "bootstrap_network_id", "bootstrap_target_macs")):
            return legacy
        return {}

    def _write_bootstrap_guard(self, result: SyncResult) -> None:
        self._write_bootstrap_guard_values(
            bootstrap_source=result.bootstrap_source,
            bootstrap_network_id=result.bootstrap_network_id,
            bootstrap_target_macs=result.bootstrap_target_macs,
        )

    def _clear_bootstrap_guard(self) -> None:
        self._write_bootstrap_guard_values(
            bootstrap_source=None,
            bootstrap_network_id=None,
            bootstrap_target_macs=(),
        )

    def _write_bootstrap_guard_triplet(
        self,
        bootstrap_source: str | None,
        bootstrap_network_id: str | None,
        bootstrap_target_macs: tuple[str, ...],
    ) -> None:
        self._write_bootstrap_guard_values(
            bootstrap_source=bootstrap_source,
            bootstrap_network_id=bootstrap_network_id,
            bootstrap_target_macs=bootstrap_target_macs,
        )

    def _write_bootstrap_guard_values(
        self,
        *,
        bootstrap_source: str | None,
        bootstrap_network_id: str | None,
        bootstrap_target_macs: tuple[str, ...],
    ) -> None:
        if self.config.dry_run:
            return
        desired = (bootstrap_source, bootstrap_network_id, bootstrap_target_macs)
        previous = self.bootstrap_guard_store.load()
        current = (
            previous.get("bootstrap_source"),
            previous.get("bootstrap_network_id"),
            self._string_tuple(previous, "bootstrap_target_macs"),
        )
        if desired == current:
            return
        self.bootstrap_guard_store.write_payload(
            {
                "route_name": self.config.route_name,
                "site": self.config.site,
                "bootstrap_source": bootstrap_source,
                "bootstrap_network_id": bootstrap_network_id,
                "bootstrap_target_macs": list(bootstrap_target_macs),
            }
        )

    def _write_state(
        self,
        *,
        status: str,
        result: SyncResult | None = None,
        error: str | None = None,
        partial_failure: bool = False,
        error_stage: str | None = None,
        rollback_attempted: bool = False,
        rollback_completed: bool = False,
        rollback_error: str | None = None,
        sync_id: str | None = None,
        reconciliation_required: bool = False,
        previous_state: dict[str, object] | None = None,
        notification_state: dict[str, object] | None = None,
        preserve_block_status: bool = False,
    ) -> None:
        if self.config.dry_run:
            return
        now = utcnow_iso()
        previous = previous_state if previous_state is not None else self._load_runtime_state()
        previous_failures = previous.get("consecutive_failures", 0)
        if not isinstance(previous_failures, int):
            previous_failures = 0
        snapshot = StateSnapshot(
            status=status,
            run_mode=self.config.run_mode,
            route_name=self.config.route_name,
            site=self.config.site,
            last_attempt_at=now,
            last_success_at=now
            if status in {"success", "dry_run"}
            else self._optional_str(previous, "last_success_at"),
            last_error=error,
            last_mode=result.mode if result else self._optional_str(previous, "last_mode"),
            last_sync_id=sync_id or self._optional_str(previous, "last_sync_id"),
            last_route_id=result.route_id if result else self._optional_str(previous, "last_route_id"),
            last_backend=result.backend_name if result else self._optional_str(previous, "last_backend"),
            feed_hash=result.feed_hash if result else self._optional_str(previous, "feed_hash"),
            destinations_hash=result.destinations_hash if result else self._optional_str(previous, "destinations_hash"),
            changed=result.changed if result else False,
            created=result.created if result else False,
            dry_run=result.dry_run if result else False,
            consecutive_failures=0 if status in {"success", "dry_run"} else previous_failures + 1,
            partial_failure=partial_failure,
            last_error_stage=error_stage,
            rollback_attempted=rollback_attempted,
            rollback_completed=rollback_completed,
            rollback_error=rollback_error,
            reconciliation_required=reconciliation_required,
            last_is_blocked=(
                result.is_blocked
                if result and not preserve_block_status
                else self._optional_bool(previous, "last_is_blocked")
            ),
            notification_pending=self._notification_map(notification_state, previous, "notification_pending"),
            last_notified_blocked=self._notification_map(notification_state, previous, "last_notified_blocked"),
            last_gotify_message_id=self._notification_int(notification_state, previous, "last_gotify_message_id"),
            last_ntfy_message_id=self._notification_str(notification_state, previous, "last_ntfy_message_id"),
            last_telegram_message_id=self._notification_int(notification_state, previous, "last_telegram_message_id"),
            last_telegram_chat_id=self._notification_str(notification_state, previous, "last_telegram_chat_id"),
            bootstrap_source=result.bootstrap_source if result else self._optional_str(previous, "bootstrap_source"),
            bootstrap_network_id=result.bootstrap_network_id
            if result
            else self._optional_str(previous, "bootstrap_network_id"),
            bootstrap_target_macs=result.bootstrap_target_macs
            if result
            else self._string_tuple(previous, "bootstrap_target_macs"),
        )
        self.state_store.write(snapshot)

    @staticmethod
    def _optional_str(payload: dict[str, object], key: str) -> str | None:
        value = payload.get(key)
        return value if isinstance(value, str) else None

    @staticmethod
    def _optional_bool(payload: dict[str, object], key: str) -> bool | None:
        value = payload.get(key)
        return value if isinstance(value, bool) else None

    @staticmethod
    def _optional_int(payload: dict[str, object], key: str) -> int | None:
        value = payload.get(key)
        if isinstance(value, bool):
            return None
        return value if isinstance(value, int) else None

    @staticmethod
    def _notification_map(current: dict[str, object] | None, previous: dict[str, object], key: str) -> dict[str, bool]:
        value = (current or {}).get(key, previous.get(key, {}))
        if not isinstance(value, dict):
            return {}
        return {k: v for k, v in value.items() if isinstance(k, str) and isinstance(v, bool)}

    @classmethod
    def _notification_int(
        cls,
        notification_state: dict[str, object] | None,
        previous: dict[str, object],
        key: str,
    ) -> int | None:
        if notification_state is not None and key in notification_state:
            return cls._optional_int(notification_state, key)
        return cls._optional_int(previous, key)

    @classmethod
    def _notification_str(
        cls,
        notification_state: dict[str, object] | None,
        previous: dict[str, object],
        key: str,
    ) -> str | None:
        if notification_state is not None and key in notification_state:
            return cls._optional_str(notification_state, key)
        return cls._optional_str(previous, key)

    @staticmethod
    def _string_tuple(payload: dict[str, object], key: str) -> tuple[str, ...]:
        value = payload.get(key)
        if not isinstance(value, list):
            return ()
        items: list[str] = []
        for item in value:
            if isinstance(item, str) and item.strip():
                items.append(item.strip())
        return tuple(items)

    def _run_router_sync(self, feed_snapshot: FeedSnapshot, previous_guard: dict[str, object]) -> SyncResult:
        if self._driver is None:
            self._driver = create_router_driver(self.config)
        return self._driver.sync(
            feed_snapshot,
            previous_guard,
            guard_writer=self._write_bootstrap_guard_triplet,
            guard_clearer=self._clear_bootstrap_guard,
            before_write=self._before_write,
        )

    @staticmethod
    def _requires_reconciliation(exc: StopLigaError, *, reconciliation_pending: bool) -> bool:
        return reconciliation_pending or (
            isinstance(exc, PartialUpdateError)
            and bool(exc.completed_stages)
            and (not exc.rollback_attempted or not exc.rollback_completed)
        )

    def run_once(self) -> SyncResult:
        sync_id = uuid4().hex[:12]
        with log_context(sync_id=sync_id):
            log_event(
                self.logger,
                logging.INFO,
                "sync_start",
                route=self.config.route_name,
                site=self.config.site,
                mode="local",
                dry_run=self.config.dry_run,
            )
            reconciliation_pending = False
            previous_runtime_state: dict[str, object] = {}
            mutation_started = False

            def journal_before_write() -> None:
                nonlocal mutation_started
                if mutation_started or self.config.dry_run:
                    return
                self._write_state(
                    status="applying",
                    partial_failure=True,
                    error_stage="remote-write",
                    reconciliation_required=True,
                    sync_id=sync_id,
                    previous_state=previous_runtime_state,
                )
                mutation_started = True

            self._before_write = journal_before_write
            try:
                previous_runtime_state = self._load_runtime_state()
                reconciliation_pending = bool(previous_runtime_state.get("reconciliation_required"))
                if (
                    reconciliation_pending
                    and not previous_runtime_state.get("partial_failure")
                    and previous_runtime_state.get("last_error_stage") is None
                ):
                    log_event(
                        self.logger,
                        logging.WARNING,
                        "reconciliation_state_ignored",
                        reason="no_partial_failure_details",
                    )
                    reconciliation_pending = False
                if reconciliation_pending:
                    log_event(
                        self.logger,
                        logging.WARNING,
                        "reconciliation_pending",
                        last_error_stage=previous_runtime_state.get("last_error_stage"),
                        rollback_completed=previous_runtime_state.get("rollback_completed"),
                    )
                    raise ReconciliationRequiredError(
                        "Previous sync left the remote state requiring reconciliation; refusing new writes"
                    )
                schedule_active = self.schedule.is_active()
                if self.schedule.configured:
                    log_event(
                        self.logger,
                        logging.INFO,
                        "schedule_check",
                        active=schedule_active,
                        timezone=self.config.active_timezone,
                    )
                try:
                    feed_snapshot = load_feed_snapshot(self.config) if schedule_active else inactive_snapshot()
                except StopLigaError:
                    if not schedule_active or self.schedule.is_active():
                        raise
                    schedule_active = False
                    feed_snapshot = inactive_snapshot()
                # A fetch can cross the closing boundary. Never enable after it closes.
                if schedule_active and not self.schedule.is_active():
                    schedule_active = False
                    feed_snapshot = inactive_snapshot()
                previous_guard = self._load_bootstrap_guard(previous_runtime_state)
                result = self._run_router_sync(feed_snapshot, previous_guard)
                # Discovery or a slow write can also cross the closing boundary.
                if schedule_active and not self.schedule.is_active():
                    schedule_active = False
                    result = self._run_router_sync(inactive_snapshot(), self._load_bootstrap_guard())
                pending = self._notification_map(None, previous_runtime_state, "notification_pending")
                acknowledged = self._notification_map(None, previous_runtime_state, "last_notified_blocked")
                if schedule_active and not result.dry_run:
                    pending = {
                        name: result.is_blocked
                        for name in notification_targets(self.config, result, previous_runtime_state)
                    }
                initial_notifications: dict[str, object] = {
                    "notification_pending": pending,
                    "last_notified_blocked": acknowledged,
                }
                self._write_state(
                    notification_state=initial_notifications,
                    status="dry_run" if result.dry_run else "success",
                    result=result,
                    preserve_block_status=not schedule_active,
                    sync_id=sync_id,
                    previous_state=previous_runtime_state,
                )
                self._write_bootstrap_guard(result)
                try:
                    notification_result = (
                        send_notifications(self.config, result, previous_runtime_state)
                        if schedule_active
                        else NotificationState()
                    )
                    if notification_result.has_values:
                        for name in notification_result.delivered_providers:
                            acknowledged[name] = result.is_blocked
                            pending.pop(name, None)
                        notification_state_payload: dict[str, object] = {
                            "notification_pending": pending,
                            "last_notified_blocked": acknowledged,
                        }
                        if notification_result.failures:
                            log_event(
                                self.logger,
                                logging.WARNING,
                                "notification_failed",
                                providers=",".join(notification_result.failures),
                            )
                        if notification_result.gotify_message_id is not None:
                            notification_state_payload["last_gotify_message_id"] = notification_result.gotify_message_id
                        if notification_result.ntfy_message_id is not None:
                            notification_state_payload["last_ntfy_message_id"] = notification_result.ntfy_message_id
                        if notification_result.telegram_message_id is not None:
                            notification_state_payload["last_telegram_message_id"] = (
                                notification_result.telegram_message_id
                            )
                        if notification_result.telegram_chat_id is not None:
                            notification_state_payload["last_telegram_chat_id"] = notification_result.telegram_chat_id
                        self._write_state(
                            status="dry_run" if result.dry_run else "success",
                            result=result,
                            preserve_block_status=not schedule_active,
                            sync_id=sync_id,
                            previous_state=previous_runtime_state,
                            notification_state=notification_state_payload,
                        )
                except StopLigaError as exc:
                    log_event(self.logger, logging.WARNING, "notification_failed", error=exc)
                log_event(
                    self.logger,
                    logging.INFO,
                    "sync_finish",
                    mode=result.mode,
                    changed=result.changed,
                    created=result.created,
                    route=result.route_name,
                    enabled=result.desired_enabled,
                    destinations=result.desired_destinations,
                    added_destinations=result.added_destinations,
                    removed_destinations=result.removed_destinations,
                )
                return result
            except StopLigaError as exc:
                if isinstance(exc, ReconciliationRequiredError):
                    raise
                try:
                    self._write_state(
                        status="error",
                        error=str(exc),
                        partial_failure=isinstance(exc, PartialUpdateError) or mutation_started,
                        error_stage=exc.failed_stage
                        if isinstance(exc, PartialUpdateError)
                        else ("remote-write" if mutation_started else None),
                        rollback_attempted=exc.rollback_attempted if isinstance(exc, PartialUpdateError) else False,
                        rollback_completed=exc.rollback_completed if isinstance(exc, PartialUpdateError) else False,
                        rollback_error=exc.rollback_error if isinstance(exc, PartialUpdateError) else None,
                        reconciliation_required=self._requires_reconciliation(
                            exc, reconciliation_pending=reconciliation_pending
                        )
                        or (mutation_started and not (isinstance(exc, PartialUpdateError) and exc.rollback_completed)),
                        sync_id=sync_id,
                        previous_state=previous_runtime_state,
                    )
                except StateError as state_exc:
                    log_event(self.logger, logging.ERROR, "state_write_failed", error=state_exc, original_error=exc)
                raise

    def run_loop(self, stop_event: Event) -> int:
        log_event(self.logger, logging.INFO, "loop_start", interval_seconds=self.config.interval_seconds)
        try:
            send_startup_notification(self.config)
        except StopLigaError as exc:
            log_event(self.logger, logging.WARNING, "notification_failed", error=exc)
        while not stop_event.is_set():
            try:
                self.run_once()
            except FATAL_LOOP_ERRORS:
                raise
            except StopLigaError as exc:
                log_event(self.logger, logging.ERROR, "loop_iteration_failed", error=exc)
            if stop_event.wait(self.schedule.wait_seconds(self.config.interval_seconds)):
                break
        log_event(self.logger, logging.INFO, "loop_stop")
        return 0
