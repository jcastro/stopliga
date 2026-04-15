# StopLiga

StopLiga sincroniza una Policy-Based Route de UniFi llamada `StopLiga` con el estado y la lista de IPs publicados en `r4y7s/laliga-ip-list`.

Usa solo la API local del propio router/controlador UniFi. Si la route no existe, intenta crear una route placeholder deshabilitada para que el usuario termine de configurar la VPN y los equipos desde la interfaz de UniFi.

## Qué hace

- descarga `laliga_status.json`
- descarga `laliga_ip_list.txt`
- valida, deduplica y ordena IPs/CIDRs
- busca una route llamada exactamente `StopLiga`
- actualiza solo destinos y estado
- preserva VPN, next hop y targets ya configurados en UniFi
- si falta la route, intenta crear un placeholder seguro

## Flujo recomendado

1. Despliegas el contenedor con solo host y credenciales.
2. StopLiga crea la route `StopLiga` si tu versión de UniFi acepta el placeholder.
3. Entras en UniFi y eliges la VPN y los equipos desde la UI.
4. A partir de ahí StopLiga mantiene IPs y enabled/disabled sincronizados con GitHub.

Si tu versión de UniFi no acepta el placeholder, el fallback es crear la route una vez en la UI y dejar que StopLiga la gestione después.

## Configuración mínima

Variables mínimas:

- `UNIFI_HOST`
- `UNIFI_API_KEY` o `UNIFI_USERNAME` + `UNIFI_PASSWORD`

Variables habituales:

- `UNIFI_VERIFY_TLS`
- `UNIFI_CA_FILE`
- `UNIFI_SITE`
- `STOPLIGA_RUN_MODE`
- `STOPLIGA_SYNC_INTERVAL_SECONDS`
- `STOPLIGA_ROUTE_NAME`

Variables opcionales para creación completa de la route:

- `STOPLIGA_VPN_NAME`
- `STOPLIGA_TARGETS`

Con esas dos, si la route no existe, StopLiga puede crearla ya asociada a una VPN cliente y a equipos concretos. Si no se aportan, intentará crear un placeholder deshabilitado y vacío de VPN/targets.

## Variables de entorno

| Variable | Obligatoria | Default | Descripción |
|---|---|---:|---|
| `UNIFI_HOST` | Sí | - | IP o hostname local del router/controlador |
| `UNIFI_API_KEY` | No | - | API key local de UniFi |
| `UNIFI_USERNAME` | No | - | Usuario para login local |
| `UNIFI_PASSWORD` | No | - | Contraseña para login local |
| `UNIFI_SITE` | No | `default` | Site de UniFi |
| `UNIFI_VERIFY_TLS` | No | `true` | Verifica el certificado TLS |
| `UNIFI_CA_FILE` | No | - | CA propia para TLS |
| `STOPLIGA_ROUTE_NAME` | No | `StopLiga` | Nombre exacto de la route |
| `STOPLIGA_RUN_MODE` | No | `once` | `once` o `loop` |
| `STOPLIGA_SYNC_INTERVAL_SECONDS` | No | `300` | Intervalo del loop |
| `STOPLIGA_LOG_LEVEL` | No | `INFO` | Nivel de log |
| `STOPLIGA_DRY_RUN` | No | `false` | No escribe en UniFi |
| `STOPLIGA_STATE_FILE` | No | `/data/state.json` | Fichero de estado |
| `STOPLIGA_LOCK_FILE` | No | `/data/stopliga.lock` | Lock local |
| `STOPLIGA_VPN_NAME` | No | - | VPN cliente para autocreación completa |
| `STOPLIGA_TARGETS` | No | - | Equipos separados por comas para autocreación completa |

Variables avanzadas:

- `STOPLIGA_STATUS_URL`
- `STOPLIGA_IP_LIST_URL`
- `STOPLIGA_ENABLE_WHEN_BLOCKED`
- `STOPLIGA_INVALID_ENTRY_POLICY`
- `STOPLIGA_MAX_DESTINATIONS`
- `STOPLIGA_REQUEST_TIMEOUT`
- `STOPLIGA_RETRIES`
- `STOPLIGA_HEALTH_MAX_AGE_SECONDS`
- `STOPLIGA_DESTINATION_FIELD`
- `STOPLIGA_DUMP_PAYLOADS_ON_ERROR`
- `STOPLIGA_FEED_VERIFY_TLS`
- `STOPLIGA_FEED_CA_FILE`

## Autenticación

