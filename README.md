# StopLiga

StopLiga keeps a managed router route named `StopLiga` in sync with the live block status published by [`hayahora.futbol`](https://hayahora.futbol/) and the destination IP list published by [`r4y7s/laliga-ip-list`](https://github.com/r4y7s/laliga-ip-list).

Today the bundled backends are `unifi`, `omada`, and `opnsense`. `unifi` remains the default.

> Pick a backend, fill in the matching credentials, and StopLiga takes care of the managed route or rule automatically.

This repo is meant to run with Docker.

## At a Glance

- Creates the UniFi policy route automatically if it does not exist
- Reuses and updates the route if it already exists
- Picks the first available UniFi network whose purpose is `vpn-client`
- Applies the route to `ALL_CLIENTS` when UniFi allows it
- Supports Omada Controller Open API and OPNsense too
- Enables or disables the route based on the live blocking status from `hayahora.futbol`
- Syncs every 5 minutes by default

By default, StopLiga uses the live DNS status feed published by [`hayahora.futbol`](https://hayahora.futbol/) to decide whether blocking is active, and the public destinations published by [`r4y7s/laliga-ip-list`](https://github.com/r4y7s/laliga-ip-list) for the managed route contents.

## Requirements

For UniFi you need:

- a UniFi gateway/controller reachable from the container
- a local UniFi Network API key
- at least one UniFi VPN Client network already configured in UniFi

> StopLiga does not create the VPN tunnel itself. It creates and manages the UniFi policy route that uses that tunnel.

For Omada you need:

- an Omada Controller with Open API enabled
- the Controller Interface Access Address, Client ID, Client Secret and Omada ID
- a target WAN or VPN already configured in Omada
- at least one LAN network in the target site

For OPNsense you need:

- an OPNsense firewall reachable from the container over HTTPS
- an API key and secret with access to firewall aliases and rules
- a firewall rule created once whose description exactly matches `STOPLIGA_ROUTE_NAME`

## Quick Start

1. Copy the example environment file.
2. Choose a backend.
3. Fill in the matching credentials.
4. Start the container.
5. Follow the logs.

```bash
cp .env.example .env
docker compose pull
docker compose up -d
docker compose logs -f
```

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

If you prefer single commands after the environment is ready:

```bash
.venv/bin/python -m ruff check src tests
.venv/bin/python -m ruff format --check src tests
.venv/bin/python -m mypy src
.venv/bin/python -m pytest
.venv/bin/python -m pip_audit
.venv/bin/python -m compileall run_stopliga.py src tests
```

### Minimal `.env` for UniFi

Your `.env` can stay as simple as this:

```dotenv
STOPLIGA_BACKEND=unifi
STOPLIGA_CONTROLLER_HOST=10.0.1.1
STOPLIGA_SITE=default
STOPLIGA_CONTROLLER_VERIFY_TLS=false
UNIFI_API_KEY=replace-me
STOPLIGA_RUN_MODE=loop
STOPLIGA_SYNC_INTERVAL_SECONDS=300
STOPLIGA_ROUTE_NAME=StopLiga
STOPLIGA_MAX_RESPONSE_BYTES=2097152
```

### Minimal `.env` for Omada

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
STOPLIGA_MAX_RESPONSE_BYTES=2097152
```

### Minimal `.env` for OPNsense

```dotenv
STOPLIGA_BACKEND=opnsense
OPNSENSE_HOST=fw.example.local
OPNSENSE_API_KEY=replace-me
OPNSENSE_API_SECRET=replace-me
OPNSENSE_VERIFY_TLS=true
STOPLIGA_RUN_MODE=loop
STOPLIGA_SYNC_INTERVAL_SECONDS=300
STOPLIGA_ROUTE_NAME=StopLiga
STOPLIGA_MAX_RESPONSE_BYTES=2097152
```

| Variable | What it is for |
| --- | --- |
| `STOPLIGA_BACKEND` | Preferred backend selector. `STOPLIGA_ROUTER_TYPE` and `STOPLIGA_CONTROLLER` are still accepted |
| `STOPLIGA_CONTROLLER_HOST` | Shared host/IP setting for UniFi and Omada |
| `STOPLIGA_CONTROLLER_PORT` | Shared HTTPS port setting for UniFi and Omada |
| `STOPLIGA_SITE` | Shared site name/identifier for UniFi and Omada |
| `STOPLIGA_CONTROLLER_VERIFY_TLS` | Shared TLS verification flag for UniFi and Omada |
| `UNIFI_API_KEY` | Local UniFi Network API key that StopLiga uses to connect |
| `OMADA_CLIENT_ID` / `OMADA_CLIENT_SECRET` / `OMADA_CONTROLLER_ID` | Omada Open API credentials plus controller/cloud id |
| `OMADA_TARGET_TYPE` / `OMADA_TARGET` | Omada egress target selection |
| `OPNSENSE_HOST` / `OPNSENSE_API_KEY` / `OPNSENSE_API_SECRET` | OPNsense connection and API credentials |
| `STOPLIGA_RUN_MODE` | `loop` keeps the container running and syncing continuously |
| `STOPLIGA_SYNC_INTERVAL_SECONDS` | How often StopLiga checks the feed and updates the selected backend. `300` means every 5 minutes |
| `STOPLIGA_ROUTE_NAME` | Managed route or rule name that StopLiga will create, match, or toggle depending on the selected driver |
| `STOPLIGA_MAX_RESPONSE_BYTES` | Safety limit for downloaded responses. Leave the default unless you have a specific reason to change it |

Preferred aliases for new setups:

- backend: `STOPLIGA_BACKEND`
- shared UniFi/Omada connection: `STOPLIGA_CONTROLLER_HOST`, `STOPLIGA_CONTROLLER_PORT`, `STOPLIGA_SITE`, `STOPLIGA_CONTROLLER_VERIFY_TLS`, `STOPLIGA_CONTROLLER_CA_FILE`

Legacy aliases still work:

- backend: `STOPLIGA_ROUTER_TYPE`, `STOPLIGA_CONTROLLER`
- UniFi connection: `UNIFI_HOST`, `UNIFI_PORT`, `UNIFI_SITE`, `UNIFI_VERIFY_TLS`, `UNIFI_CA_FILE`
- Omada connection: `OMADA_HOST`, `OMADA_PORT`, `OMADA_SITE`, `OMADA_VERIFY_TLS`, `OMADA_CA_FILE`
- Omada auth/target: both `OMADA_*` and `STOPLIGA_OMADA_*`

For the API key, open UniFi Network and go to `Settings > Control Plane > Integrations`, then create or copy a local Network API key and paste it into `UNIFI_API_KEY` in `.env`.

Official reference: [Getting Started with the Official UniFi API](https://help.ui.com/hc/en-us/articles/30076656117655-Getting-Started-with-the-Official-UniFi-API)

For Omada, open `Settings > Platform Integration > Open API`, copy the Controller Interface Access Address, Client ID, Client Secret and Omada ID, then set `STOPLIGA_BACKEND=omada`.

For OPNsense, set `STOPLIGA_BACKEND=opnsense`, then configure `OPNSENSE_HOST`, `OPNSENSE_API_KEY`, and `OPNSENSE_API_SECRET`. StopLiga keeps a managed alias in sync and toggles the existing firewall rule whose description matches `STOPLIGA_ROUTE_NAME`. Legacy compatibility with `STOPLIGA_FIREWALL_BACKEND=opnsense` is still supported.

## What Happens Automatically (UniFi)

Once the container starts:

1. StopLiga resolves the current blocking status.
2. StopLiga downloads the current IP/CIDR list.
3. StopLiga looks for the UniFi route named by `STOPLIGA_ROUTE_NAME`.
4. If the route does not exist, StopLiga creates it automatically.
5. StopLiga compares the published destinations against the UniFi route.
6. StopLiga enables or disables the route to match the published blocking status.
7. StopLiga updates the destination list if it changed.

Normal automatic behavior:

- if the route already exists, StopLiga updates it
- if the route does not exist, StopLiga creates it
- it uses the first available UniFi `vpn-client` network
- it applies the route to `ALL_CLIENTS` when UniFi accepts that target

Fallback behavior:

- if UniFi rejects `ALL_CLIENTS`, StopLiga retries with one detected client device
- that degraded route stays disabled until you review it

## What Happens Automatically (Omada)

Once the container starts in Omada mode:

1. StopLiga resolves the current blocking status.
2. StopLiga downloads the current IP/CIDR list.
3. StopLiga resolves the Omada site and egress target.
4. StopLiga reconciles the managed Omada IP Groups.
5. StopLiga creates or updates the policy route named by `STOPLIGA_ROUTE_NAME`.
6. StopLiga enables or disables that route to match the published blocking status.

## What Happens Automatically (OPNsense)

Once the container starts in OPNsense mode:

1. StopLiga resolves the current blocking status.
2. StopLiga downloads the current IP/CIDR list.
3. StopLiga syncs the managed alias contents with the published destinations.
4. StopLiga enables or disables the existing firewall rule to match the published blocking status.

## VPN Client Network Required

StopLiga can create the policy route automatically, but UniFi must already have at least one VPN Client network.

- StopLiga looks for UniFi networks whose purpose is `vpn-client`.
- If one exists, StopLiga picks the first available one automatically.
- If no `vpn-client` network exists, StopLiga stops and logs a clear error with a link to this section.
- After you create a VPN Client network in UniFi, restart the container.

## Configuration

These are the same values that appear in [`.env.example`](.env.example).

For most users, leave everything except the credentials for their selected backend unchanged.

### Core Settings

```dotenv
STOPLIGA_BACKEND=unifi
STOPLIGA_CONTROLLER_HOST=10.0.1.1
# STOPLIGA_CONTROLLER_PORT=443
STOPLIGA_SITE=default
STOPLIGA_CONTROLLER_VERIFY_TLS=false
UNIFI_API_KEY=replace-me
STOPLIGA_RUN_MODE=loop
STOPLIGA_SYNC_INTERVAL_SECONDS=300
STOPLIGA_ROUTE_NAME=StopLiga
STOPLIGA_MAX_RESPONSE_BYTES=2097152
```

### Omada Controller Settings

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
# Optional:
# OMADA_SOURCE_NETWORKS=LAN,IoT
# OMADA_GROUP_SIZE=16
```

| Variable | What it is for |
| --- | --- |
| `OMADA_CLIENT_ID` | Omada Open API client ID |
| `OMADA_CLIENT_SECRET` | Omada Open API client secret |
| `OMADA_CONTROLLER_ID` | Omada controller/cloud ID |
| `OMADA_TARGET_TYPE` | Egress target kind: `wan` or `vpn` |
| `OMADA_TARGET` | Exact WAN/VPN name or ID to route through |
| `OMADA_SOURCE_NETWORKS` | Optional comma-separated list of LAN names/IDs. If unset, StopLiga uses all LAN networks in the site |
| `OMADA_GROUP_SIZE` | Max IPv4 subnets per managed Omada IP Group. `16` is a conservative default |

### OPNsense Settings

```dotenv
STOPLIGA_BACKEND=opnsense
OPNSENSE_HOST=fw.example.local
OPNSENSE_API_KEY=replace-me
OPNSENSE_API_SECRET=replace-me
OPNSENSE_VERIFY_TLS=true
# Optional:
# OPNSENSE_ALIAS_NAME=StopLiga
```

| Variable | What it is for |
| --- | --- |
| `OPNSENSE_HOST` | OPNsense firewall IP or hostname |
| `OPNSENSE_API_KEY` | OPNsense API key |
| `OPNSENSE_API_SECRET` | OPNsense API secret |
| `OPNSENSE_VERIFY_TLS` | Whether StopLiga should verify the OPNsense HTTPS certificate |
| `OPNSENSE_ALIAS_NAME` | Optional managed alias name. If unset, StopLiga derives one from `STOPLIGA_ROUTE_NAME` |

### Optional Notifications

```dotenv
# STOPLIGA_GOTIFY_URL=https://gotify.example.com
# STOPLIGA_GOTIFY_TOKEN=replace-me
# STOPLIGA_GOTIFY_PRIORITY=5
# STOPLIGA_GOTIFY_ALLOW_PLAIN_HTTP=false
# STOPLIGA_GOTIFY_VERIFY_TLS=true
# STOPLIGA_TELEGRAM_BOT_TOKEN=123456:replace-me
# STOPLIGA_TELEGRAM_CHAT_ID=123456789
# STOPLIGA_TELEGRAM_GROUP_ID=-1001234567890
# STOPLIGA_TELEGRAM_TOPIC_ID=42
```

Telegram options:

- `STOPLIGA_TELEGRAM_CHAT_ID`: send to a private chat or to any chat id you already use today
- `STOPLIGA_TELEGRAM_GROUP_ID`: explicit target for a Telegram group or supergroup
- `STOPLIGA_TELEGRAM_TOPIC_ID`: optional forum topic id inside that Telegram group
- set either `STOPLIGA_TELEGRAM_CHAT_ID` or `STOPLIGA_TELEGRAM_GROUP_ID`, not both
- if `STOPLIGA_TELEGRAM_TOPIC_ID` is set, StopLiga sends the message with Telegram `message_thread_id`

## Sync Cycle

With `STOPLIGA_SYNC_INTERVAL_SECONDS=300`, StopLiga runs a full sync every 5 minutes:

1. resolve the current blocking status
2. download the current IP/CIDR list
3. compare that feed against the managed router object for the selected driver
4. enable or disable the route or rule
5. update the managed destination list if it changed

## Docker Compose

The repo includes a working [`docker-compose.yml`](docker-compose.yml):

```yaml
services:
  stopliga:
    image: ghcr.io/jcastro/stopliga:latest
    container_name: stopliga
    restart: unless-stopped
    env_file:
      - .env
    volumes:
      - ./data:/data
```

The image already includes a built-in Docker healthcheck, so Compose will keep it enabled by default.

Useful commands:

```bash
docker compose up -d
docker compose logs -f
docker compose pull && docker compose up -d
```

## Docker Run

If you do not want Compose:

```bash
docker run -d \
  --name stopliga \
  --restart unless-stopped \
  --env-file .env \
  -v "$(pwd)/data:/data" \
  ghcr.io/jcastro/stopliga:latest
```

## Sources

- Live block status: [`blocked.dns.hayahora.futbol`](https://hayahora.futbol/)
- Destination IP list: [`r4y7s/laliga-ip-list`](https://github.com/r4y7s/laliga-ip-list)
- Thanks to the maintainers of both feeds for publishing and keeping the data available

## Extensibility

The sync service is structured around router drivers.

- `unifi` is the built-in driver today
- `omada` is built in for Omada Controller Open API
- `opnsense` is built in for alias plus firewall-rule synchronization
- `STOPLIGA_BACKEND` is the preferred selector, with `STOPLIGA_ROUTER_TYPE` kept as a compatibility alias
- the core service, feed loading, state handling and notifications are shared so new router integrations can be added without rewriting the sync loop
