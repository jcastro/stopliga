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
- a local UniFi API key
- Docker

## Quick Start

1. Create `.env` from the example.
2. Set your UniFi host and API key.
3. Start the container with Docker Compose.

```bash
cp .env.example .env
docker compose pull
docker compose up -d
```

## Recommended `.env`

```dotenv
UNIFI_HOST=10.0.1.1
UNIFI_API_KEY=replace-me
UNIFI_SITE=default
UNIFI_VERIFY_TLS=false

STOPLIGA_RUN_MODE=loop
STOPLIGA_SYNC_INTERVAL_SECONDS=300
STOPLIGA_ROUTE_NAME=StopLiga
STOPLIGA_MAX_RESPONSE_BYTES=2097152

# Optional notifications
# STOPLIGA_GOTIFY_URL=https://gotify.example.com
# STOPLIGA_GOTIFY_TOKEN=replace-me
# STOPLIGA_GOTIFY_PRIORITY=5
# STOPLIGA_GOTIFY_ALLOW_PLAIN_HTTP=false
# STOPLIGA_GOTIFY_VERIFY_TLS=true
# STOPLIGA_TELEGRAM_BOT_TOKEN=123456:replace-me
# STOPLIGA_TELEGRAM_CHAT_ID=123456789
```

## Docker Compose

The repo includes a simple production-oriented compose file:

```yaml
services:
  stopliga:
    image: ghcr.io/jcastro/stopliga:latest
    container_name: stopliga
    restart: unless-stopped
    env_file:
      - .env
    command: ["--loop"]
    volumes:
      - ./data:/data
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
  ghcr.io/jcastro/stopliga:latest --loop
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

Gotify stays on its own TLS settings:

```dotenv
STOPLIGA_GOTIFY_URL=https://gotify.example.com
STOPLIGA_GOTIFY_TOKEN=replace-me
STOPLIGA_GOTIFY_VERIFY_TLS=true
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

- Authentication is API-key only. Set `UNIFI_API_KEY` in `.env`.
- TLS verification is enabled by default.
- `UNIFI_VERIFY_TLS=false` is only for local setups with self-signed certs.
- Telegram notifications always verify TLS.
- Gotify over plain HTTP is blocked unless `STOPLIGA_GOTIFY_ALLOW_PLAIN_HTTP=true`.
- Large HTTP responses are capped by `STOPLIGA_MAX_RESPONSE_BYTES`.
- If `reconciliation_required` is set in runtime state, StopLiga refuses new writes until the previous partial failure is reviewed.
- If the route does not exist, StopLiga can bootstrap it, but day-to-day operation is only destination and enable/disable sync.

## Image

- `ghcr.io/jcastro/stopliga:latest`
- `ghcr.io/jcastro/stopliga:0.1.7`
