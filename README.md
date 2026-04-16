# StopLiga

StopLiga keeps a UniFi policy-based route named `StopLiga` in sync with the status and IP list published by [`r4y7s/laliga-ip-list`](https://github.com/r4y7s/laliga-ip-list).

> Edit `UNIFI_HOST` and `UNIFI_API_KEY`, start the container, and StopLiga takes care of the route automatically.

This repo is meant to run with Docker.

## At a Glance

- Creates the UniFi policy route automatically if it does not exist
- Reuses and updates the route if it already exists
- Picks the first available UniFi network whose purpose is `vpn-client`
- Applies the route to `ALL_CLIENTS` when UniFi allows it
- Enables or disables the route based on the published blocking status
- Syncs every 5 minutes by default

StopLiga uses the public destinations published by [`r4y7s/laliga-ip-list`](https://github.com/r4y7s/laliga-ip-list). It does not discover those IPs by itself.

## Requirements

You need:

- a UniFi gateway/controller reachable from the container
- a local UniFi Network API key
- at least one UniFi VPN Client network already configured in UniFi

> StopLiga does not create the VPN tunnel itself. It creates and manages the UniFi policy route that uses that tunnel.

## Quick Start

1. Copy the example environment file.
2. Edit only `UNIFI_HOST` and `UNIFI_API_KEY`.
3. Start the container.
4. Follow the logs.

```bash
cp .env.example .env
docker compose pull
docker compose up -d
docker compose logs -f
```

### Minimal `.env`

Your `.env` can stay as simple as this:

```dotenv
UNIFI_HOST=10.0.1.1
UNIFI_API_KEY=replace-me
UNIFI_SITE=default
UNIFI_VERIFY_TLS=false
STOPLIGA_RUN_MODE=loop
STOPLIGA_SYNC_INTERVAL_SECONDS=300
STOPLIGA_ROUTE_NAME=StopLiga
STOPLIGA_MAX_RESPONSE_BYTES=2097152
```

| Variable | What it is for |
| --- | --- |
| `UNIFI_HOST` | IP or hostname of your UniFi router or controller |
| `UNIFI_API_KEY` | Local UniFi Network API key that StopLiga uses to connect |
| `UNIFI_SITE` | UniFi site to use. `default` is the normal value for most setups |
| `UNIFI_VERIFY_TLS` | Whether StopLiga should verify the UniFi HTTPS certificate. `false` is common on home setups with self-signed certs |
| `STOPLIGA_RUN_MODE` | `loop` keeps the container running and syncing continuously |
| `STOPLIGA_SYNC_INTERVAL_SECONDS` | How often StopLiga checks the feed and updates UniFi. `300` means every 5 minutes |
| `STOPLIGA_ROUTE_NAME` | UniFi route name that StopLiga will create or manage automatically |
| `STOPLIGA_MAX_RESPONSE_BYTES` | Safety limit for downloaded responses. Leave the default unless you have a specific reason to change it |

For the API key, open UniFi Network and go to `Settings > Control Plane > Integrations`, then create or copy a local Network API key and paste it into `UNIFI_API_KEY` in `.env`.

Official reference: [Getting Started with the Official UniFi API](https://help.ui.com/hc/en-us/articles/30076656117655-Getting-Started-with-the-Official-UniFi-API)

## What Happens Automatically

Once the container starts:

1. StopLiga downloads the current blocking status.
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

## VPN Client Network Required

StopLiga can create the policy route automatically, but UniFi must already have at least one VPN Client network.

- StopLiga looks for UniFi networks whose purpose is `vpn-client`.
- If one exists, StopLiga picks the first available one automatically.
- If no `vpn-client` network exists, StopLiga stops and logs a clear error with a link to this section.
- After you create a VPN Client network in UniFi, restart the container.

## Configuration

These are the same values that appear in [`.env.example`](.env.example).

For most users, leave everything except `UNIFI_HOST` and `UNIFI_API_KEY` unchanged.

### Core Settings

```dotenv
UNIFI_HOST=10.0.1.1
UNIFI_API_KEY=replace-me
UNIFI_SITE=default
UNIFI_VERIFY_TLS=false
STOPLIGA_RUN_MODE=loop
STOPLIGA_SYNC_INTERVAL_SECONDS=300
STOPLIGA_ROUTE_NAME=StopLiga
STOPLIGA_MAX_RESPONSE_BYTES=2097152
```

### Optional Web UI

StopLiga includes a read-only web dashboard. To enable it, add these lines to your `.env` and expose the port in `docker-compose.yml`.

**`.env`**

```dotenv
STOPLIGA_WEBUI_ENABLED=true
STOPLIGA_WEBUI_PORT=8080
STOPLIGA_WEBUI_HOST=0.0.0.0
```

**`docker-compose.yml`** — add a `ports` entry under the `stopliga` service:

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
    ports:
      - "8080:8080"
    healthcheck:
      disable: true
```

Then open `http://<host-ip>:8080` in your browser. The dashboard shows:

- whether the LaLiga block is currently active or inactive
- the number of IP destinations in the route
- the UniFi connection status
- the timestamp of the last successful sync

The dashboard auto-refreshes every 30 seconds, and has a manual refresh button in the top-right corner.

| Variable | Default | What it is for |
| --- | --- | --- |
| `STOPLIGA_WEBUI_ENABLED` | `false` | Set to `true` to start the web UI |
| `STOPLIGA_WEBUI_PORT` | `8080` | Port the web server listens on inside the container |
| `STOPLIGA_WEBUI_HOST` | `0.0.0.0` | Interface the web server binds to. `0.0.0.0` makes it reachable from outside the container |

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

1. download the current blocking status
2. download the current IP/CIDR list
3. compare that feed against the UniFi route
4. enable or disable the route
5. update the destination IP list if it changed

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
    healthcheck:
      disable: true
```

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

- Data source: [`r4y7s/laliga-ip-list`](https://github.com/r4y7s/laliga-ip-list)
- Thanks to the maintainers of that repository for publishing and keeping the feed updated
