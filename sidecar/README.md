# sidecar — HTTP bridge de ToDus (todus-sdk)

El puente Go (`bridge-todus`) **no habla el protocolo de ToDus directamente**. Este
`sidecar` es la pieza que falta: una aplicación **FastAPI** en Python que usa el
paquete [`todus-sdk`](https://pypi.org/project/todus-sdk/) (v1.10.2) para conectarse
a los servidores de ToDus.

El puente llama a los endpoints HTTP del sidecar (por defecto en
`http://127.0.0.1:8765`, configurable con `network.sidecar_url` en `config.yaml`)
para solicitar códigos SMS, iniciar sesión, enviar mensajes y medios, consultar
contactos/grupos/canales… y el sidecar hace *poll* de su cola para entregar al
puente los mensajes entrantes, acuses de entrega/lectura y presencia.

```
bridge-todus (Go, bridgev2)
        │  HTTP: /request_code, /login, /send_message, /poll, /contacts, ...
        ▼
  sidecar (FastAPI, Python)
        │  todus-sdk (XMPP)
        ▼
  servidores de ToDus (im.todus.cu)
```

## Requisitos

- Python 3.10+
- Solo necesita `requirements.txt` (FastAPI, uvicorn, todus-sdk).

## Instalación y ejecución

```bash
cd sidecar
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

TODUS_QUEUE_DIR=/var/lib/todus-sidecar/queues \
TODUS_TOKEN_DIR=/var/lib/todus-sidecar/tokens \
TODUS_INBOX_DIR=/var/lib/todus-sidecar/inbox \
.venv/bin/python main.py
```

El sidecar escucha en `127.0.0.1:8765` por defecto. Ajusta `network.sidecar_url`
en el `config.yaml` del puente si lo cambias.

## Variables de entorno

| Variable | Descripción | Valor por defecto |
|----------|-------------|-------------------|
| `TODUS_PORT` | Puerto HTTP donde escucha el sidecar | `8765` |
| `TODUS_PROXY` | Proxy opcional que usa todus-sdk (p. ej. `http://proxy:3128`) | *(ninguno)* |
| `TODUS_QUEUE_DIR` | Directorio con las colas SQLite por teléfono | `/opt/todus-service/queues` |
| `TODUS_TOKEN_DIR` | Directorio donde se guardan los tokens de sesión (`<teléfono>.token`) | `/opt/todus-service/tokens` |
| `TODUS_INBOX_DIR` | Directorio donde se guardan los mensajes entrantes pendientes por teléfono | `/opt/todus-service/inbox` |

⚠️ `TODUS_TOKEN_DIR` contiene los tokens de sesión de las cuentas conectadas:
**no lo subas a git** (las carpetas `tokens/`, `queues/` e `inbox/` ya están en el
`.gitignore` del repo).

## Systemd (opcional)

Copia el unit de ejemplo y ajústalo a tus rutas:

```bash
sudo cp sidecar/todus-sidecar.service.example /etc/systemd/system/todus-sidecar.service
# edita las rutas si es necesario
sudo systemctl daemon-reload
sudo systemctl enable --now todus-sidecar
sudo systemctl status todus-sidecar
```

## Endpoints principales

| Endpoint | Uso |
|----------|-----|
| `GET /health` | Comprobación de vida del sidecar |
| `POST /request_code` | Solicita el código SMS para un teléfono |
| `POST /validate_code` | Valida el código y obtiene token (SDK) |
| `POST /login` | Login con código (o token) y guarda la sesión |
| `POST /logout` | Cierra la sesión de un teléfono |
| `POST /poll` | Entrega eventos pendientes de la cola (mensajes, acuses, presencia) |
| `POST /resync` | Fuerza la reconexión XMPP de un teléfono para traer la cola offline del servidor |
| `POST /send_message` | Envía un mensaje directo (espera el acuse del servidor) |
| `POST /send_group_message` | Envía un mensaje a un grupo |
| `POST /send_channel_message` | Envía un mensaje a un canal |
| `POST /send_read_receipt` | Marca un mensaje como leído |
| `POST /send_media` | Envía un archivo/medio |
| `POST /media` | Consulta/descarga de un medio |
| `POST /contacts` | Lista los contactos del teléfono |
| `POST /groups` | Lista los grupos del teléfono |
| `POST /group_info`, `POST /group_members` | Detalle y miembros de un grupo |
| `POST /channels` | Lista los canales suscritos |
| `POST /channel_info`, `POST /channel_publications` | Detalle y publicaciones de canales |
| `GET /last_stanza/{phone}` | Última trama vista para depuración |
| `POST /debug_*`, `POST /debug_send_raw`, `POST /debug_iq` | Endpoints de depuración (solo para diagnosticar) |
| `POST /inject_message` | Inyecta un mensaje en la cola (pruebas) |

## Notas

- Los mensajes entrantes se guardan primero en `TODUS_INBOX_DIR` y el puente los
  consume con `/poll`.
- El sidecar guarda la sesión en `TODUS_TOKEN_DIR/<teléfono>.token`; si lo reinicias,
  la sesión se reutiliza automáticamente.
- El puente y el sidecar deben poder verse entre sí: si corres ambos en la misma
  máquina, `127.0.0.1` funciona; si el sidecar está en otra máquina, expón su
  puerto solo por una red de confianza (el sidecar no tiene TLS).
- **Mensajes offline:** el servidor de ToDus no implementa MAM (historial remoto),
  pero reentrega la cola de mensajes no entregados al reconectar la sesión y
  anunciar presencia. El puente llama a `POST /resync` al arrancar (y a mano con
  `!todus resync`) para pedir esa cola: los mensajes recibidos mientras el puente
  estuvo caído llegan al inbox con `offline_ts` y el puente les asigna el timestamp
  original.
