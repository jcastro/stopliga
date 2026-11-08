# UniFi Network 11 validation

Validated on 2026-11-08 with a real UCG Fiber running UniFi Network 11.0.99
and UniFi OS 6.0.17. The existing deployment was StopLiga 0.1.29; the proposed
changes were exercised from a temporary source directory without replacing or
restarting that deployment.

## Results

The existing `/proxy/network/v2/api/site/{site}/trafficroutes` backend accepted
route creation and returned the persisted route on a subsequent GET. A first
check using the installed 0.1.29 client also successfully created, read back and
deleted a disabled test route.

The candidate source passed these live controller operations:

| Scenario | Result |
| --- | --- |
| Create disabled route, including verification when no subsequent PUT is needed | Pass |
| Enable inside the active window | Pass |
| Repeat sync with identical destinations | Pass; no changes |
| Update destinations | Pass |
| Disable outside the window without fetching an unavailable feed | Pass |
| Re-enable using a new service instance and persisted state | Pass |
| Delete temporary route and compare original managed-route configuration | Pass; original unchanged |

The tests used a uniquely named temporary route, the existing VPN Client network,
and documentation-only destinations (`192.0.2.1` and `192.0.2.2`). Feed contents
and the closed-window decision were controlled test inputs; controller reads and
writes were real. No real sports-event traffic was redirected for the test.

The existing managed StopLiga route was also visible in the web interface under
**Settings > Policy Engine > Policy Table**, with type **Policy-Based Route**.

## Issue #21 assessment

The reported missing-route behavior was **not reproduced on 11.0.99 / 6.0.17**.
This result does not establish compatibility with every 11.0.x / 6.0.x build and
does not identify the cause in the reporter's installation. It confirms API
configuration persistence, not VPN data-plane forwarding.

The changes improve diagnostics and detect additional failures:

- Read back newly created routes even when their POST response requires no
  follow-up update.
- Reject an absent or non-boolean `enabled` state during verification.
- Treat `meta.rc` application errors as failures even with HTTP 200.
- Explain an intentionally skipped creation as
  `Route not created: no active destinations`.

Before attributing a remaining failure to a controller upgrade, collect the exact
StopLiga image/version, exact Network and OS versions, the affected site and route
name, whether dry run is enabled, and verbose logs showing feed status,
`created`, `enabled`, destination count and backend selection. Remove credentials
and personal network data before sharing logs.
