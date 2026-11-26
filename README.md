# bridge-todus

Puente de [Matrix](https://matrix.org/) para [ToDus](https://todus.cu), el servicio de mensajería de Cuba.
Conecta tu homeserver de Matrix (Synapse, Conduit, etc.) con tu cuenta de ToDus: recibe y envía mensajes,
ve la presencia de tus contactos, y administra tus grupos y canales desde cualquier cliente Matrix.

Construido con [mautrix-go](https://github.com/mautrix/go) (framework `bridgev2`) a partir del template
[matrix-bridge-quickstart](https://github.com/dvcrn/matrix-bridge-quickstart).

## Cómo funciona

El puente **no** habla directamente con los servidores de ToDus. Se necesitan **dos piezas**:

```
cliente Matrix  <->  bridge-todus (Go, bridgev2)  <->  sidecar (FastAPI/Python)  <->  servidores de ToDus
```

1. **bridge-todus** (este repo, lenguaje Go): el puente de Matrix. Traduce eventos Matrix ↔ ToDus.
2. **sidecar** (carpeta [`sidecar/`](sidecar/README.md) de este repo, lenguaje Python): habla con
   los servidores de ToDus usando el paquete [`todus-sdk`](https://pypi.org/project/todus-sdk/).
   Expone una API HTTP local que el puente usa para loguearse y enviar/recibir mensajes.

El sidecar debe estar corriendo y accesible en `http://127.0.0.1:8765`
(configurable con `network.sidecar_url` en `config.yaml`).

Para iniciar sesión se usa el **número de teléfono** de ToDus y un **código SMS**:
introduces tu número en el chat con el bot, el sidecar pide el código al servidor de ToDus,
y lo introduces cuando el bot te lo pida.

## Características

- Login por número de teléfono + código SMS.
- DMs, grupos y canales de ToDus bridged a salas de Matrix.
- Historial (backfill) en salas vacías.
- Presencia de contactos, confirmaciones de lectura y relay de salas.
- Comandos en español e inglés para administrar el puente (ver abajo).

## Requisitos

- Go 1.26+ (ver `go.mod`).
- Python 3.10+ (para el sidecar).
- Un homeserver de Matrix que soporte appservices (Synapse recomendado).
- (Opcional) libolm para cifrado E2EE: en Debian/Ubuntu `apt install libolm-dev`, en macOS `brew install libolm`.

## Instalación del sidecar (obligatorio)

El sidecar es el componente que conecta el puente con ToDus. **Sin él, el puente no funciona.**

```bash
cd sidecar
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

TODUS_QUEUE_DIR=/var/lib/todus-sidecar/queues \
TODUS_TOKEN_DIR=/var/lib/todus-sidecar/tokens \
TODUS_INBOX_DIR=/var/lib/todus-sidecar/inbox \
.venv/bin/python main.py
```

El sidecar escucha en `127.0.0.1:8765` por defecto (variable `TODUS_PORT`).
La documentación completa (variables de entorno, systemd, endpoints) está en
[`sidecar/README.md`](sidecar/README.md).

## Compilación

```bash
git clone git@github.com:raymand211092/bridge-todus.git
cd bridge-todus
go build
```

Esto genera el binario `bridge-todus` en el directorio del proyecto.
También puedes ejecutar el puente directamente desde el código con `go run .`.

## Configuración

### 1. Genera la configuración de ejemplo

```bash
go run . -e -c config.yaml
```

El archivo `config.yaml` se genera con valores de ejemplo. También puedes copiar el
`example-config.yaml` incluido en el repositorio.

### 2. Edita `config.yaml`

Ajusta al menos estos valores:

| Sección | Clave | Qué poner |
|---------|-------|-----------|
| `homeserver` | `address` | Dirección de tu homeserver (p. ej. `http://localhost:8008` si está en la misma máquina). |
| `homeserver` | `domain` | El `server_name` de tu homeserver (p. ej. `example.com`). |
| `homeserver` | `software` | `standard` para Synapse/Conduit. |
| `appservice` | `id` | Un ID único para el appservice (p. ej. `todus`). |
| `appservice` | `bot.username` | El nombre de usuario del bot (p. ej. `todusbot`). |
| `appservice` | `port` / `hostname` | Dónde escucha el appservice para recibir eventos del homeserver. |
| `database` | `uri` | Ruta a una base de datos SQLite o URI de Postgres. |
| `network` | `sidecar_url` | Dirección del sidecar (por defecto `http://127.0.0.1:8765`). |
| `permissions` | | Quién puede usar el puente (por defecto solo el admin). |

Cuando genere el `registration.yaml` en el paso siguiente, copia los valores de
`id`, `as_token` y `hs_token` del registration dentro de la sección `appservice` de `config.yaml`.

### 3. Genera el registro del appservice

```bash
go run . -g -c config.yaml -r registration.yaml
```

⚠️ **`registration.yaml` contiene secretos** (`as_token` y `hs_token`). No lo compartas
ni lo subas a git (ya está en `.gitignore`).

### 4. Registra el appservice en tu homeserver

Copia el `registration.yaml` generado al directorio de configuración del homeserver y reinícialo.

**Synapse** (Debian/Ubuntu):

```bash
sudo cp registration.yaml /etc/matrix-synapse/conf.d/todus-registration.yaml
sudo systemctl restart matrix-synapse
```

### 5. Arranca el puente

```bash
./bridge-todus -c config.yaml -r registration.yaml
```

También puedes ejecutarlo con `go run . -c config.yaml -r registration.yaml`.
Para más opciones: `./bridge-todus --help`.

## Uso

1. Invita al bot a una sala (p. ej. `@todusbot:example.com`).
2. Envía `login` y sigue las instrucciones: tu número de ToDus y el código SMS.
3. Los chats de ToDus aparecerán como salas de Matrix.

### Comandos

Prefijo por defecto: `!todus` (configurable con `bridge.command_prefix`).

| Comando | Alias | Descripción |
|---------|-------|-------------|
| `!todus contacts` | `contactos` | Lista tus contactos de ToDus y su presencia. |
| `!todus groups` | `grupos` | Lista tus grupos de ToDus. |
| `!todus channels` | `canales` | Lista los canales a los que estás suscrito (solo lectura). |
| `!todus presence` | `estados` | Muestra la presencia observada de tus contactos. |
| `!todus relay` | — | Muestra el estado del relay de la sala. |
| `!todus relay on` / `!todus relay off` | — | Activa/desactiva el relay en la sala. |
| `!todus relay <teléfono> <mensaje>` | — | Envía un mensaje directo de ToDus desde el puente. |

## Estructura del proyecto

```
main.go                  # Punto de entrada: configura, loguea y arranca el puente.
sidecar/                 # Sidecar FastAPI (Python) que habla con los servidores de ToDus.
  main.py                #   API HTTP: /request_code, /login, /send_message, /poll, ...
  requirements.txt       #   fastapi, uvicorn, todus-sdk
  README.md              #   Documentación del sidecar.
connector/
  my_connector.go        # Implementación principal de NetworkConnector (bridgev2).
  login.go               # Login por teléfono + código SMS vía sidecar.
  network_client.go      # Cliente HTTP del sidecar y bucle de polling por sesión.
  network_client_backfill.go  # Backfill de historial.
  handle_matrix.go       # Eventos de Matrix -> ToDus.
  handle_remote.go       # Eventos de ToDus -> Matrix.
  receipts.go            # Confirmaciones de lectura.
  profiles.go            # Perfiles de usuarios/ghosts.
  offline_ts.go          # Timestamps offline.
  autoload.go            # Carga de logins al arrancar.
  commands.go            # Comandos del puente (!todus ...).
  todus_client.go        # Cliente de la API del sidecar (tipos y llamadas).
  config.go              # Configuración específica de ToDus (network.sidecar_url).
  types.go               # Metadatos de portales/ghosts/logins.
example-config.yaml      # Configuración de ejemplo (sin secretos).
gen-config.sh            # Helper para generar config.yaml con bbctl (uso interno de Beeper).
```

## Notas de seguridad

- `config.yaml` y `registration.yaml` contienen credenciales. Ambos están en `.gitignore`:
  **no los subas nunca a un repositorio público**.
- Los directorios de datos del sidecar (`tokens/`, `queues/`, `inbox/`) contienen tokens de
  sesión de los usuarios conectados. Están en `.gitignore` — mantenlos fuera de git.
- Genera tu propio `registration.yaml`; no reutilices el de otra instalación.

## Créditos

- [mautrix-go](https://github.com/mautrix/go) y su framework `bridgev2`.
- Template [matrix-bridge-quickstart](https://github.com/dvcrn/matrix-bridge-quickstart) de [dvcrn](https://github.com/dvcrn).
- [`todus-sdk`](https://pypi.org/project/todus-sdk/), el SDK de Python usado por el sidecar.
