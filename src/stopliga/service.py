"""Synchronization orchestration."""

from __future__ import annotations

import logging
from uuid import uuid4
from threading import Event

from .errors import AuthenticationError, ConfigError, DiscoveryError, PartialUpdateError, ReconciliationRequiredError, RouteNotFoundError, StateError, StopLigaError, UnsupportedRouteShapeError
from .feed import load_feed_snapshot
from .logging_utils import log_context, log_event
from .models import BootstrapPreview, Config, FeedSnapshot, StateSnapshot, SyncResult
from .notifier import send_notifications
from .state import StateStore, utcnow_iso
from .opnsense import sync_opnsense
from .unifi import (
    ALL_CLIENTS_TARGET,
    BaseRouteBackend,
    UniFiClient,
    apply_plan,
    build_direct_bootstrap_payload,
    choose_create_backend,
    choose_existing_route_backend,
    log_unsupported_shape,
    summarize_plan,
)


FATAL_LOOP_ERRORS = (AuthenticationError, ConfigError, ReconciliationRequiredError, StateError, UnsupportedRouteShapeError)
VPN_CLIENT_NETWORK_REQUIRED_URL = "https://github.com/jcastro/stopliga/blob/main/README.md#vpn-client-network-required"


class StopLigaService:
    """End-to-end synchronization service."""

    def __init__(self, config: Config):
        self.config = config
        self.logger = logging.getLogger("stopliga.service")
        self.state_store = StateStore(config.state_file)
        self.bootstrap_guard_store = StateStore(config.bootstrap_guard_file)

    def _load_runtime_state(self) -> dict[str, object]:
        try:
            return self.state_store.load()
        except ConfigError as exc:
            try:
                bad_path = self.state_store.quarantine_invalid_file()
            except StateError as quarantine_exc:
                log_event(self.logger, logging.WARNING, "state_quarantine_failed", error=quarantine_exc, original_error=exc)
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

    def _load_bootstrap_guard(self) -> dict[str, object]:
        try:
            guard = self.bootstrap_guard_store.load()
        except ConfigError as exc:
            try:
                bad_path = self.bootstrap_guard_store.quarantine_invalid_file()
            except StateError as quarantine_exc:
                log_event(self.logger, logging.WARNING, "bootstrap_guard_quarantine_failed", error=quarantine_exc, original_error=exc)
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
        legacy = self._load_runtime_state()
        if any(
            legacy.get(key)
            for key in ("bootstrap_source", "bootstrap_network_id", "bootstrap_target_macs")
        ):
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

    def _write_bootstrap_guard_values(
        self,
        *,
        bootstrap_source: str | None,
        bootstrap_network_id: str | None,
        bootstrap_target_macs: tuple[str, ...],
    ) -> None:
        snapshot = StateSnapshot(
            status="guard",
            run_mode=self.config.run_mode,
            route_name=self.config.route_name,
            site=self.config.site,
            last_attempt_at=utcnow_iso(),
            last_success_at=None,
            last_error=None,
            last_mode=None,
            last_sync_id=None,
            last_route_id=None,
            last_backend=None,
            feed_hash=None,
            destinations_hash=None,
            changed=False,
            created=False,
            dry_run=False,
            last_is_blocked=None,
            bootstrap_source=bootstrap_source,
            bootstrap_network_id=bootstrap_network_id,
            bootstrap_target_macs=bootstrap_target_macs,
        )
        self.bootstrap_guard_store.write(snapshot)

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
    ) -> None:
        now = utcnow_iso()
        previous = self._load_runtime_state()
        previous_failures = previous.get("consecutive_failures", 0)
        if not isinstance(previous_failures, int):
            previous_failures = 0
        snapshot = StateSnapshot(
            status=status,
            run_mode=self.config.run_mode,
            route_name=self.config.route_name,
            site=self.config.site,
            last_attempt_at=now,
            last_success_at=now if status in {"success", "dry_run"} else self._optional_str(previous, "last_success_at"),
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
            last_is_blocked=result.is_blocked if result else self._optional_bool(previous, "last_is_blocked"),
            bootstrap_source=result.bootstrap_source if result else self._optional_str(previous, "bootstrap_source"),
            bootstrap_network_id=result.bootstrap_network_id if result else self._optional_str(previous, "bootstrap_network_id"),
            bootstrap_target_macs=result.bootstrap_target_macs if result else self._string_tuple(previous, "bootstrap_target_macs"),
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
    def _string_tuple(payload: dict[str, object], key: str) -> tuple[str, ...]:
        value = payload.get(key)
        if not isinstance(value, list):
            return ()
        items: list[str] = []
        for item in value:
            if isinstance(item, str) and item.strip():
                items.append(item.strip())
        return tuple(items)

    def _bootstrap_requires_manual_review(self, source: str | None) -> bool:
        return source == "auto-bootstrap-device-fallback"

    def _route_target_macs(self, route_record: dict[str, object]) -> tuple[str, ...]:
        target_devices = route_record.get("target_devices")
        if not isinstance(target_devices, list):
            return ()
        if any(isinstance(item, dict) and item.get("type") == "ALL_CLIENTS" for item in target_devices):
            return ("__all_clients__",)
        macs: list[str] = []
        for item in target_devices:
            if isinstance(item, dict):
                client_mac = item.get("client_mac")
                if isinstance(client_mac, str) and client_mac.strip():
                    macs.append(client_mac.strip().lower())
        return tuple(sorted(set(macs)))

    def _is_pending_auto_bootstrap(self, route_record: dict[str, object], state: dict[str, object]) -> bool:
        if not self._bootstrap_requires_manual_review(str(state.get("bootstrap_source") or "")):
            return False
        route_network_id = route_record.get("network_id")
        if not isinstance(route_network_id, str) or route_network_id.strip() != str(state.get("bootstrap_network_id", "")).strip():
            return False
        saved_macs = tuple(sorted(item.lower() for item in self._string_tuple(state, "bootstrap_target_macs")))
        return saved_macs == self._route_target_macs(route_record)

    def _build_result(
        self,
        *,
        plan,
        feed_snapshot: FeedSnapshot,
        created: bool,
        bootstrap_source: str | None,
        bootstrap_network_id: str | None,
        bootstrap_target_macs: tuple[str, ...],
    ) -> SyncResult:
        added_destinations = len([ip for ip in plan.desired_destinations if ip not in plan.current_destinations])
        removed_destinations = len([ip for ip in plan.current_destinations if ip not in plan.desired_destinations])
        return SyncResult(
            mode="local",
            route_name=self.config.route_name,
            route_id=plan.route_id,
            backend_name=plan.backend_name,
            changed=created or plan.has_changes,
            created=created,
            dry_run=self.config.dry_run,
            desired_enabled=plan.desired_enabled,
            current_enabled=plan.current_enabled,
            desired_destinations=len(plan.desired_destinations),
            current_destinations=len(plan.current_destinations),
            invalid_entries=feed_snapshot.invalid_count,
            feed_hash=feed_snapshot.feed_hash,
            destinations_hash=feed_snapshot.destinations_hash,
            summary=summarize_plan(plan, feed_snapshot),
            is_blocked=feed_snapshot.is_blocked,
            added_destinations=added_destinations,
            removed_destinations=removed_destinations,
            bootstrap_source=bootstrap_source,
            bootstrap_network_id=bootstrap_network_id,
            bootstrap_target_macs=bootstrap_target_macs,
        )

    def _log_plan_details(self, *, plan, pending_manual_review: bool) -> None:
        added = [ip for ip in plan.desired_destinations if ip not in plan.current_destinations]
        removed = [ip for ip in plan.current_destinations if ip not in plan.desired_destinations]

        log_event(
            self.logger,
            logging.INFO,
            "route_check",
            backend=plan.backend_name,
            route=plan.route_label,
            route_id=plan.route_id,
            pending_manual_review=pending_manual_review,
            current_enabled=plan.current_enabled,
            desired_enabled=plan.desired_enabled,
            current_destinations=len(plan.current_destinations),
            desired_destinations=len(plan.desired_destinations),
            route_changed_fields=",".join(plan.route_changed_fields) if plan.route_changed_fields else "",
            linked_list_changed_fields=",".join(plan.linked_list_changed_fields) if plan.linked_list_changed_fields else "",
        )

        if added or removed:
            log_event(
                self.logger,
                logging.INFO,
                "route_ip_delta",
                route=plan.route_label,
                route_id=plan.route_id,
                added_count=len(added),
                removed_count=len(removed),
                added_sample=",".join(added[:5]),
                removed_sample=",".join(removed[:5]),
            )

    def _plan_route_update(
        self,
        *,
        backend: BaseRouteBackend,
        endpoint: str,
        route_record: dict[str, object],
        feed_snapshot: FeedSnapshot,
        previous_state: dict[str, object],
        created: bool,
        bootstrap_source: str | None,
        bootstrap_network_id: str | None,
        bootstrap_target_macs: tuple[str, ...],
        client: UniFiClient,
    ) -> SyncResult:
        desired_enabled = feed_snapshot.desired_enabled
        pending_manual_review = self._bootstrap_requires_manual_review(bootstrap_source) or self._is_pending_auto_bootstrap(route_record, previous_state)
        if pending_manual_review:
            if desired_enabled:
                log_event(
                    self.logger,
                    logging.WARNING,
                    "route_incomplete",
                    route=self.config.route_name,
                    reason="auto_bootstrap_pending_manual_review",
                )
            desired_enabled = False
            if not bootstrap_source:
                bootstrap_source = "auto-bootstrap"
                bootstrap_network_id = str(route_record.get("network_id") or "") or None
                bootstrap_target_macs = self._route_target_macs(route_record)

        try:
            plan = backend.build_plan(endpoint, route_record, feed_snapshot.destinations, desired_enabled)
        except UnsupportedRouteShapeError:
            if self.config.dump_payloads_on_error:
                log_unsupported_shape(self.logger, route_record)
            raise

        result = self._build_result(
            plan=plan,
            feed_snapshot=feed_snapshot,
            created=created,
            bootstrap_source=bootstrap_source,
            bootstrap_network_id=bootstrap_network_id,
            bootstrap_target_macs=bootstrap_target_macs,
        )
        self._log_plan_details(plan=plan, pending_manual_review=pending_manual_review)
        log_event(
            self.logger,
            logging.INFO,
            "route_plan",
            route=plan.route_label,
            changed=plan.has_changes,
            dry_run=self.config.dry_run,
            desired_enabled=plan.desired_enabled,
            fields_changed=",".join(plan.route_changed_fields + plan.linked_list_changed_fields),
        )
        if not self.config.dry_run and plan.has_changes:
            apply_plan(client, backend, plan)
        return result

    def _bootstrap_route(
        self,
        client: UniFiClient,
        desired_ips: list[str],
    ) -> tuple[BaseRouteBackend, BootstrapPreview]:
        create_backend = choose_create_backend(client, client.resolve_site_context())
        if self.config.vpn_name and self.config.target_clients:
            vpn_network = client.resolve_vpn_network(self.config.vpn_name)
            target_devices = client.resolve_target_devices(self.config.target_clients)
            payload = build_direct_bootstrap_payload(
                route_name_value=self.config.route_name,
                desired_ips=desired_ips,
                desired_enabled=False,
                vpn_network_id=str(vpn_network.get("_id")),
                target_devices=target_devices,
            )
            source = f"vpn:{self.config.vpn_name}"
        else:
            try:
                vpn_network = client.pick_default_vpn_network()
            except DiscoveryError as exc:
                message = (
                    "No UniFi VPN client network was found. Create at least one UniFi VPN Client network "
                    f"and start StopLiga again. See {VPN_CLIENT_NETWORK_REQUIRED_URL}"
                )
                log_event(
                    self.logger,
                    logging.ERROR,
                    "vpn_client_network_missing",
                    docs_url=VPN_CLIENT_NETWORK_REQUIRED_URL,
                )
                raise DiscoveryError(message) from exc
            payload = build_direct_bootstrap_payload(
                route_name_value=self.config.route_name,
                desired_ips=desired_ips,
                desired_enabled=False,
                vpn_network_id=str(vpn_network.get("_id")),
                target_devices=[ALL_CLIENTS_TARGET],
            )
            source = "auto-bootstrap"
        return (
            create_backend,
            BootstrapPreview(
                backend_name=create_backend.backend_name,
                payload=payload,
                source=source,
            ),
        )

    def _run_once(self, feed_snapshot: FeedSnapshot) -> SyncResult:
        if self.config.firewall_backend == "opnsense":
            return sync_opnsense(self.config, feed_snapshot)

        client = UniFiClient(self.config)
        client.authenticate()
        site_context = client.resolve_site_context()
        created = False
        previous_guard = self._load_bootstrap_guard()
        bootstrap_source: str | None = None
        bootstrap_network_id: str | None = None
        bootstrap_target_macs: tuple[str, ...] = ()

        try:
            backend, endpoint, route_record = choose_existing_route_backend(client, site_context, self.config.route_name)
        except RouteNotFoundError:
            bootstrap_backend, preview = self._bootstrap_route(
                client,
                feed_snapshot.destinations,
            )
            log_event(
                self.logger,
                logging.INFO,
                "route_bootstrap_prepared",
                backend=preview.backend_name,
                source=preview.source,
                dry_run=self.config.dry_run,
            )
            if self.config.dry_run:
                preview_enabled = preview.payload.get("enabled") if isinstance(preview.payload.get("enabled"), bool) else None
                return SyncResult(
                    mode="local",
                    route_name=self.config.route_name,
                    route_id=None,
                    backend_name=preview.backend_name,
                    changed=True,
                    created=True,
                    dry_run=True,
                    desired_enabled=bool(preview_enabled),
                    current_enabled=None,
                    desired_destinations=len(feed_snapshot.destinations),
                    current_destinations=0,
                    invalid_entries=feed_snapshot.invalid_count,
                    feed_hash=feed_snapshot.feed_hash,
                    destinations_hash=feed_snapshot.destinations_hash,
                    summary=f"dry-run bootstrap via {preview.source}",
                    bootstrap_source=preview.source,
                    bootstrap_network_id=str(preview.payload.get("network_id") or "") or None,
                    bootstrap_target_macs=self._route_target_macs(preview.payload),
                )
            applied_preview = preview
            if preview.source.startswith("auto-bootstrap"):
                self._write_bootstrap_guard_values(
                    bootstrap_source=preview.source,
                    bootstrap_network_id=str(preview.payload.get("network_id") or "") or None,
                    bootstrap_target_macs=self._route_target_macs(preview.payload),
                )
            try:
                endpoint, route_record = bootstrap_backend.create_route(preview.payload)
            except StopLigaError as exc:
                if preview.source == "auto-bootstrap":
                    target_device = client.pick_default_target_device()
                    fallback_payload = dict(preview.payload)
                    fallback_payload["target_devices"] = [target_device]
                    log_event(
                        self.logger,
                        logging.WARNING,
                        "route_bootstrap_retry",
                        backend=preview.backend_name,
                        source="auto-bootstrap-device-fallback",
                        reason="all_clients_target_rejected",
                    )
                    try:
                        endpoint, route_record = bootstrap_backend.create_route(fallback_payload)
                    except StopLigaError as fallback_exc:
                        self._clear_bootstrap_guard()
                        raise RouteNotFoundError(
                            f"Route {self.config.route_name!r} not found and bootstrap failed. "
                            f"Primary error: {exc}. Fallback error: {fallback_exc}"
                        ) from fallback_exc
                    applied_preview = BootstrapPreview(
                        backend_name=preview.backend_name,
                        payload=fallback_payload,
                        source="auto-bootstrap-device-fallback",
                    )
                    self._write_bootstrap_guard_values(
                        bootstrap_source=applied_preview.source,
                        bootstrap_network_id=str(applied_preview.payload.get("network_id") or "") or None,
                        bootstrap_target_macs=self._route_target_macs(applied_preview.payload),
                    )
                else:
                    self._clear_bootstrap_guard()
                    raise RouteNotFoundError(
                        f"Route {self.config.route_name!r} not found and bootstrap via {preview.source} failed: {exc}"
                    ) from exc
            created = True
            bootstrap_source = applied_preview.source
            bootstrap_network_id = str(applied_preview.payload.get("network_id") or "") or None
            bootstrap_target_macs = self._route_target_macs(applied_preview.payload)
            backend = bootstrap_backend

        return self._plan_route_update(
            backend=backend,
            endpoint=endpoint,
            route_record=route_record,
            feed_snapshot=feed_snapshot,
            previous_state=previous_guard,
            created=created,
            bootstrap_source=bootstrap_source,
            bootstrap_network_id=bootstrap_network_id,
            bootstrap_target_macs=bootstrap_target_macs,
            client=client,
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
                mode=self.config.firewall_backend,
                dry_run=self.config.dry_run,
            )
            reconciliation_pending = False
            try:
                previous_runtime_state = self._load_runtime_state()
                reconciliation_pending = bool(previous_runtime_state.get("reconciliation_required"))
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
                feed_snapshot = load_feed_snapshot(self.config)
                result = self._run_once(feed_snapshot)
                self._write_state(status="dry_run" if result.dry_run else "success", result=result, sync_id=sync_id)
                self._write_bootstrap_guard(result)
                try:
                    send_notifications(self.config, result, previous_runtime_state)
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
                try:
                    self._write_state(
                        status="error",
                        error=str(exc),
                        partial_failure=isinstance(exc, PartialUpdateError),
                        error_stage=exc.failed_stage if isinstance(exc, PartialUpdateError) else None,
                        rollback_attempted=exc.rollback_attempted if isinstance(exc, PartialUpdateError) else False,
                        rollback_completed=exc.rollback_completed if isinstance(exc, PartialUpdateError) else False,
                        rollback_error=exc.rollback_error if isinstance(exc, PartialUpdateError) else None,
                        reconciliation_required=(
                            reconciliation_pending
                            or
                            isinstance(exc, PartialUpdateError)
                            and (not exc.rollback_attempted or not exc.rollback_completed)
                        ),
                        sync_id=sync_id,
                    )
                except StateError as state_exc:
                    log_event(self.logger, logging.ERROR, "state_write_failed", error=state_exc, original_error=exc)
                raise

    def run_loop(self, stop_event: Event) -> int:
        log_event(self.logger, logging.INFO, "loop_start", interval_seconds=self.config.interval_seconds)
        while not stop_event.is_set():
            try:
                self.run_once()
            except FATAL_LOOP_ERRORS:
                raise
            except StopLigaError as exc:
                try:
                    self._write_state(
                        status="error",
                        error=str(exc),
                        partial_failure=isinstance(exc, PartialUpdateError),
                        error_stage=exc.failed_stage if isinstance(exc, PartialUpdateError) else None,
                        rollback_attempted=exc.rollback_attempted if isinstance(exc, PartialUpdateError) else False,
                        rollback_completed=exc.rollback_completed if isinstance(exc, PartialUpdateError) else False,
                        rollback_error=exc.rollback_error if isinstance(exc, PartialUpdateError) else None,
                        reconciliation_required=(
                            isinstance(exc, PartialUpdateError)
                            and (not exc.rollback_attempted or not exc.rollback_completed)
                        ),
                        sync_id=None,
                    )
                except StateError as state_exc:
                    log_event(self.logger, logging.ERROR, "state_write_failed", error=state_exc, original_error=exc)
                log_event(self.logger, logging.ERROR, "loop_iteration_failed", error=exc)
            if stop_event.wait(self.config.interval_seconds):
                break
        log_event(self.logger, logging.INFO, "loop_stop")
        return 0
