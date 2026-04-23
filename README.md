# StopLiga

StopLiga reads the public block status from [`hayahora.futbol`](https://hayahora.futbol/) and keeps one managed route or rule called `StopLiga` in sync.

Supported routers:

- `unifi`
- `omada`
- `fritzbox`
- `opnsense`

## Quick Start

If you can run Docker, you can usually run StopLiga.

1. Copy `.env.example` to `.env`
2. In `.env`, choose your router and fill only that section
3. Start it

```bash
cp .env.example .env
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

### FRITZ!Box

- discovers the TR-064 `Layer3Forwarding` service from `tr64desc.xml`
- reconciles one managed set of static destination routes
- enables or disables that managed route set from the live status
- collapses raw destinations into aggregated IPv4 CIDRs before writing routes

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
- one firewall rule created once with description `StopLiga`

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

### FRITZ!Box

You need:

- a reachable FRITZ!Box with TR-064 enabled
- a FRITZ! user with configuration rights
- a known next-hop gateway for the alternate path
- a dedicated route metric reserved for StopLiga, so managed routes can be identified safely

Minimal `.env`:

```dotenv
STOPLIGA_BACKEND=fritzbox
STOPLIGA_CONTROLLER_HOST=fritz.box
STOPLIGA_CONTROLLER_PORT=49443
STOPLIGA_CONTROLLER_VERIFY_TLS=true
FRITZBOX_USERNAME=stopliga
FRITZBOX_PASSWORD=replace-me
FRITZBOX_GATEWAY=192.168.178.2
FRITZBOX_ROUTE_METRIC=4096
STOPLIGA_RUN_MODE=loop
STOPLIGA_SYNC_INTERVAL_SECONDS=300
STOPLIGA_ROUTE_NAME=StopLiga
```

Important notes:

- the backend uses official TR-064 `Layer3Forwarding`, not UI scraping
- it currently manages IPv4 static routes only
- choose a unique `FRITZBOX_ROUTE_METRIC`, because TR-064 routes do not have comments or labels

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
  ghcr.io/jcastro/stopliga:0.1.14
```

The `/config` mount is optional.

## Notifications

StopLiga can also notify through:

- Gotify
- Telegram

When notifications are configured and StopLiga runs in `loop` mode, it sends a startup test message once when the service begins so you can verify delivery without waiting for the next route change.

Most users can ignore notifications until the main sync is working.

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

## Sources

- Live block status: [`blocked.dns.hayahora.futbol`](https://hayahora.futbol/)
- Destination IP list: [`r4y7s/laliga-ip-list`](https://github.com/r4y7s/laliga-ip-list)

## Extensibility

The sync loop, feed loading, state handling and notifications are shared across all backends.

Current design intent:

- common runtime settings stay in `[app]`, `[feeds]` and shared env vars
- controller-backed routers reuse `[controller]`
- backend-specific credentials and behavior stay grouped in their own sections

That keeps Docker setup simple today while making it easier to introduce more router drivers later.
