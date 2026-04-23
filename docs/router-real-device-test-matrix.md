# Real-Device Router Test Matrix

This document tracks the real-hardware validation we should run before merging the in-progress router backends from their feature branches into `main`.

Current targets:

- `bbb/issue-3-fritzbox`
- `bbb/issue-9-keenetic`
- `bbb/issue-10-mikrotik`

## Goal

The purpose of this matrix is to answer three practical questions for each router:

1. Can StopLiga authenticate and talk to the router reliably?
2. Does it create, update and clean up only the managed objects we expect?
3. Does it behave safely on a real network when block status changes over time?

## Test Protocol

Use the same validation rhythm for every router branch:

1. Start from the router-specific branch and its updated `README.md` and `.env.example`.
2. Point StopLiga at a real device or an isolated lab instance, not your production path first if you can avoid it.
3. Run one dry run first.
4. Run one live sync while the block feed is active.
5. Re-run to confirm idempotency.
6. Test the inactive state and confirm cleanup or disable behavior.
7. Capture logs and screenshots of the router UI or CLI after each major step.

Recommended command sequence:

```bash
docker compose up -d
docker compose exec stopliga stopliga --once --dry-run --verbose
docker compose exec stopliga stopliga --once --verbose
docker compose exec stopliga stopliga --once --verbose
docker compose logs --tail=200 stopliga
```

If you are not using Docker:

```bash
.venv/bin/python run_stopliga.py --once --dry-run --verbose
.venv/bin/python run_stopliga.py --once --verbose
```

## Evidence To Collect

For each executed test, keep:

- branch name and commit hash
- router model and firmware version
- exact StopLiga config variant used
- StopLiga logs for the run
- screenshot or export of the affected router objects after the run
- note of whether the test was done on a lab or production-adjacent device

## Cross-Router Matrix

Status legend:

- `todo`: not executed yet
- `pass`: passed on real hardware
- `fail`: reproduced a defect
- `risk`: inconclusive or partially blocked

| ID | Scenario | FRITZ!Box | Keenetic | MikroTik | Notes / Expected Outcome |
| --- | --- | --- | --- | --- | --- |
| C01 | Control plane reachable from StopLiga host | todo | todo | todo | API or management endpoint is reachable without packet loss or timeouts. |
| C02 | Authentication succeeds with intended auth mode | todo | todo | todo | StopLiga completes the first authenticated request without manual intervention. |
| C03 | Dry run succeeds and reports planned changes cleanly | todo | todo | todo | No writes are made; logs clearly show detection of desired state. |
| C04 | First live run creates the managed objects | todo | todo | todo | Only the expected managed objects appear. |
| C05 | Second live run is idempotent | todo | todo | todo | No duplicate routes, rules or lists are created. |
| C06 | Existing unmanaged objects are preserved | todo | todo | todo | StopLiga does not alter unrelated routes, groups or firewall rules. |
| C07 | Feed changes while block is active | todo | todo | todo | Managed destinations update to the new set only. |
| C08 | Inactive state cleanup works as designed | todo | todo | todo | Backend-specific disable/remove behavior matches expectations. |
| C09 | Restart persistence is safe | todo | todo | todo | Restarting StopLiga does not recreate duplicates or lose track of managed state. |
| C10 | TLS verification in strict mode | todo | todo | todo | Valid certificate path works when TLS verification is enabled. |
| C11 | Wrong credentials fail safely | todo | todo | todo | StopLiga aborts cleanly and leaves managed objects untouched. |
| C12 | Large destination list sanity check | todo | todo | todo | No obvious timeout, API truncation or router-side instability with a bigger feed. |

## FRITZ!Box Branch

Target branch: `bbb/issue-3-fritzbox`

Implemented model in that branch:

- TR-064 over HTTPS with digest authentication
- `Layer3Forwarding` service discovery from the TR-064 description
- managed static routes keyed by gateway and metric
- inactive state handled by disabling the managed routes rather than leaving them active

### FRITZ!Box Acceptance Checks

