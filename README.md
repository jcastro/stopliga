# StopLiga

StopLiga keeps a UniFi Policy-Based Route named `StopLiga` in sync with the status and IP list published by `r4y7s/laliga-ip-list`.

This project is meant to run as a Docker container.

## What it does

- downloads the status feed
- downloads the IP list
- validates and normalizes IPs/CIDRs
- finds a UniFi route named `StopLiga`
- updates only destinations and `enabled`
- keeps existing VPN / next hop / targets unchanged

## Requirements

- UniFi gateway or controller reachable on the local network
- a local UniFi API key, or local username/password
- Docker

## Quick Start

1. Create `.env` from the example.
2. Set your UniFi host and credentials.
3. Start the container with Docker Compose.

```bash
cp .env.example .env
mkdir -p secrets
docker compose pull
docker compose up -d
```

## Recommended `.env`

Use an API key file if possible:

```dotenv
UNIFI_HOST=10.0.1.1
UNIFI_API_KEY_FILE=/run/secrets/unifi_api_key
UNIFI_SITE=default
UNIFI_VERIFY_TLS=false

STOPLIGA_RUN_MODE=loop
STOPLIGA_SYNC_INTERVAL_SECONDS=300
STOPLIGA_ROUTE_NAME=StopLiga

# Optional notifications
# STOPLIGA_GOTIFY_URL=https://gotify.example.com
# STOPLIGA_GOTIFY_TOKEN=replace-me
# STOPLIGA_GOTIFY_PRIORITY=5
# STOPLIGA_TELEGRAM_BOT_TOKEN=123456:replace-me
# STOPLIGA_TELEGRAM_CHAT_ID=123456789
```

Create the secret file on the host:

```bash
mkdir -p secrets
printf '%s\n' 'YOUR_UNIFI_API_KEY' > secrets/unifi_api_key
chmod 600 secrets/unifi_api_key
```

If you prefer username/password:

```dotenv
UNIFI_HOST=10.0.1.1
UNIFI_USERNAME=admin
UNIFI_PASSWORD=replace-me
UNIFI_SITE=default
UNIFI_VERIFY_TLS=false

STOPLIGA_RUN_MODE=loop
STOPLIGA_SYNC_INTERVAL_SECONDS=300
STOPLIGA_ROUTE_NAME=StopLiga
```

## Docker Compose

The repo includes a simple production-oriented compose file:

```yaml
services:
  stopliga:
    image: bluepr0/stopliga:latest
    container_name: stopliga
    restart: unless-stopped
    env_file:
      - .env
    command: ["--loop"]
    volumes:
      - ./data:/data
      - ./secrets:/run/secrets:ro
    healthcheck:
      disable: true
```

## Docker Run

```bash
docker run -d \
  --name stopliga \
  --restart unless-stopped \
  --env-file .env \
  -v "$(pwd)/data:/data" \
  -v "$(pwd)/secrets:/run/secrets:ro" \
  bluepr0/stopliga:latest --loop
```

## Useful Logs

Typical loop logs:

- `feed_check`
- `feed_revision_resolved`
- `feed_loaded`
- `route_check`
- `route_ip_delta` when IPs changed
- `route_plan`
- `sync_finish`

## Notifications

Optional notifications are supported for:

- Gotify
- Telegram bot + chat/user id

Notifications are sent when:

- the block status changes
- IPs are added or removed

Examples:

```dotenv
STOPLIGA_GOTIFY_URL=https://gotify.example.com
STOPLIGA_GOTIFY_TOKEN=replace-me
```

```dotenv
STOPLIGA_TELEGRAM_BOT_TOKEN=123456:replace-me
STOPLIGA_TELEGRAM_CHAT_ID=123456789
```

Token files are also supported:

```dotenv
STOPLIGA_GOTIFY_TOKEN_FILE=/run/secrets/gotify_token
STOPLIGA_TELEGRAM_BOT_TOKEN_FILE=/run/secrets/telegram_bot_token
```

## Notes

- API key auth is preferred.
- TLS verification is enabled by default.
- `UNIFI_VERIFY_TLS=false` is only for local setups with self-signed certs.
- If the route does not exist, StopLiga can bootstrap it, but day-to-day operation is only destination and enable/disable sync.

## Image

- `bluepr0/stopliga:latest`
- `bluepr0/stopliga:0.1.4`
