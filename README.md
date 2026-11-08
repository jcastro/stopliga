# StopLiga

StopLiga reads the public block status from [`hayahora.futbol`](https://hayahora.futbol/) and keeps one managed route or rule called `StopLiga` in sync.

It uses Hayahora's canonical JSON feed and derives destinations from the active entries in that structured payload.

Supported routers:

- `unifi`
- `omada`
- `opnsense`

## Quick Start

If you can run Docker, you can usually run StopLiga.

1. Copy `.env.example` to `.env`
2. In `.env`, choose your router and fill only that section
3. Prepare the persistent folders for the container user, then start it

```bash
cp .env.example .env
mkdir -p data config
sudo chown 10001:10001 data
docker compose pull
docker compose up -d
docker compose logs -f
```

Most users only need `.env`.

`config.toml` is optional and only useful if you want to keep non-secret settings out of `.env`.

## What StopLiga Does

### UniFi

- creates the managed policy route if it does not exist
- updates it when the IP list changes
- enables it when blocks are active
- disables it when blocks end

### Omada

- creates or updates managed IP groups
- creates or updates the managed policy route
- enables or disables it from the live status

### OPNsense

- updates a managed alias with the published IP list
- enables or disables an existing firewall rule with description `StopLiga`

## Pick Your Router

### UniFi

You need:

- a reachable UniFi gateway or controller
- a local UniFi API key
- at least one VPN Client network already created in UniFi

Minimal `.env`:

```dotenv
STOPLIGA_BACKEND=unifi
STOPLIGA_CONTROLLER_HOST=10.0.1.1
STOPLIGA_SITE=default
STOPLIGA_CONTROLLER_VERIFY_TLS=false
UNIFI_API_KEY=replace-me
STOPLIGA_RUN_MODE=loop
STOPLIGA_SYNC_INTERVAL_SECONDS=300
STOPLIGA_ROUTE_NAME=StopLiga
```

For UniFi Network `10.3.x` upgrade notes and a post-upgrade smoke test, see [docs/unifi-network-10.3-validation.md](docs/unifi-network-10.3-validation.md).

For most UniFi setups, the minimal `.env` above is enough. `STOPLIGA_ROUTE_NAME` is usually the only route-specific setting you need.

Advanced UniFi bootstrap overrides (most users can ignore these):

- Leave both `STOPLIGA_VPN_NAME` and `STOPLIGA_TARGETS` unset to auto-pick the first VPN Client network and target all clients.
- Set only `STOPLIGA_VPN_NAME` to pick the VPN Client network explicitly and still target all clients.
- Set `STOPLIGA_VPN_NAME` and `STOPLIGA_TARGETS` together to limit the route to specific clients.
- `STOPLIGA_TARGETS` accepts client hostnames, display names or MAC addresses. It does not accept network names.

### Omada

You need:

- an Omada Controller with Open API enabled
- Client ID, Client Secret and Omada ID
- a WAN or VPN target already created in Omada

Minimal `.env`:

```dotenv
STOPLIGA_BACKEND=omada
STOPLIGA_CONTROLLER_HOST=omada-controller.example
STOPLIGA_CONTROLLER_PORT=8043
STOPLIGA_SITE=Default
STOPLIGA_CONTROLLER_VERIFY_TLS=true
OMADA_CLIENT_ID=replace-me
OMADA_CLIENT_SECRET=replace-me
OMADA_CONTROLLER_ID=replace-me
OMADA_TARGET_TYPE=vpn
OMADA_TARGET=WG Main
STOPLIGA_RUN_MODE=loop
STOPLIGA_SYNC_INTERVAL_SECONDS=300
STOPLIGA_ROUTE_NAME=StopLiga
```

### OPNsense

You need:

- a reachable OPNsense firewall
- an API key and API secret
- one firewall rule created once in `Firewall > Rules [new]` or `Firewall > Automation > Filter`
- that rule must use the exact description `StopLiga`

Important notes:

- StopLiga uses the OPNsense filter API to find and toggle that rule
- a rule created only in the legacy `Firewall > Rules` view may be visible in the UI but not discoverable through the API StopLiga uses

Minimal `.env`:

```dotenv
STOPLIGA_BACKEND=opnsense
OPNSENSE_HOST=fw.example.local
OPNSENSE_API_KEY=replace-me
OPNSENSE_API_SECRET=replace-me
OPNSENSE_VERIFY_TLS=true
STOPLIGA_RUN_MODE=loop
STOPLIGA_SYNC_INTERVAL_SECONDS=300
STOPLIGA_ROUTE_NAME=StopLiga
```

## Files

- `.env`: easiest option and recommended for most people
- `config/config.toml`: optional starter config file if you prefer
- `./data`: where Docker stores state and health information

## Optional `config.toml`

You can skip this section if `.env` is enough for you.

If you want to use a config file:

```bash
mkdir -p config
cp config.example.toml config/config.toml
```

Then keep secrets in `.env` and non-secret settings in `config/config.toml`.

Environment variables still override `config.toml`.

## Docker Compose

The repo already includes [`docker-compose.yml`](docker-compose.yml), so the normal workflow is:

```bash
docker compose up -d
docker compose logs -f
docker compose pull && docker compose up -d
docker compose down
```

The image uses Python 3.12 on Alpine Linux and runs as UID/GID `10001:10001`.
It supports AMD64 and ARM64. Compose uses a read-only root filesystem, drops
all Linux capabilities and rotates logs at 5 MiB × 3 files.

When upgrading an existing installation, stop it first and preserve `./data`.
If it already belongs to UID/GID 10001, no ownership change is needed. For an
older deployment with a different owner, adjust only StopLiga's own files:

```bash
docker compose stop
sudo chown 10001:10001 data
sudo find data -maxdepth 1 -type f \( -name state.json -o -name bootstrap_guard.json -o -name stopliga.lock \) -exec chown 10001:10001 {} +
docker compose pull
docker compose up -d
```

Alternatively, set `STOPLIGA_UID` and `STOPLIGA_GID` in `.env` to the existing
owner. Ensure that user can write the folder and the existing state files;
keep their private permissions. Do not delete the bootstrap guard to solve a
permission issue. The bind mount remains `./data:/data` across upgrades.

What it does:

- mounts `./data` to store runtime state
- optionally mounts `./config`
- automatically uses `config/config.toml` if that file exists

## Docker Run

If you prefer `docker run`:

```bash
docker run -d \
  --name stopliga \
  --restart unless-stopped \
  --env-file .env \
  -v "$(pwd)/data:/data" \
  -v "$(pwd)/config:/config:ro" \
  ghcr.io/jcastro/stopliga:0.1.31
```

The `/config` mount is optional.

## Feed safety and recovery

Hayahora history is parsed incrementally, retaining only the newest change per
entry. Downloads negotiate gzip with separate limits on compressed and expanded
bytes. `STOPLIGA_FEED_MAX_AGE_SECONDS` defaults to 3600 (`[feeds].max_age_seconds`
in TOML). A missing, stale or future history timestamp fails the cycle, leaves
the router unchanged and fails the healthcheck until a fresh feed arrives.

When the schedule closes, StopLiga disables the managed route independently of
feed availability. With no matching destinations it disables the route and keeps
previous destinations for inspection. Custom status JSON (`isBlocked`, `blocked`
or `state`) can still use `STOPLIGA_IP_LIST_URL`; custom DNS status feeds are
supported too. `strict_feed_consistency` rejects an active legacy status with an
empty TXT list. These legacy feeds have no history freshness guarantee.

Before a router write, StopLiga persists an in-progress marker. If a process dies
or a write outcome cannot be confirmed, new writes stop and healthcheck fails.
Inspect the exact managed route/rule, its destinations and pending bootstrap
review first. After reconciling the router, stop StopLiga, back up `state.json`,
clear its `reconciliation_required` flag and restart. Preserve
`bootstrap_guard.json`; it records any manual review still required. Readback
verifies successful updates and completed rollbacks. `--dry-run` preserves
applied state, bootstrap review and pending notification acknowledgments.

## Notifications

StopLiga can also notify through:

- Gotify
- ntfy
- Telegram

When notifications are configured and StopLiga runs in `loop` mode, it sends a startup test message once when the service begins so you can verify delivery without waiting for the next route change.

Gotify sends its configured token in `X-Gotify-Key`. Failed status-change
notifications remain pending per provider and retry on the next successful sync,
including after a restart. Successful providers are acknowledged separately.
Pending status is coalesced to the newest observed state, so an old transition
is not queued indefinitely. Delivery is at least once: a lost acknowledgment
can cause a duplicate message.

Most users can ignore notifications until the main sync is working.

Minimal ntfy `.env`:

```dotenv
STOPLIGA_NTFY_URL=https://ntfy.sh
STOPLIGA_NTFY_TOPIC=replace-me-topic
# STOPLIGA_NTFY_TOKEN=replace-me
STOPLIGA_NTFY_PRIORITY=3
```

## Feed Safety Ceiling

StopLiga refuses to apply an unexpectedly huge feed. The default ceiling is 16384 destinations. If the public list grows again before you update the container image, set `STOPLIGA_MAX_DESTINATIONS=16384` or a higher value in `.env`.

For Omada, StopLiga splits destinations across managed IP Groups. The default `OMADA_GROUP_SIZE=32` lines up with the global feed ceiling and the conservative 512-group safety guard.

## Optional ISP Destination Filtering

By default StopLiga reads active destinations from Hayahora's structured status feed, limited to the last 24 hours.

Set your ISP to keep only active entries for that provider:

```dotenv
STOPLIGA_HAYAHORA_ISP=DIGI
```

If `STOPLIGA_HAYAHORA_ISP` is unset, StopLiga includes active entries for all ISPs in the Hayahora payload.

The lookback window defaults to 24 hours. Most users should leave it as-is, but it can be adjusted:

```dotenv
STOPLIGA_HAYAHORA_LOOKBACK_HOURS=24
```

## Compatibility

New setups should use:

- `STOPLIGA_BACKEND`
- `STOPLIGA_CONTROLLER_HOST`
- `STOPLIGA_CONTROLLER_PORT`
- `STOPLIGA_SITE`
- `STOPLIGA_CONTROLLER_VERIFY_TLS`

Older variable names still work for compatibility.

## Example Files

The repo includes three starter files:

- [`.env.example`](.env.example): simple `.env` example for common Docker setups
- [`config.example.toml`](config.example.toml): simple optional config file
- [`docker-compose.yml`](docker-compose.yml): compose file for the normal Docker setup

## Sync Cycle

With `STOPLIGA_SYNC_INTERVAL_SECONDS=300`, each loop does this:

1. resolves the current block status
2. downloads the current IP/CIDR list
3. compares the desired feed state against the selected backend
4. enables or disables the managed route or rule
5. updates the managed destinations if needed

## Quality Checks

Recommended local workflow:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]"
python -m ruff check src tests
python -m ruff format --check src tests
python -m mypy src
python -m pytest
python -m pip_audit
python -m compileall run_stopliga.py src tests
```

If the environment is already prepared:

```bash
.venv/bin/python -m ruff check src tests
.venv/bin/python -m ruff format --check src tests
.venv/bin/python -m mypy src
.venv/bin/python -m pytest
.venv/bin/python -m pip_audit
.venv/bin/python -m compileall run_stopliga.py src tests
```

For branch-level validation of the in-progress router backends (`FRITZ!Box`, `Keenetic`, `MikroTik`) on real hardware, see [docs/router-real-device-test-matrix.md](docs/router-real-device-test-matrix.md).

## Sources

- Live block status JSON: [`hayahora.futbol/estado/data.json`](https://hayahora.futbol/estado/data.json)

## Extensibility

The sync loop, feed loading, state handling and notifications are shared across all backends.

Current design intent:

- common runtime settings stay in `[app]`, `[feeds]` and shared env vars
- controller-backed routers reuse `[controller]`
- backend-specific credentials and behavior stay grouped in their own sections

That keeps Docker setup simple today while making it easier to introduce more router drivers later.

## Optional active schedule

Keep the container running and optionally restrict managed routing to local-time windows:

```dotenv
STOPLIGA_ACTIVE_DAYS=fri,sat,sun
STOPLIGA_ACTIVE_HOURS=20:00-23:59
STOPLIGA_ACTIVE_TIMEZONE=Europe/Madrid
```

Days use `mon` through `sun`. Days alone allow the whole selected day; hours alone
apply every day. Multiple windows are comma-separated, e.g. `12:00-14:00,20:00-23:00`.
Start times are inclusive and end times exclusive: `20:00-23:59` closes at 23:59.
An overnight window such as `fri` with `22:00-02:00` includes Saturday until 02:00.
Equal start and end times are rejected; omit hours for a full day.
The timezone defaults to `UTC`; IANA zones follow daylight-saving changes.
Repeated local hours are active on both occurrences and skipped hours do not occur.

Inside the window, normal feed-based synchronization runs. Outside it, StopLiga
explicitly disables the managed route/rule through the existing inactive-state
handling, even if the feed is unavailable. Unrelated routes are untouched.
Feed-status notifications are suppressed outside the window, preserving the last
observed feed status. Dry run previews this without changing the router.
The loop checks at least every minute when a schedule is configured; remote API
latency or an unavailable router can delay enforcement. Once mode evaluates only
at execution time and cannot enforce subsequent boundaries. Keep loop mode running.
With no days or hours configured, existing polling behavior is unchanged.

Equivalent TOML keys under `[app]` are `active_days`, `active_hours` and
`active_timezone`; CLI options are `--active-days`, `--active-hours` and
`--active-timezone`. CLI settings override environment variables, which override TOML.

### UniFi: successful sync with no visible route

`Route not created: no active destinations` means that the managed route was absent
and the feed required no active destinations; no route was created. This is an
intentional no-op, not proof of route creation. Check the feed/ISP filter and use
`--once --dry-run --verbose` to inspect the planned action.
Creation and updates are read back through the controller API before success is
reported. A successful API readback does not prove that traffic traverses the VPN.
See [UniFi Network 11 validation](docs/unifi-network-11-validation.md) for the tested build.