| ID | Scenario | Expected Result |
| --- | --- | --- |
| F01 | TR-064 description discovery | StopLiga can load the TR-064 description and find `Layer3Forwarding` without a hard-coded SOAP path. |
| F02 | User permission model | The configured user has enough rights for route changes and does not get blocked by the FRITZ!Box auth model. |
| F03 | Active block run | Managed static routes for the published destinations are created and enabled. |
| F04 | Inactive block run | Managed static routes are disabled or removed according to branch behavior, and traffic no longer follows the test path. |
| F05 | Unmanaged route preservation | A manually created route with a different metric or gateway remains untouched. |
| F06 | Re-enable after inactive cycle | The same managed routes can be re-enabled cleanly on the next active cycle. |
| F07 | Remote HTTPS access | If the router is reached remotely, digest auth over HTTPS still works without UI-only assumptions. |

### FRITZ!Box Notes

- Use a unique route metric for the StopLiga-managed routes during testing.
- Pay attention to user permissions, remote access restrictions and any FRITZ!Box login hardening on the device.
- If the installation uses 2FA or unusual auth rules, validate that the chosen account still supports TR-064 route changes.

## Keenetic Branch

Target branch: `bbb/issue-9-keenetic`

Implemented model in that branch:

- `/rci/ip/route`
- managed static routes bound to a chosen interface
- optional gateway and `auto` or `reject` flags
- inactive state handled by removing the managed routes

### Keenetic Acceptance Checks

| ID | Scenario | Expected Result |
| --- | --- | --- |
| K01 | RCI authentication and route listing | StopLiga can read the current route list and authenticate consistently. |
| K02 | Active block run | Managed static routes are created for the current feed on the selected interface. |
| K03 | Host and network route shapes | Both `/32` destinations and larger CIDRs are represented correctly on the router. |
| K04 | Inactive block run | Managed routes are removed and no stale entries remain. |
| K05 | Unmanaged route preservation | Existing routes that do not match the StopLiga-managed signature remain untouched. |
| K06 | Optional gateway path | If a gateway is configured, routes resolve as expected and do not fall back incorrectly. |
| K07 | `auto` and `reject` behavior | If those flags are used, the router applies the intended route semantics and StopLiga can still reconcile the routes on the next run. |

### Keenetic Notes

- This branch currently validates the static-route path, not the future DNS-based route/object-group idea from the issue research.
- Confirm the exact route shape shown by the router UI after StopLiga writes both host and subnet entries.
- If you test `reject=true`, do it on a lab path first.

## MikroTik Branch

Target branch: `bbb/issue-10-mikrotik`

Implemented model in that branch:

- RouterOS REST API
- managed `address-list`
- dedicated routing table
- default route in that table
- `mangle` rule with `mark-routing`
- inactive state handled by disabling the managed routing rule

### MikroTik Acceptance Checks

| ID | Scenario | Expected Result |
| --- | --- | --- |
| M01 | REST auth and API access | StopLiga authenticates to the RouterOS REST API and can enumerate existing objects. |
| M02 | First live run bootstrap | The routing table, default route, address-list and mangle rule are created or reused correctly. |
| M03 | Address-list reconciliation | The managed list matches the feed exactly after an update and stale managed entries are pruned. |
| M04 | Rule toggle on inactive state | The mangle rule is disabled when the block is inactive and re-enabled when active again. |
| M05 | Existing router policy preservation | Unrelated mangle rules, routes and address-lists are untouched. |
| M06 | FastTrack interaction | The test path still works as intended and FastTrack does not bypass the routing mark unexpectedly. |
| M07 | Gateway failure mode | If the configured gateway is invalid or unavailable, StopLiga fails safely and the previous managed state remains understandable. |

### MikroTik Notes

- Validate both with and without FastTrack if the target router uses it.
- Record the exact RouterOS version because REST behavior can drift between releases.
- Confirm that the dedicated routing table already has a usable egress path before blaming StopLiga for traffic not moving.

## Suggested Execution Order

To reduce risk, run the first real tests in this order:

1. Keenetic
2. FRITZ!Box
3. MikroTik

Reasoning:

- Keenetic is the simplest current implementation model.
- FRITZ!Box adds TR-064 and auth nuance but still stays close to static-route management.
- MikroTik has the widest operational surface because routing policy, address-lists and FastTrack can all interact.

## Exit Criteria Before Merge

A router branch should not merge until:

- all common checks `C01` to `C11` are either `pass` or explicitly waived with a written reason
- the router-specific checks for that backend are `pass`
- at least one active cycle and one inactive cycle have been validated on a real device
- we have evidence that unmanaged router objects remain untouched
- the operator notes are good enough for a first user-facing troubleshooting section
