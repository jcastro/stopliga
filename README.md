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

Si tu versión de UniFi no acepta el placeholder con `Source = Any`, el fallback es crear la route una vez en la UI o degradar a un dispositivo concreto si ese backend obliga a ello.

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

Con esas dos, si la route no existe, StopLiga puede crearla ya asociada a una VPN cliente y a equipos concretos. Si no se aportan, intentará crear un placeholder deshabilitado usando `Source = Any` y la primera VPN cliente disponible.

## Variables de entorno

| Variable | Obligatoria | Default | Descripción |
|---|---|---:|---|
| `UNIFI_HOST` | Sí | - | IP o hostname local del router/controlador |
| `UNIFI_API_KEY` | No | - | API key local de UniFi |
| `UNIFI_API_KEY_FILE` | No | - | Fichero con la API key local |
| `UNIFI_USERNAME` | No | - | Usuario para login local |
| `UNIFI_PASSWORD` | No | - | Contraseña para login local |
| `UNIFI_PASSWORD_FILE` | No | - | Fichero con la contraseña local |
| `UNIFI_SITE` | No | `default` | Site de UniFi |
| `UNIFI_VERIFY_TLS` | No | `true` | Verifica el certificado TLS |
| `UNIFI_CA_FILE` | No | - | CA propia para TLS |
| `STOPLIGA_ROUTE_NAME` | No | `StopLiga` | Nombre exacto de la route |
| `STOPLIGA_RUN_MODE` | No | `once` | `once` o `loop` |
| `STOPLIGA_SYNC_INTERVAL_SECONDS` | No | `300` | Intervalo del loop |
| `STOPLIGA_LOG_LEVEL` | No | `INFO` | Nivel de log |
| `STOPLIGA_DRY_RUN` | No | `false` | No escribe en UniFi |
| `STOPLIGA_STATE_FILE` | No | `/data/state.json` | Fichero de estado |
| `STOPLIGA_BOOTSTRAP_GUARD_FILE` | No | `/data/bootstrap_guard.json` | Guarda el marcador de bootstrap provisional |
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
- `STOPLIGA_FEED_ALLOW_PRIVATE_HOSTS`
- `STOPLIGA_STRICT_FEED_CONSISTENCY`

## Autenticación

StopLiga usa la API local del router. La autenticación puede hacerse con API key o con usuario y contraseña.

También admite `*_FILE` para cargar secretos desde ficheros montados, por ejemplo `UNIFI_API_KEY_FILE=/run/secrets/unifi_api_key`.

Prioridad recomendada:

- `UNIFI_API_KEY` si tu controlador expone esos endpoints con esa autenticación
- `UNIFI_USERNAME` y `UNIFI_PASSWORD` como fallback compatible

UniFi documenta que, si ya tienes Remote Management configurado, puedes usar la misma UI Account para iniciar sesión localmente. Si no, necesitas credenciales locales del controlador. Fuente: <https://help.ui.com/hc/en-us/articles/28457353760919-UniFi-Local-Management>

## Seguridad

- TLS se verifica por defecto.
- Mejor opción: `UNIFI_CA_FILE`.
- Opción temporal en laboratorio: `UNIFI_VERIFY_TLS=false`.
- Los feeds remotos se validan y, por defecto, rechazan hosts privados/locales y URLs con credenciales embebidas.
- El contenedor corre como usuario no root.
- En Docker, el entrypoint prepara `/data` como root y baja enseguida al `uid/gid` configurado para evitar problemas típicos de permisos con bind mounts en Linux.
- Logs por stdout/stderr.
- `state.json`, `bootstrap_guard.json` y `stopliga.lock` viven en `/data`.

## Comportamiento de la route

Si la route existe:

- StopLiga preserva VPN, next hop y targets.
- Solo actualiza IPs y `enabled`.

Si la route no existe:

- intenta crear `StopLiga`
- si no hay `STOPLIGA_VPN_NAME` ni `STOPLIGA_TARGETS`, usa la primera VPN cliente disponible de forma determinista y crea `Source = Any` con `target_devices=[{"type":"ALL_CLIENTS"}]`
- si sí existen, intenta crearla completa

Si se autocrea sin variables explícitas, StopLiga guarda esa asignación automática como provisional y no la habilitará hasta que el usuario cambie VPN o targets desde la UI de UniFi. Si un backend rechaza `ALL_CLIENTS`, el último recurso es degradar a un dispositivo concreto y dejar igualmente la route deshabilitada hasta revisión manual.

## Consistencia del feed

- Por defecto, StopLiga fija `laliga_status.json` y `laliga_ip_list.txt` a la misma revisión de GitHub antes de sincronizar.
- Si `STOPLIGA_STRICT_FEED_CONSISTENCY=true`, un fallo resolviendo esa revisión detiene la sincronización.
- Si `STOPLIGA_STRICT_FEED_CONSISTENCY=false`, degrada al fetch directo de ambos ficheros y lo deja reflejado en logs.
- `STOPLIGA_FEED_ALLOW_PRIVATE_HOSTS=true` existe solo para laboratorio o tests locales; en producción conviene mantenerlo en `false`.

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

### Publicar en Docker Hub

La release automática usa [publish-docker.yml](/Users/jonatan/Code/stopliga/.github/workflows/publish-docker.yml:1) y se dispara al hacer push de una tag `v*`.

Secrets requeridos en GitHub:

- `DOCKERHUB_USERNAME`
- `DOCKERHUB_TOKEN`

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

El proyecto incluye [docker-compose.yml](/Users/jonatan/Code/stopliga/docker-compose.yml:1).

```bash
cp .env.example .env
mkdir -p secrets
docker compose pull
docker compose up -d
```

Si quieres evitar secretos en variables de entorno:

```bash
printf '%s\n' 'replace-me' > secrets/unifi_api_key
printf '%s\n' 'admin' > secrets/unifi_username
printf '%s\n' 'change-me' > secrets/unifi_password
chmod 600 secrets/unifi_api_key secrets/unifi_username secrets/unifi_password
```

Y en `.env` deja solo `UNIFI_HOST` y referencia los ficheros:

```dotenv
UNIFI_API_KEY_FILE=/run/secrets/unifi_api_key
# o, si usas login local:
# UNIFI_USERNAME_FILE=/run/secrets/unifi_username
# UNIFI_PASSWORD_FILE=/run/secrets/unifi_password
```

El `docker-compose.yml` del repo está simplificado para producción normal:

- imagen `bluepr0/stopliga:latest`
- `uid/gid 1000`
- volumen `./data:/data`
- secretos en `./secrets:/run/secrets:ro`

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
