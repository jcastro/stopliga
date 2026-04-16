# StopLiga

StopLiga keeps a UniFi policy-based route named `StopLiga` in sync with the status and IP list published by `r4y7s/laliga-ip-list`.

This repo is meant to be run with Docker.

## Quick Start

1. Copy the example environment file.
2. Set your UniFi host and API key.
3. Start the container with Docker Compose.

```bash
cp .env.example .env
docker compose pull
docker compose up -d
```

## Minimum `.env`

These are the only values you normally need to change:

```dotenv
UNIFI_HOST=10.0.1.1
UNIFI_API_KEY=replace-me
UNIFI_SITE=default
UNIFI_VERIFY_TLS=false
```

Notes:

- `UNIFI_API_KEY` is required. Authentication is API-key only.
- Leave `UNIFI_SITE=default` unless your UniFi setup uses a different site.
- Use `UNIFI_VERIFY_TLS=false` only if your UniFi device uses a self-signed certificate on your local network.

The included `.env.example` already contains the rest of the defaults used by the container.

## Docker Compose

The repo already includes a working [`docker-compose.yml`](/Users/jonatan/Nextcloud/AI/Claude/Apps/StopLiga/docker-compose.yml:1) that uses the published image:

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
```

Useful commands:

```bash
docker compose up -d
docker compose logs -f
docker compose pull && docker compose up -d
```

## Optional Notifications

If you want notifications, add one of these blocks to `.env`.

Gotify:

```dotenv
STOPLIGA_GOTIFY_URL=https://gotify.example.com
STOPLIGA_GOTIFY_TOKEN=replace-me
```

Telegram:

```dotenv
STOPLIGA_TELEGRAM_BOT_TOKEN=123456:replace-me
STOPLIGA_TELEGRAM_CHAT_ID=123456789
```

## Docker Run

If you do not want Compose:

```bash
docker run -d \
  --name stopliga \
  --restart unless-stopped \
  --env-file .env \
  -v "$(pwd)/data:/data" \
  ghcr.io/jcastro/stopliga:latest --loop
```
