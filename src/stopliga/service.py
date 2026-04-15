"""Synchronization orchestration."""

from __future__ import annotations

import logging
from threading import Event

from .errors import AuthenticationError, ConfigError, PartialUpdateError, RouteNotFoundError, StateError, StopLigaError, UnsupportedRouteShapeError
from .feed import load_feed_snapshot
from .logging_utils import log_event
from .models import BootstrapPreview, Config, FeedSnapshot, StateSnapshot, SyncResult
from .state import StateStore, utcnow_iso
from .unifi import (
    ALL_CLIENTS_TARGET,
    UniFiClient,
    apply_plan,
    build_direct_bootstrap_payload,
    choose_create_backend,
    choose_existing_route_backend,
    log_unsupported_shape,
    summarize_plan,
)


FATAL_LOOP_ERRORS = (AuthenticationError, ConfigError, StateError, UnsupportedRouteShapeError)


class StopLigaService:
    """End-to-end synchronization service."""

    def __init__(self, config: Config):
        self.config = config
        self.logger = logging.getLogger("stopliga.service")
        self.state_store = StateStore(config.state_file)

    def _write_state(
        self,
        *,
        status: str,
        result: SyncResult | None = None,
        error: str | None = None,
        partial_failure: bool = False,
        error_stage: str | None = None,
    ) -> None:
        now = utcnow_iso()
        try:
            previous = self.state_store.load()
        except (ConfigError, StateError) as exc:
            previous = {}
            log_event(self.logger, logging.WARNING, "state_load_failed", error=exc)
        snapshot = StateSnapshot(
            status=status,
            run_mode=self.config.run_mode,
            route_name=self.config.route_name,
            site=self.config.site,
            last_attempt_at=now,
            last_success_at=now if status in {"success", "dry_run"} else previous.get("last_success_at"),
            last_error=error,
            last_mode=result.mode if result else previous.get("last_mode"),
            last_route_id=result.route_id if result else previous.get("last_route_id"),
            last_backend=result.backend_name if result else previous.get("last_backend"),
            feed_hash=result.feed_hash if result else previous.get("feed_hash"),
            destinations_hash=result.destinations_hash if result else previous.get("destinations_hash"),
            changed=result.changed if result else False,
            created=result.created if result else False,
            dry_run=result.dry_run if result else False,
            partial_failure=partial_failure,
            last_error_stage=error_stage,
            bootstrap_source=result.bootstrap_source if result else previous.get("bootstrap_source"),
            bootstrap_network_id=result.bootstrap_network_id if result else previous.get("bootstrap_network_id"),
            bootstrap_target_macs=result.bootstrap_target_macs if result else tuple(previous.get("bootstrap_target_macs", [])),
        )
        self.state_store.write(snapshot)

    def _bootstrap_requires_manual_review(self, source: str | None) -> bool:
        return bool(source and source.startswith("auto-bootstrap"))

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
        saved_macs = tuple(sorted(str(item).strip().lower() for item in state.get("bootstrap_target_macs", []) if str(item).strip()))
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
            bootstrap_source=bootstrap_source,
            bootstrap_network_id=bootstrap_network_id,
            bootstrap_target_macs=bootstrap_target_macs,
        )

    def _plan_route_update(
        self,
        *,
        backend,
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
        if self._bootstrap_requires_manual_review(bootstrap_source) or self._is_pending_auto_bootstrap(route_record, previous_state):
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
        log_event(self.logger, logging.INFO, "route_plan", mode="local", summary=result.summary)
        if not self.config.dry_run and plan.has_changes:
            apply_plan(client, backend, plan)
        return result

    def _bootstrap_route(
        self,
        client: UniFiClient,
        desired_ips: list[str],
    ) -> tuple[object, BootstrapPreview]:
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
            vpn_network = client.pick_default_vpn_network()
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
        client = UniFiClient(self.config)
        client.login()
        site_context = client.resolve_site_context()
        created = False
        previous_state = self.state_store.load()
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
            try:
                bootstrap_backend.create_route(preview.payload)
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
                        bootstrap_backend.create_route(fallback_payload)
                    except StopLigaError as fallback_exc:
                        raise RouteNotFoundError(
                            f"Route {self.config.route_name!r} not found and bootstrap failed. "
                            f"Primary error: {exc}. Fallback error: {fallback_exc}"
                        ) from fallback_exc
                    applied_preview = BootstrapPreview(
                        backend_name=preview.backend_name,
                        payload=fallback_payload,
                        source="auto-bootstrap-device-fallback",
                    )
                else:
                    raise RouteNotFoundError(
                        f"Route {self.config.route_name!r} not found and bootstrap via {preview.source} failed: {exc}"
                    ) from exc
            created = True
            bootstrap_source = applied_preview.source
            bootstrap_network_id = str(applied_preview.payload.get("network_id") or "") or None
            bootstrap_target_macs = self._route_target_macs(applied_preview.payload)
            backend, endpoint, route_record = choose_existing_route_backend(client, site_context, self.config.route_name)

        return self._plan_route_update(
            backend=backend,
            endpoint=endpoint,
            route_record=route_record,
            feed_snapshot=feed_snapshot,
            previous_state=previous_state,
            created=created,
            bootstrap_source=bootstrap_source,
            bootstrap_network_id=bootstrap_network_id,
            bootstrap_target_macs=bootstrap_target_macs,
            client=client,
        )

    def run_once(self) -> SyncResult:
        log_event(
            self.logger,
            logging.INFO,
            "sync_start",
            route=self.config.route_name,
            site=self.config.site,
            mode="local",
            dry_run=self.config.dry_run,
        )
        feed_snapshot = load_feed_snapshot(self.config)
        try:
            result = self._run_once(feed_snapshot)
            self._write_state(status="dry_run" if result.dry_run else "success", result=result)
            log_event(
                self.logger,
                logging.INFO,
                "sync_finish",
                mode=result.mode,
                changed=result.changed,
                created=result.created,
                route_id=result.route_id,
            )
            return result
        except StopLigaError as exc:
            try:
                self._write_state(
                    status="error",
                    error=str(exc),
                    partial_failure=isinstance(exc, PartialUpdateError),
                    error_stage=exc.stage if isinstance(exc, PartialUpdateError) else None,
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
                        error_stage=exc.stage if isinstance(exc, PartialUpdateError) else None,
                    )
                except StateError as state_exc:
                    log_event(self.logger, logging.ERROR, "state_write_failed", error=state_exc, original_error=exc)
                log_event(self.logger, logging.ERROR, "loop_iteration_failed", error=exc)
            if stop_event.wait(self.config.interval_seconds):
                break
        log_event(self.logger, logging.INFO, "loop_stop")
        return 0
