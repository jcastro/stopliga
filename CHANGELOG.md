# Changelog

## 0.1.31 — 2026-11-08

### Runtime and resources

- Switch the pinned Python 3.12 base from Debian Slim to Alpine for AMD64 and ARM64.
- Run as UID/GID 10001 by default so the included Compose works with all capabilities removed and a read-only filesystem. Preserve the existing data bind mount; document ownership preparation and upgrades.
- Remove pip, setuptools, wheel and bytecode from the build environment before copying it into the runtime image.
- Parse Hayahora history incrementally from the bounded download stream and retain the latest timestamped change per entry, with a bounded entry count. Negotiate gzip and bound both compressed and expanded responses.
- Reuse router clients and TLS contexts with bounded lifetimes; import only the selected backend. Use a minimal read-only container healthcheck.
- Wait for actual schedule transitions without increasing normal feed polling. Write a compact bootstrap guard only when it changes. Rotate Compose logs at 5 MiB × 3 files.

### Correctness and recovery

- Preserve UniFi fallback review requirements across repeated runs, previews and restarts. Refuse another bootstrap POST unless the first attempt was explicitly rejected.
- Persist intent before remote writes; fail health and stop further writes after an interrupted or uncertain operation. Verify completed rollback state.
- Restrict Omada group ownership to exact generated names; protect groups referenced by another route. Detect incomplete or repeated paginated inventories.
- Reject stale or future history against the current UTC clock, preserving the last remote state. Close scheduled routes even if fetching crosses a boundary or the feed fails at closing time or notifications delay the cycle.
- Authenticate Gotify with `X-Gotify-Key`. Persist pending transitions and acknowledgments separately per provider, retry failed providers after restart and preserve successful deliveries.
- Bound notification and controller error responses; normalize Omada timeouts, oversized responses and invalid encodings. Keep UniFi GET retries free of previous error bodies.
- Validate OPNsense application-level failures, reject duplicate alias/rule matches, read back changes and restore confirmed partial changes.
- Handle invalid state encoding and timestamps without uncontrolled exceptions. Fsync the state directory after atomic replacement.
- Honor explicit router selection over legacy environment aliases, reject non-finite timeouts and restore custom JSON/DNS/TXT feed compatibility.
- Disable routes consistently when filtered destinations are empty. Keep previews from rewriting applied state, bootstrap guards or notification acknowledgments.

### Validation

- Add behavioral regressions for the audit findings and run the full suite on Python 3.11, 3.12 and 3.13.
- Build and execute the actual Alpine Compose on AMD64 and ARM64, testing permissions, health, persistence, updates, stale-feed recovery, schedules, restart and shutdown against an isolated HTTPS controller.

### Upgrade

Keep `./data` and `bootstrap_guard.json`. Ensure the mounted folder and existing state files are writable by UID/GID 10001, or set `STOPLIGA_UID` / `STOPLIGA_GID` to their current owner. See the README upgrade instructions.
