# UniFi Network 10.3.x Compatibility Check

Validated on 2026-04-22 against the official UniFi Network 10.3.55 release notes and the current Ubiquiti help/developer docs.

## Summary

Current verdict: low risk for StopLiga's UniFi backend.

Nothing in the official UniFi Network 10.3.55 release notes indicates a documented breaking change for the local route-management flow that StopLiga uses. The larger platform shift is around hosting and API positioning:

- Ubiquiti continues to push self-hosted users toward UniFi OS Server.
- The public developer portal documents the Site Manager API (`api.ui.com`), not the local per-controller Network configuration API.
- Ubiquiti's own help center says the local Network API documentation is version-specific and is exposed inside each UniFi Network instance under `Settings > Control Plane > Integrations`.

That means the highest-value post-upgrade check is still to verify the local, version-specific Network API docs from the upgraded controller and run a StopLiga dry run against the real site.

## Official Doc Review

### UniFi Network 10.3.55 release notes

The official release notes focus on:

- Identity Firewall
- Client Devices in Infrastructure Topology and Device Supervisor
- WiFi Broadcast, DNS Assistance, DNS Policy orchestration
- VPN and IPsec settings improvements
- general bugfixes and UX improvements

I did not find any documented removal or rename of UniFi traffic-route endpoints or a note that route-management APIs had moved to a different control plane.

### Official API guidance

Ubiquiti's help center currently splits official API guidance into two layers:

- `Site Manager API`: public developer docs at `developer.ui.com`, used through `api.ui.com`
- `Local Application APIs`: version-specific docs hosted by each local application instance

For UniFi Network specifically, the official help article says to open:

- `UniFi Network > Settings > Control Plane > Integrations`

to access the localized Network API documentation for the installed version.

### Developer portal scope

The current developer portal content for UniFi is still centered on the Site Manager API:

- hosts
- sites
- devices
- ISP metrics
- SD-WAN config resources

The Site Manager API docs also state that some response structures can vary by UniFi OS or Network version, so clients should tolerate optional or changing fields. That aligns with the defensive parsing already used by StopLiga.

### Self-hosting guidance

The current self-hosting docs now describe UniFi OS Server as the new standard for self-hosting, replacing the legacy UniFi Network Server.

This does not, by itself, imply a breaking change for StopLiga's local route sync, but it does raise the long-term maintenance risk around:

- deployment packaging
- local access patterns
- the exact in-app location of API docs and integrations

## Why StopLiga Still Looks Safe

StopLiga's UniFi backend already has a few compatibility guards:

- it autodetects whether UniFi Network is exposed under `/proxy/network` or at the root path
- it resolves the site using both the older site list and the newer "official/integration" site shapes
- it tries both the modern `v2 ... /trafficroutes` endpoint and the legacy `rest/trafficroute` endpoint
- it tolerates multiple destination-field shapes, including linked traffic-matching lists

This does not guarantee that a future UniFi release cannot break the backend, but it lowers the chance that a routine `10.3.x` update will break existing installs.

## Post-Upgrade Checklist

Run this after upgrading UniFi Network to any `10.3.x` build.

1. Confirm local access still works.
   Open the controller locally and verify you can reach the UniFi Network UI normally.

2. Check the in-app API docs for the upgraded version.
   In UniFi Network, open `Settings > Control Plane > Integrations`.

3. Confirm the local API model still matches expectations.
   Spot-check that the upgraded instance still exposes:
   - API key-based local access for integrations
   - site-scoped Network API docs for the installed version
   - route-related resources in the local Network API docs, if documented in your build

4. Run a StopLiga dry run against the upgraded controller.

```bash
docker compose exec stopliga stopliga --once --dry-run --verbose
```

If the container is not already running:

```bash
docker compose up -d
docker compose exec stopliga stopliga --once --dry-run --verbose
```

5. Check the StopLiga logs.

Healthy signs:

- `Detected UniFi Network API`
- `Connected to UniFi site`
- `Found managed route`
- `Dry run completed` or `No route changes needed`

Potential break indicators:

- authentication failures after a UniFi upgrade
- site discovery failures
- route discovery failures
- `unsupported_route_shape`

6. If the dry run is clean, let the normal loop continue.

```bash
docker compose logs --tail=100 stopliga
```

## Re-check Triggers

Re-run this checklist whenever one of these changes:

- UniFi Network minor version (`10.3.x` -> later `10.3.y`)
- UniFi OS / UniFi OS Server migration
- API key regeneration or Control Plane / Integrations settings changes
- route model changes in UniFi itself

## Sources

- Official release notes: https://community.ui.com/releases/UniFi-Network-Application-10-3-55/3ed8145b-94a2-44b2-a42e-2d970f135b7b
- Official API overview: https://help.ui.com/hc/en-us/articles/30076656117655-Getting-Started-with-the-Official-UniFi-API
- Official local management guide: https://help.ui.com/hc/en-us/articles/28457353760919-UniFi-Local-Management
- Official self-hosting guide: https://help.ui.com/hc/en-us/articles/34210126298775-Self-Hosting-UniFi
- Official developer portal (Site Manager API): https://developer.ui.com/site-manager/v1.0.0/gettingstarted
