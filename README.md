# toDus-Matrix Bridge

Un puente bidireccional entre **toDus** (la plataforma de mensajería instantánea cubana) y **Matrix**, construido al estilo de los puentes [mautrix](https://github.com/mautrix).

## ✨ Características

- 🔗 **Puente bidireccional**: Envía y recibe mensajes entre toDus y Matrix
- 👤 **Gestión de usuarios**: Autenticación en toDus mediante comandos de bot
- 💬 **Chats directos**: Crea salas Matrix para chatear con contactos de toDus
- 🤖 **Comandos intuitivos**: Interactúa con el bot usando comandos simples
- 👻 **Puppets**: Los contactos de toDus aparecen como usuarios fantasma en Matrix
- 🔒 **Soporte para cifrado**: Compatible con cifrado E2EE de Matrix (opcional)
- 💾 **Persistencia PostgreSQL**: Guarda sesiones de usuarios, portales y configuración

## 📋 Requisitos

- Python 3.9+
- Un homeserver de Matrix (Synapse, Dendrite, etc.)
- Una cuenta en toDus
- PostgreSQL 12+ (para persistencia de datos)
- Acceso a internet (para conectar con la red de toDus)

## 🚀 Instalación

### 1. Clonar el repositorio

```bash
cd /workspace
git clone <tu-repositorio> todus-bridge
cd todus-bridge
```

### 2. Instalar dependencias

```bash
pip install -r requirements.txt
```

### 3. Configurar PostgreSQL

Crea una base de datos y usuario para el puente:

```bash
sudo -u postgres psql
CREATE DATABASE todus_bridge;
CREATE USER todus WITH PASSWORD 'tu_password_seguro';
GRANT ALL PRIVILEGES ON DATABASE todus_bridge TO todus;
\q
```

### 4. Configurar el puente

Copia el archivo de ejemplo y edítalo:

```bash
cp todus_bridge/config.example.yaml config.yaml
nano config.yaml
```

Edita las siguientes secciones:

```yaml
homeserver:
  address: "http://tu-homeserver:8008"
  domain: "tudominio.com"

appservice:
  address: "http://localhost:29318"
  token: "genera_un_token_seguro"
  bot_username: "todusbot"

database:
  host: "localhost"
  port: 5432
  name: "todus_bridge"
  user: "todus"
  password: "tu_password_seguro"
```

### 5. Registrar el Appservice en Matrix

Genera el archivo de registro del appservice:

```bash
python -m todus_bridge.bridge --generate-registration
```

Luego copia el archivo generado (`registration.yaml`) a tu homeserver de Matrix y reinícialo.

## 🎮 Uso

### Iniciar el puente

```bash
python -m todus_bridge.bridge -c config.yaml
```

### Comandos disponibles

Invita al bot `@todusbot:tudominio.com` a una sala y usa los siguientes comandos:

#### 📋 Gestión de Sesión

| Comando | Descripción |
|---------|-------------|
| `!todus help` | Muestra ayuda sobre los comandos |
| `!todus login <teléfono> <contraseña>` | Inicia sesión en toDus |
| `!todus logout` | Cierra sesión en toDus |
| `!todus status` | Muestra el estado de tu conexión |
| `!todus me` | Muestra tu información de perfil en toDus |
| `!todus ping` | Verifica si el puente está respondiendo |

#### 💬 Chats y Contactos

| Comando | Descripción |
|---------|-------------|
| `!todus chat <número>` | Crea un chat con un contacto de toDus |
| `!todus search <nombre>` | Busca contactos en toDus |
| `!todus contacts` | Lista todos tus contactos de toDus |
| `!todus list` | Lista todos tus chats/portales activos |

#### 👥 Grupos y Canales

| Comando | Descripción |
|---------|-------------|
| `!todus groups` | Lista tus grupos de toDus |
| `!todus channels` | Lista tus canales de toDus |

### Ejemplos

```bash
# Iniciar sesión en toDus
!todus login 51234567 mi_contraseña

# Crear un chat con un contacto
!todus chat 51987654

# Buscar contactos
!todus search Juan Pérez

# Verificar estado
!todus status

# Listar grupos
!todus groups

# Listar canales
!todus channels

# Ver mis contactos
!todus contacts

# Ver perfil personal
!todus me
```

## 🏗️ Arquitectura

El puente sigue la arquitectura de mautrix-python:

```
todus_bridge/
├── __init__.py          # Paquete principal
├── bridge.py            # Clase principal del puente
├── config.py            # Gestión de configuración
├── db.py                # Base de datos PostgreSQL
├── user.py              # Usuarios de Matrix autenticados
├── puppet.py            # Usuarios fantasma de toDus en Matrix
├── portal.py            # Salas que conectan Matrix con toDus
├── matrix_handler.py    # Manejador de eventos Matrix
└── config.example.yaml  # Configuración de ejemplo
```

### Flujo de mensajes

```
Matrix User → Bot Command → Matrix Handler → User → toDus Client → toDus Network
                                                              ↓
Matrix User ← Portal ← Puppet ← toDus Client ← toDus Network ← toDus Contact
```

### Base de datos

El puente utiliza PostgreSQL para persistir:

- **users**: Sesiones de usuarios de Matrix autenticados en toDus
- **portals**: Salas Matrix conectadas a chats/grupos/canales de toDus
- **puppets**: Información de usuarios fantasma (contactos de toDus)
- **portal_rooms**: Relación muchos-a-muchos entre portales y salas

## 🔧 Desarrollo

### Estructura del proyecto

- **Bridge**: Coordina todos los componentes
- **Config**: Carga y valida la configuración YAML
- **Database**: Maneja la persistencia en PostgreSQL
- **User**: Representa usuarios de Matrix con sesión en toDus
- **Puppet**: Representa contactos de toDus como usuarios fantasma en Matrix
- **Portal**: Salas Matrix sincronizadas con chats de toDus
- **MatrixHandler**: Procesa eventos y comandos de Matrix

### Añadir nuevos comandos

1. Añade el handler en `matrix_handler.py`:

```python
async def cmd_micomando(self, user: User, args: list[str]) -> None:
    """Descripción del comando."""
    # Tu lógica aquí
    pass
```

2. Regístralo en `__init__`:

```python
self.commands["micomando"] = self.cmd_micomando
```

## 🔐 Seguridad

- Las contraseñas de toDus se guardan cifradas en la base de datos
- Usa tokens seguros para el appservice
- Configura correctamente los permisos de PostgreSQL
- Habilita cifrado E2EE en Matrix para mayor seguridad

## 📝 Licencia

Este proyecto está licenciado bajo MIT. Consulta el archivo `LICENSE` para más detalles.

## 🙏 Agradecimientos

- [mautrix/python](https://github.com/mautrix/python) - Framework base para puentes Matrix
- [nyxthor-dev/toDus-SDK](https://github.com/nyxthor-dev/toDus-SDK) - SDK oficial de toDus para Python

## 📞 Soporte

Para reportar bugs o solicitar características, abre un issue en el repositorio.

---

**Nota**: Este puente es un proyecto comunitario y no está afiliado oficialmente con toDus o Element.
