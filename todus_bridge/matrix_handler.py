"""
Manejador de eventos Matrix del puente toDus-Matrix

Este módulo maneja los eventos entrantes desde Matrix, incluyendo
mensajes, invitaciones y comandos del bot.
"""

from __future__ import annotations

import logging

from mautrix.bridge import BaseMatrixHandler
from mautrix.types import (
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
from .commands import CommandEvent, CommandProcessor
from .user import User
from .portal import Portal

log = logging.getLogger("todus_bridge.matrix_handler")


class MatrixHandler(BaseMatrixHandler):
    """
    Manejador de eventos Matrix para el puente toDus.

    Procesa mensajes entrantes, comandos del bot, y gestiona
    la creación de portales y salas usando el sistema de comandos
    de mautrix.
    """

    def __init__(self, bridge: ToDusBridge) -> None:
        super().__init__(bridge)
        self.bridge = bridge
        
        # Usar el procesador de comandos del módulo commands
        self.commands = CommandProcessor(bridge)
        self.command_prefix = "!todus"

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
            await self.handle_command(user, room_id, event_id, content)
            return

        # Obtener portal
        portal = await Portal.get_by_room_id(room_id)
        if portal:
            # Enviar mensaje a toDus
            await portal.handle_matrix_message(user, event_id, content)

    async def handle_command(
        self,
        user: User,
        room_id: RoomID,
        event_id: EventID,
        content: MessageEventContent,
    ) -> None:
        """
        Procesa un comando del bot usando CommandProcessor.

        Args:
            user: Usuario que ejecutó el comando
            room_id: Sala donde se ejecutó
            event_id: ID del evento
            content: Contenido del mensaje
        """
        # Parsear comando
        command_text = content.body
        parts = command_text[len(self.command_prefix):].strip().split()
        
        if not parts:
            await self._send_notice(
                room_id,
                f"Comando vacío. Usa `{self.command_prefix} help`",
            )
            return

        cmd = parts[0].lower()
        args = parts[1:]

        log.debug(f"Comando recibido: {cmd} de {user.mxid}")

        # Crear evento de comando
        evt = CommandEvent(
            processor=self.commands,
            room_id=room_id,
            event_id=event_id,
            sender=user,
            command=cmd,
            args=args,
            content=content,
            portal=None,
            is_management=True,
            has_bridge_bot=True,
        )

        # Ejecutar comando
        await self.commands.handle_command(evt)

    async def get_user(self, mxid: UserID) -> User | None:
        """
        Obtiene o crea un usuario.

        Args:
            mxid: Matrix User ID

        Returns:
            El usuario correspondiente
        """
        return await self.bridge.get_user(mxid)

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
            await self.bridge.az.intent.join_room(room_id)

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