StopLiga usa la API local del router. La autenticación puede hacerse con API key o con usuario y contraseña.

Prioridad recomendada:

- `UNIFI_API_KEY` si tu controlador expone esos endpoints con esa autenticación
- `UNIFI_USERNAME` y `UNIFI_PASSWORD` como fallback compatible

UniFi documenta que, si ya tienes Remote Management configurado, puedes usar la misma UI Account para iniciar sesión localmente. Si no, necesitas credenciales locales del controlador. Fuente: <https://help.ui.com/hc/en-us/articles/28457353760919-UniFi-Local-Management>

## Seguridad

- TLS se verifica por defecto.
- Mejor opción: `UNIFI_CA_FILE`.
- Opción temporal en laboratorio: `UNIFI_VERIFY_TLS=false`.
- El contenedor corre como usuario no root.
- Logs por stdout/stderr.
- `state.json` y `stopliga.lock` viven en `/data`.

## Comportamiento de la route

Si la route existe:

- StopLiga preserva VPN, next hop y targets.
- Solo actualiza IPs y `enabled`.

Si la route no existe:

- intenta crear `StopLiga`
- si no hay `STOPLIGA_VPN_NAME` ni `STOPLIGA_TARGETS`, usa la primera VPN cliente disponible de forma determinista y deja el source en `Any`
- si sí existen, intenta crearla completa

Si se autocrea sin variables explícitas, StopLiga guarda esa asignación automática como provisional y no la habilitará hasta que el usuario cambie VPN o targets desde la UI de UniFi.

## Uso rápido

### `.env` mínimo

```dotenv
UNIFI_HOST=192.168.1.1
UNIFI_API_KEY=replace-me
UNIFI_VERIFY_TLS=false
STOPLIGA_RUN_MODE=loop
```

### Docker build

```bash
docker build -t stopliga .
```

### One-shot

```bash
docker run --rm \
  --env-file .env \
  -v "$(pwd)/data:/data" \
  stopliga --once
```

### Daemon

```bash
docker run -d \
  --name stopliga \
  --restart unless-stopped \
  --env-file .env \
  -v "$(pwd)/data:/data" \
  stopliga --loop
```

## Docker Compose

El proyecto incluye [docker-compose.yml](/Users/jonatan/Nextcloud/AI/Claude/Apps/StopLiga/docker-compose.yml:1).

```bash
cp .env.example .env
docker compose up -d --build
```

Prueba puntual:

```bash
docker compose run --rm stopliga --once --dry-run
```

## CLI

Overrides útiles:

- `--config /config/stopliga.toml`
- `--dry-run`
- `--verbose`
- `--route-name StopLiga`
- `--site default`
- `--api-key "replace-me"`
- `--once`
- `--loop`
- `--interval 300`
- `--insecure-skip-verify`
- `--vpn-name "Mullvad"`
- `--targets "apple-tv,aa:bb:cc:dd:ee:ff"`

## Healthcheck

El contenedor usa:

```bash
stopliga --healthcheck
```

## Troubleshooting

### Certificado autofirmado

- mejor: `UNIFI_CA_FILE`
- rápido: `UNIFI_VERIFY_TLS=false`

### La route no existe

- si tu UniFi acepta placeholder, StopLiga la crea
- si no, créala una vez en la UI y vuelve a ejecutar

### La route no se habilita

Si la route está incompleta, StopLiga la mantiene deshabilitada. Configura desde UniFi:

- VPN o next hop
- equipos/targets

### Shape no soportado

- activa `STOPLIGA_DUMP_PAYLOADS_ON_ERROR=true`
- ejecuta en `--dry-run`
- revisa el payload y usa `STOPLIGA_DESTINATION_FIELD` si hace falta

## Tests

```bash
python3 -m py_compile run_stopliga.py src/stopliga/*.py tests/*.py
PYTHONPATH=src python3 -m unittest tests.test_config tests.test_feed tests.test_integration -v
```

## Referencias

- Feed de estado: <https://raw.githubusercontent.com/r4y7s/laliga-ip-list/main/laliga_status.json>
- Feed de IPs: <https://raw.githubusercontent.com/r4y7s/laliga-ip-list/main/laliga_ip_list.txt>
- UniFi Local Management: <https://help.ui.com/hc/en-us/articles/28457353760919-UniFi-Local-Management>
- Official UniFi API: <https://help.ui.com/hc/en-us/articles/30076656117655-Getting-Started-with-the-Official-UniFi-API>
