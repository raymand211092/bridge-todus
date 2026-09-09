"""
Manejador de eventos Matrix del puente toDus-Matrix

Este módulo maneja los eventos entrantes desde Matrix, incluyendo
mensajes, invitaciones y comandos del bot.
"""

from __future__ import annotations

import logging
import re
from typing import Awaitable, Callable

from mautrix.appservice import AppService
from mautrix.bridge import BaseMatrixHandler, BaseUser
from mautrix.types import (
    Event,
    EventID,
    EventType,
    MessageEventContent,
    MessageType,
    RoomID,
    TextMessageEventContent,
    UserID,
)
from mautrix.util import markdown

from .bridge import ToDusBridge
from .user import User
from .portal import Portal
from .db import Database

log = logging.getLogger("todus_bridge.matrix_handler")


class MatrixHandler(BaseMatrixHandler):
    """
    Manejador de eventos Matrix para el puente toDus.
    
    Procesa mensajes entrantes, comandos del bot, y gestiona
    la creación de portales y salas.
    """

    def __init__(self, bridge: ToDusBridge) -> None:
        super().__init__(bridge)
        self.bridge = bridge
        self.command_prefix = "!todus"
        
        # Base de datos
        self.db = Database.get_instance(bridge.config)
        
        # Registro de comandos
        self.commands: dict[str, Callable[[User, list[str]], Awaitable[None]]] = {
            "help": self.cmd_help,
            "login": self.cmd_login,
            "logout": self.cmd_logout,
            "status": self.cmd_status,
            "chat": self.cmd_chat,
            "search": self.cmd_search,
            "list": self.cmd_list,
            "channels": self.cmd_channels,
            "groups": self.cmd_groups,
            "contacts": self.cmd_contacts,
            "me": self.cmd_me,
            "ping": self.cmd_ping,
        }

    async def handle_message(
        self,
        room_id: RoomID,
        event_id: EventID,
        sender: UserID,
        event_type: EventType,
        content: MessageEventContent,
    ) -> None:
        """
        Maneja un mensaje recibido en Matrix.
        
        Args:
            room_id: ID de la sala
            event_id: ID del evento
            sender: Usuario que envió el mensaje
            event_type: Tipo de evento
            content: Contenido del mensaje
        """
        # Obtener usuario
        user = await self.get_user(sender)
        if not user:
            return
        
        # Verificar si es un comando
        if content.msgtype == MessageType.TEXT and content.body.startswith(self.command_prefix):
            await self.handle_command(user, room_id, event_id, content.body)
            return
        
        # Obtener portal
        portal = await Portal.get_by_room_id(room_id)
        if portal:
            # Enviar mensaje a toDus
            await portal.handle_matrix_message(user, event_id, content)
        else:
            # Si no hay portal, verificar si es una sala de gestión
            if await self._is_management_room(room_id):
                await self._send_notice(
                    room_id,
                    f"Usa `{self.command_prefix} help` para ver los comandos disponibles.",
                )

    async def handle_command(
        self,
        user: User,
        room_id: RoomID,
        event_id: EventID,
        command_text: str,
    ) -> None:
        """
        Procesa un comando del bot.
        
        Args:
            user: Usuario que ejecutó el comando
            room_id: Sala donde se ejecutó
            event_id: ID del evento
            command_text: Texto completo del comando
        """
        # Parsear comando
        parts = command_text[len(self.command_prefix):].strip().split()
        if not parts:
            await self._send_notice(room_id, "Comando vacío. Usa `!todus help`")
            return
        
        cmd = parts[0].lower()
        args = parts[1:]
        
        log.debug(f"Comando recibido: {cmd} de {user.mxid}")
        
        # Ejecutar comando
        handler = self.commands.get(cmd)
        if handler:
            try:
                await handler(user, args)
                await self._send_notice(room_id, f"✅ Comando `{cmd}` ejecutado")
            except Exception as e:
                log.error(f"Error ejecutando comando {cmd}: {e}")
                await self._send_notice(room_id, f"❌ Error: {str(e)}")
        else:
            await self._send_notice(
                room_id,
                f"Comando desconocido: `{cmd}`. Usa `{self.command_prefix} help`",
            )

    async def cmd_help(self, user: User, args: list[str]) -> None:
        """Muestra ayuda sobre los comandos disponibles."""
        help_text = """
**🌉 Puente toDus-Matrix - Comandos Disponibles**

**📋 Gestión de Sesión:**
• `!todus help` - Muestra esta ayuda
• `!todus login <teléfono> <contraseña>` - Inicia sesión en toDus
• `!todus logout` - Cierra sesión en toDus
• `!todus status` - Muestra el estado de tu conexión
• `!todus me` - Muestra tu información de perfil en toDus
• `!todus ping` - Verifica si el puente está respondiendo

**💬 Chats y Contactos:**
• `!todus chat <número>` - Crea un chat con un contacto de toDus
• `!todus search <nombre>` - Busca contactos en toDus
• `!todus contacts` - Lista todos tus contactos de toDus
• `!todus list` - Lista todos tus chats/portales activos

**👥 Grupos y Canales:**
• `!todus groups` - Lista tus grupos de toDus
• `!todus channels` - Lista tus canales de toDus

**Ejemplos de uso:**
- `!todus login 51234567 mi_password`
- `!todus chat 51987654`
- `!todus search Juan Pérez`
- `!todus list all`
""".strip()
        
        await self._send_formatted_help(user, help_text)

    async def _send_formatted_help(self, user: User, text: str) -> None:
        """Envía ayuda formateada al usuario en su sala de chat con el bot."""
        # Obtener o crear sala de gestión para el usuario
        room_id = await self._get_management_room(user.mxid)
        if room_id:
            content = TextMessageEventContent(
                msgtype=MessageType.TEXT,
                body=text,
                format="org.matrix.custom.html",
                formatted_body=markdown.render(text),
            )
            await self.bridge.az.intent.send_message(room_id, content)
        else:
            log.warning(f"No se pudo obtener sala de gestión para {user.mxid}")

    async def cmd_login(self, user: User, args: list[str]) -> None:
        """
        Inicia sesión en toDus.
        
        Args:
            user: Usuario
            args: [teléfono, contraseña]
        """
        if len(args) < 2:
            raise ValueError("Uso: `!todus login <teléfono> <contraseña>`")
        
        phone = args[0]
        password = args[1]
        
        success = await user.login_to_todus(phone, password)
        
        if not success:
            raise Exception("No se pudo iniciar sesión. Verifica tus credenciales.")
        
        # Guardar sesión en PostgreSQL
        await self.db.save_user_session(user.mxid, phone, user.todus_token)
        
        # Notificar al usuario
        room_id = await self._get_management_room(user.mxid)
        if room_id:
            await self._send_notice(
                room_id,
                f"✅ **Inicio de sesión exitoso**\n\n"
                f"Has iniciado sesión en toDus como `{phone}`.\n"
                f"Tu sesión ha sido guardada.\n\n"
                f"Usa `!todus help` para ver los comandos disponibles."
            )

    async def cmd_logout(self, user: User, args: list[str]) -> None:
        """Cierra sesión en toDus."""
        await user.logout_from_todus()
        
        # Eliminar sesión de la base de datos
        await self.db.delete_user_session(user.mxid)
        
        # Notificar al usuario
        room_id = await self._get_management_room(user.mxid)
        if room_id:
            await self._send_notice(room_id, "✅ Sesión cerrada exitosamente.")

    async def cmd_status(self, user: User, args: list[str]) -> None:
        """Muestra el estado de la conexión."""
        room_id = await self._get_management_room(user.mxid)
        if not room_id:
            return
        
        if user.is_logged_in:
            status = (
                f"✅ **Conectado a toDus**\n\n"
                f"Teléfono: `{user.todus_phone}`\n"
                f"Estado: En línea"
            )
        else:
            status = "❌ **No conectado**\n\nUsa `!todus login <teléfono> <contraseña>` para iniciar sesión."
        
        await self._send_notice(room_id, status)

    async def cmd_ping(self, user: User, args: list[str]) -> None:
        """Verifica si el puente está respondiendo."""
        room_id = await self._get_management_room(user.mxid)
        if room_id:
            await self._send_notice(room_id, "🏓 ¡Pong! El puente está funcionando correctamente.")

    async def cmd_me(self, user: User, args: list[str]) -> None:
        """Muestra la información de perfil del usuario en toDus."""
        if not user.is_logged_in:
            raise Exception("Debes iniciar sesión primero con `!todus login`")
        
        if not user.todus_client:
            raise Exception("Cliente de toDus no disponible")
        
        # Obtener perfil de toDus
        profile = await user.todus_client.get_profile()
        
        room_id = await self._get_management_room(user.mxid)
        if room_id:
            profile_text = (
                f"👤 **Tu perfil en toDus**\n\n"
                f"Nombre: {profile.get('name', 'N/A')}\n"
                f"Teléfono: `{user.todus_phone}`\n"
                f"Estado: {profile.get('about', 'Sin estado')}"
            )
            await self._send_notice(room_id, profile_text)

    async def cmd_chat(self, user: User, args: list[str]) -> None:
        """
        Crea un chat con un contacto de toDus.
        
        Args:
            user: Usuario
            args: [número de teléfono]
        """
        if not user.is_logged_in:
            raise Exception("Debes iniciar sesión primero con `!todus login`")
        
        if len(args) < 1:
            raise ValueError("Uso: `!todus chat <número>`")
        
        phone = args[0]
        
        # Construir JID
        from todus.util import build_jid
        jid = build_jid(phone)
        
        # Obtener o crear portal
        portal = await Portal.get_by_todus_jid(jid)
        if not portal:
            raise Exception("No se pudo crear el portal")
        
        # Crear sala Matrix
        room_id = await portal.create_matrix_room(user)
        if not room_id:
            raise Exception("No se pudo crear la sala Matrix")
        
        # Guardar en base de datos
        await self.db.add_portal_room(jid, room_id, user.mxid)
        
        # Notificar al usuario
        management_room = await self._get_management_room(user.mxid)
        if management_room:
            await self._send_notice(
                management_room,
                f"✅ **Chat creado**\n\n"
                f"Se ha creado un chat con `{phone}`.\n"
                f"Sala: {room_id}"
            )

    async def cmd_search(self, user: User, args: list[str]) -> None:
        """
        Busca contactos en toDus.
        
        Args:
            user: Usuario
            args: [término de búsqueda]
        """
        if not user.is_logged_in:
            raise Exception("Debes iniciar sesión primero")
        
        if len(args) < 1:
            raise ValueError("Uso: `!todus search <nombre>`")
        
        query = " ".join(args)
        
        if not user.todus_client:
            raise Exception("Cliente de toDus no disponible")
        
        # Buscar contactos usando la API de toDus
        results = await user.todus_client.search_contacts(query)
        
        room_id = await self._get_management_room(user.mxid)
        if not room_id:
            return
        
        if not results:
            await self._send_notice(room_id, f"🔍 No se encontraron contactos para '{query}'")
            return
        
        # Formatear resultados
        text = f"🔍 **Resultados para '{query}':**\n\n"
        for contact in results[:10]:  # Limitar a 10 resultados
            name = contact.get('name', 'Desconocido')
            phone = contact.get('phone', 'N/A')
            text += f"• **{name}** - `{phone}`\n"
        
        if len(results) > 10:
            text += f"\n_y más {len(results) - 10} resultados..._"
        
        await self._send_notice(room_id, text)

    async def cmd_list(self, user: User, args: list[str]) -> None:
        """Lista todos los chats/portales activos del usuario."""
        if not user.is_logged_in:
            raise Exception("Debes iniciar sesión primero")
        
        room_id = await self._get_management_room(user.mxid)
        if not room_id:
            return
        
        # Obtener tipo de filtro
        filter_type = args[0] if args else None
        
        # Obtener portales desde la base de datos
        portals = await self.db.get_all_portals_for_user(user.mxid, filter_type)
        
        if not portals:
            await self._send_notice(room_id, "📋 No tienes chats activos.")
            return
        
        text = "📋 **Tus chats activos:**\n\n"
        for portal in portals:
            jid = portal.get('todus_jid', 'N/A')
            room = portal.get('room_id', 'N/A')
            ptype = portal.get('portal_type', 'direct')
            name = portal.get('name', 'Sin nombre')
            
            icon = "💬" if ptype == 'direct' else "👥" if ptype == 'group' else "📢"
            text += f"{icon} **{name}** - `{jid}`\n"
            text += f"   Sala: {room}\n\n"
        
        await self._send_notice(room_id, text)

    async def cmd_contacts(self, user: User, args: list[str]) -> None:
        """Lista todos los contactos del usuario en toDus."""
        if not user.is_logged_in:
            raise Exception("Debes iniciar sesión primero")
        
        if not user.todus_client:
            raise Exception("Cliente de toDus no disponible")
        
        room_id = await self._get_management_room(user.mxid)
        if not room_id:
            return
        
        # Obtener lista de contactos
        contacts = await user.todus_client.get_contacts()
        
        if not contacts:
            await self._send_notice(room_id, "📞 No tienes contactos.")
            return
        
        text = f"📞 **Tus contactos ({len(contacts)}):**\n\n"
        for contact in contacts[:20]:  # Limitar a 20
            name = contact.get('name', 'Desconocido')
            phone = contact.get('phone', 'N/A')
            text += f"• **{name}** - `{phone}`\n"
        
        if len(contacts) > 20:
            text += f"\n_y más {len(contacts) - 20} contactos..._"
        
        await self._send_notice(room_id, text)

    async def cmd_groups(self, user: User, args: list[str]) -> None:
        """Lista los grupos del usuario en toDus."""
        if not user.is_logged_in:
            raise Exception("Debes iniciar sesión primero")
        
        if not user.todus_client:
            raise Exception("Cliente de toDus no disponible")
        
        room_id = await self._get_management_room(user.mxid)
        if not room_id:
            return
        
        # Obtener lista de grupos
        groups = await user.todus_client.get_groups()
        
        if not groups:
            await self._send_notice(room_id, "👥 No estás en ningún grupo.")
            return
        
        text = f"👥 **Tus grupos ({len(groups)}):**\n\n"
        for group in groups:
            name = group.get('name', 'Sin nombre')
            jid = group.get('jid', 'N/A')
            participants = group.get('participants_count', '?')
            text += f"• **{name}** - `{jid}`\n"
            text += f"   Participantes: {participants}\n\n"
        
        await self._send_notice(room_id, text)

    async def cmd_channels(self, user: User, args: list[str]) -> None:
        """Lista los canales del usuario en toDus."""
        if not user.is_logged_in:
            raise Exception("Debes iniciar sesión primero")
        
        if not user.todus_client:
            raise Exception("Cliente de toDus no disponible")
        
        room_id = await self._get_management_room(user.mxid)
        if not room_id:
            return
        
        # Obtener lista de canales
        channels = await user.todus_client.get_channels()
        
        if not channels:
            await self._send_notice(room_id, "📢 No sigues ningún canal.")
            return
        
        text = f"📢 **Tus canales ({len(channels)}):**\n\n"
        for channel in channels:
            name = channel.get('name', 'Sin nombre')
            jid = channel.get('jid', 'N/A')
            subscribers = channel.get('subscribers_count', '?')
            text += f"• **{name}** - `{jid}`\n"
            text += f"   Suscriptores: {subscribers}\n\n"
        
        await self._send_notice(room_id, text)

    async def _is_management_room(self, room_id: RoomID) -> bool:
        """Verifica si una sala es una sala de gestión."""
        # Por ahora, todas las salas DM con el bot son salas de gestión
        # En el futuro, esto puede verificarse contra la base de datos
        return True

    async def _get_management_room(self, user_mxid: UserID) -> RoomID | None:
        """
        Obtiene o crea una sala de gestión para un usuario.
        
        Esta es la sala donde el usuario interactúa con el bot para
        ejecutar comandos como login, logout, help, etc.
        
        Args:
            user_mxid: MXID del usuario
            
        Returns:
            RoomID de la sala de gestión o None si falla
        """
        # Buscar sala existente en caché o base de datos
        # Por simplicidad, creamos una nueva sala si no existe
        try:
            intent = self.bridge.az.intent
            
            # Crear sala DM para gestión
            room_id = await intent.create_room(
                name="toDus Bridge - Gestión",
                invitees=[user_mxid],
                is_direct=True,
                initial_state=[
                    {
                        "type": "m.room.join_rules",
                        "content": {"join_rule": "invite"},
                    }
                ],
            )
            
            log.info(f"Sala de gestión creada: {room_id} para {user_mxid}")
            return room_id
            
        except Exception as e:
            log.error(f"Error al crear sala de gestión: {e}")
            return None

    async def _send_notice(self, room_id: RoomID, text: str) -> None:
        """Envía una notificación a una sala."""
        try:
            intent = self.bridge.az.intent
            content = TextMessageEventContent(
                msgtype=MessageType.NOTICE,
                body=text,
                format="org.matrix.custom.html",
                formatted_body=markdown.render(text),
            )
            await intent.send_message(room_id, content)
        except Exception as e:
            log.warning(f"Error al enviar notificación: {e}")

    async def get_user(self, mxid: UserID) -> User | None:
        """
        Obtiene o crea un usuario.
        
        Args:
            mxid: Matrix User ID
            
        Returns:
            El usuario correspondiente
        """
        # TODO: Implementar cache de usuarios
        return User(mxid, self.bridge, self.bridge.config)

    async def handle_invite(
        self,
        room_id: RoomID,
        event_id: EventID,
        inviter: UserID,
        sender: UserID,
    ) -> None:
        """
        Maneja una invitación a una sala.
        
        Args:
            room_id: ID de la sala
            event_id: ID del evento
            inviter: Usuario que invitó
            sender: Usuario invitado (el bot)
        """
        log.info(f"Invitación recibida en {room_id} de {inviter}")
        
        # Unirse a la sala
        try:
            await self.az.intent.join_room(room_id)
            
            # Obtener usuario
            user = await self.get_user(inviter)
            
            # Obtener o crear portal
            portal = await Portal.get_by_room_id(room_id)
            if not portal:
                # Crear nuevo portal para esta sala
                portal = await Portal.get_by_todus_jid(f"unknown_{room_id}")
                if portal:
                    portal.room_id = room_id
                    portal.by_room_id[room_id] = portal
            
            # Manejar invitación
            if portal and user:
                await portal.handle_matrix_invite(user, event_id)
                
        except Exception as e:
            log.error(f"Error al manejar invitación: {e}")
