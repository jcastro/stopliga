# StopLiga

StopLiga keeps a firewall route or alias in sync with the status and IP list published by [`r4y7s/laliga-ip-list`](https://github.com/r4y7s/laliga-ip-list).

Supported firewall backends:

- **UniFi** — manages a policy-based traffic route (default)
- **OPNsense** — manages a firewall alias and toggles a firewall rule

> Set your firewall credentials, start the container, and StopLiga takes care of the rest automatically.

This repo is meant to run with Docker.

## At a Glance

- Enables or disables the route/alias based on the published blocking status
- Creates the route/alias automatically if it does not exist
- Reuses and updates it if it already exists
- Syncs every 5 minutes by default

StopLiga uses the public destinations published by [`r4y7s/laliga-ip-list`](https://github.com/r4y7s/laliga-ip-list). It does not discover those IPs by itself.

---

## UniFi

### Requirements

- A UniFi gateway/controller reachable from the container
- A local UniFi Network API key
- At least one UniFi VPN Client network already configured in UniFi

> StopLiga does not create the VPN tunnel itself. It creates and manages the UniFi policy route that uses that tunnel.

### Quick Start

1. Copy the example environment file.
2. Edit only `UNIFI_HOST` and `UNIFI_API_KEY`.
3. Start the container.

```bash
cp .env.example .env
docker compose pull
docker compose up -d
docker compose logs -f
```

### Minimal `.env` (UniFi)

```dotenv
UNIFI_HOST=10.0.1.1
UNIFI_API_KEY=replace-me
UNIFI_SITE=default
UNIFI_VERIFY_TLS=false
STOPLIGA_RUN_MODE=loop
STOPLIGA_SYNC_INTERVAL_SECONDS=300
STOPLIGA_ROUTE_NAME=StopLiga
```

| Variable | What it is for |
| --- | --- |
| `UNIFI_HOST` | IP or hostname of your UniFi router or controller |
| `UNIFI_API_KEY` | Local UniFi Network API key |
| `UNIFI_SITE` | UniFi site name. `default` works for most setups |
| `UNIFI_VERIFY_TLS` | Set `false` for self-signed certs, common on home setups |
| `STOPLIGA_RUN_MODE` | `loop` keeps the container running and syncing continuously |
| `STOPLIGA_SYNC_INTERVAL_SECONDS` | Sync interval in seconds. `300` = every 5 minutes |
| `STOPLIGA_ROUTE_NAME` | Route name StopLiga will create or manage |

For the API key, open UniFi Network → `Settings > Control Plane > Integrations`, then create a local Network API key.

Official reference: [Getting Started with the Official UniFi API](https://help.ui.com/hc/en-us/articles/30076656117655-Getting-Started-with-the-Official-UniFi-API)

### What Happens Automatically (UniFi)

Once the container starts:

1. StopLiga downloads the current blocking status.
2. StopLiga downloads the current IP/CIDR list.
3. StopLiga looks for the UniFi route named by `STOPLIGA_ROUTE_NAME`.
4. If the route does not exist, StopLiga creates it automatically.
5. StopLiga enables or disables the route to match the published blocking status.
6. StopLiga updates the destination list if it changed.

Normal automatic behavior:

- if the route already exists, StopLiga updates it
- if the route does not exist, StopLiga creates it using the first available `vpn-client` network
- it applies the route to `ALL_CLIENTS` when UniFi accepts that target

Fallback behavior:

- if UniFi rejects `ALL_CLIENTS`, StopLiga retries with one detected client device
- that degraded route stays disabled until you review it

### VPN Client Network Required

StopLiga can create the policy route automatically, but UniFi must already have at least one VPN Client network.

- StopLiga looks for UniFi networks whose purpose is `vpn-client`.
- If one exists, StopLiga picks the first available one automatically.
- If no `vpn-client` network exists, StopLiga stops and logs a clear error.
- After you create a VPN Client network in UniFi, restart the container.

---

## OPNsense

### How It Works

StopLiga manages two OPNsense objects:

| Object | Controlled by | Named by |
| --- | --- | --- |
| **Firewall alias** | StopLiga — keeps IPs current | `OPNSENSE_ALIAS_NAME` |
| **Firewall rule** | StopLiga — enables or disables it | `STOPLIGA_ROUTE_NAME` |

```
OPNSENSE_ALIAS_NAME  →  alias (IP list)  ←  StopLiga updates IPs here
                              ↑
STOPLIGA_ROUTE_NAME  →  rule (destination = alias, gateway = VPN)  ←  StopLiga toggles enabled
```

When La Liga is blocking: rule is **enabled** → traffic to the alias IPs goes through the VPN gateway.  
When La Liga is not blocking: rule is **disabled** → rule is skipped, traffic uses the default route.

The alias always contains the current IP list regardless of the rule state.

### Requirements

- OPNsense reachable from the container over HTTPS
- An OPNsense API key and secret with **Firewall: Aliases** and **Firewall: Rules** privileges
- A firewall rule in OPNsense created manually once (StopLiga will enable/disable it)

### OPNsense API Key Setup

1. Log in to OPNsense as administrator.
2. Go to **System → Access → Users**.
3. Select a user or create a new one dedicated to StopLiga.
4. Under *Effective Privileges*, add:
   - `Firewall: Aliases`
   - `Firewall: Alias: Edit`
   - `Firewall: Rules`
   - `Firewall: Rules: [new]`
   - `Firewall: Rules: Edit`
   - `System: Advanced: Firewall and NAT`
5. Scroll to the **Commands** section → click **Create and Download API key** to generate a new key.
6. Copy the **Key** and **Secret** — from the downloaded file.

### Firewall Rule Setup

Create the rule once in **Firewall → Rules**. StopLiga finds it by its `Description` field and enables or disables it on each sync.

Required fields:

| Field | Value |
| --- | --- |
| **Description** | must match `STOPLIGA_ROUTE_NAME` exactly (e.g. `VPN_LIGA`) |
| **Action** | Pass |
| **Interface** | interface your clients use (e.g. LAN) |
| **Direction** | In |
| **Destination** | the alias named by `OPNSENSE_ALIAS_NAME` |
| **Gateway** | your VPN gateway |

Place the rule above any default gateway rules. Save — StopLiga manages the enabled state via API from this point on.

StopLiga creates the alias automatically on first run if it does not exist yet.

### Quick Start (OPNsense)

```bash
cp .env.example .env
# Edit .env with your OPNsense credentials (see below)
docker compose pull
docker compose up -d
docker compose logs -f
```

### Minimal `.env` (OPNsense)

```dotenv
STOPLIGA_FIREWALL_BACKEND=opnsense
OPNSENSE_HOST=192.168.1.1
OPNSENSE_API_KEY=replace-me
OPNSENSE_API_SECRET=replace-me
OPNSENSE_VERIFY_TLS=false
OPNSENSE_ALIAS_NAME=laliga_ips
STOPLIGA_ROUTE_NAME=StopLiga
STOPLIGA_RUN_MODE=loop
STOPLIGA_SYNC_INTERVAL_SECONDS=300
```

| Variable | What it is for |
| --- | --- |
| `STOPLIGA_FIREWALL_BACKEND` | Set to `opnsense` to use OPNsense instead of UniFi |
| `OPNSENSE_HOST` | IP or hostname of your OPNsense router |
| `OPNSENSE_API_KEY` | OPNsense API key |
| `OPNSENSE_API_SECRET` | OPNsense API secret |
| `OPNSENSE_VERIFY_TLS` | Set `false` for self-signed certs, common on home setups |
| `OPNSENSE_ALIAS_NAME` | Name of the alias StopLiga will create and populate with IPs. Defaults to `STOPLIGA_ROUTE_NAME` sanitized |
| `STOPLIGA_ROUTE_NAME` | Exact description of the firewall rule StopLiga will enable or disable |
| `STOPLIGA_RUN_MODE` | `loop` keeps the container running and syncing continuously |
| `STOPLIGA_SYNC_INTERVAL_SECONDS` | Sync interval in seconds. `300` = every 5 minutes |

Secrets can also be loaded from files using `OPNSENSE_API_KEY_FILE` and `OPNSENSE_API_SECRET_FILE`.

### What Happens Automatically (OPNsense)

Once the container starts:

1. StopLiga downloads the current blocking status.
2. StopLiga downloads the current IP/CIDR list.
3. StopLiga searches OPNsense for the alias named by `OPNSENSE_ALIAS_NAME`.
4. If the alias does not exist, StopLiga creates it and populates it with the IP list.
5. If the alias exists, StopLiga updates its IPs if they changed.
6. StopLiga searches for the firewall rule whose description matches `STOPLIGA_ROUTE_NAME`.
7. StopLiga enables or disables that rule to match the published blocking status.

---

## Configuration Reference

### Firewall Backend

| Variable | Default | Description |
| --- | --- | --- |
| `STOPLIGA_FIREWALL_BACKEND` | `unifi` | `unifi` or `opnsense` |

### UniFi Settings

| Variable | Default | Description |
| --- | --- | --- |
| `UNIFI_HOST` | — | UniFi router or controller IP/hostname |
| `UNIFI_API_KEY` | — | Local UniFi Network API key |
| `UNIFI_SITE` | `default` | UniFi site name |
| `UNIFI_PORT` | `443` | UniFi HTTPS port |
| `UNIFI_VERIFY_TLS` | `true` | Set `false` for self-signed certs |
| `UNIFI_CA_FILE` | — | Path to custom CA bundle |

### OPNsense Settings

| Variable | Default | Description |
| --- | --- | --- |
| `OPNSENSE_HOST` | — | OPNsense IP/hostname |
| `OPNSENSE_API_KEY` | — | OPNsense API key |
| `OPNSENSE_API_KEY_FILE` | — | Path to file containing the API key |
| `OPNSENSE_API_SECRET` | — | OPNsense API secret |
| `OPNSENSE_API_SECRET_FILE` | — | Path to file containing the API secret |
| `OPNSENSE_VERIFY_TLS` | `true` | Set `false` for self-signed certs |
| `OPNSENSE_CA_FILE` | — | Path to custom CA bundle |
| `OPNSENSE_ALIAS_NAME` | *(route_name sanitized)* | Name of the alias to create and populate with IPs |

### Common Settings

| Variable | Default | Description |
| --- | --- | --- |
| `STOPLIGA_RUN_MODE` | `once` | `loop` or `once` |
| `STOPLIGA_SYNC_INTERVAL_SECONDS` | `300` | Seconds between syncs in loop mode |
| `STOPLIGA_ROUTE_NAME` | `StopLiga` | Route/alias name to create or manage |
| `STOPLIGA_DRY_RUN` | `false` | Compute changes without writing |
| `STOPLIGA_MAX_DESTINATIONS` | `2048` | Safety ceiling for IP entries |
| `STOPLIGA_MAX_RESPONSE_BYTES` | `2097152` | Safety limit for HTTP response bodies |
| `STOPLIGA_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

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

- `STOPLIGA_TELEGRAM_CHAT_ID`: send to a private chat
- `STOPLIGA_TELEGRAM_GROUP_ID`: send to a Telegram group or supergroup
- `STOPLIGA_TELEGRAM_TOPIC_ID`: optional forum topic id inside that group
- set either `STOPLIGA_TELEGRAM_CHAT_ID` or `STOPLIGA_TELEGRAM_GROUP_ID`, not both

## Sync Cycle

With `STOPLIGA_SYNC_INTERVAL_SECONDS=300`, StopLiga runs a full sync every 5 minutes:

1. download the current blocking status
2. download the current IP/CIDR list
3. compare that feed against the current firewall state
4. enable or disable the route/alias
5. update the IP list if it changed

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
