"""
Portal del puente toDus-Matrix

Este módulo representa una sala de chat que conecta Matrix con toDus,
manejando la sincronización bidireccional de mensajes.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from mautrix.appservice import IntentAPI
from mautrix.bridge import BasePortal
from mautrix.types import (
    EventID,
    EventType,
    MessageEventContent,
    MessageType,
    RoomID,
    TextMessageEventContent,
    UserID,
)
from mautrix.util.simple_lock import SimpleLock

from .user import User
from .puppet import Puppet

if TYPE_CHECKING:
    from .bridge import ToDusBridge

log = logging.getLogger("todus_bridge.portal")


class Portal(BasePortal):
    """
    Representa una sala de Matrix conectada a un chat de toDus.
    
    Los portales manejan la sincronización bidireccional de mensajes
    entre Matrix y toDus, convirtiendo formatos y manejando eventos.
    """

    # Cache global de portales
    by_room_id: dict[RoomID, "Portal"] = {}
    by_todus_jid: dict[str, "Portal"] = {}
    
    _cache_lock = asyncio.Lock()

    def __init__(
        self,
        room_id: RoomID | None,
        todus_jid: str,
        bridge: ToDusBridge,
        is_direct: bool = True,
    ) -> None:
        super().__init__()
        self.room_id = room_id
        self.todus_jid = todus_jid
        self.bridge = bridge
        self.is_direct = is_direct
        
        # Información de la sala
        self.name: str | None = None
        self.topic: str | None = None
        self.avatar_url: str | None = None
        
        # Estado
        self.backfill_lock = SimpleLock()
        self._sync_lock = asyncio.Lock()
        
        # Usuarios en la sala
        self._users_in_room: set[UserID] = set()

    @classmethod
    async def get_by_room_id(cls, room_id: RoomID) -> "Portal" | None:
        """Obtiene un portal por su ID de sala Matrix."""
        async with cls._cache_lock:
            return cls.by_room_id.get(room_id)

    @classmethod
    async def get_by_todus_jid(
        cls,
        jid: str,
        create: bool = True,
    ) -> "Portal" | None:
        """
        Obtiene o crea un portal para un JID de toDus.
        
        Args:
            jid: JID de toDus
            create: Si es True, crea el portal si no existe
            
        Returns:
            El portal correspondiente
        """
        async with cls._cache_lock:
            if jid in cls.by_todus_jid:
                return cls.by_todus_jid[jid]
            
            if not create:
                return None
            
            bridge = ToDusBridge.instance
            if not bridge:
                return None
            
            portal = cls(None, jid, bridge)
            cls.by_todus_jid[jid] = portal
            
            log.debug(f"Portal creado para JID {jid}")
            return portal

    async def save(self) -> None:
        """Guarda el estado del portal en la base de datos."""
        # TODO: Implementar persistencia en base de datos
        pass

    async def get_dm_puppet(self) -> Puppet | None:
        """
        Obtiene el puppet que representa el otro extremo del chat directo.
        
        Returns:
            El puppet correspondiente al contacto de toDus
        """
        if not self.is_direct:
            return None
        
        # Extraer teléfono del JID
        phone = self.todus_jid.split("@")[0].split(".")[0]
        return await Puppet.get_by_todus_phone(phone)

    async def create_matrix_room(self, user: User) -> RoomID | None:
        """
        Crea una sala de Matrix para este portal.
        
        Args:
            user: Usuario que solicita la creación
            
        Returns:
            RoomID de la sala creada
        """
        if self.room_id:
            return self.room_id
        
        async with self._sync_lock:
            if self.room_id:
                return self.room_id
            
            try:
                # Obtener puppet para el contacto
                puppet = await self.get_dm_puppet()
                
                # Determinar nombre de la sala
                if puppet and puppet.displayname:
                    room_name = puppet.displayname
                else:
                    room_name = f"ToDus: {self.todus_jid}"
                
                # Crear sala usando el intent del usuario
                intent = self.bridge.az.intent.user(user.mxid)
                
                # Configurar invitación directa si es DM
                if self.is_direct and puppet:
                    await puppet.ensure_registered()
                    invitees = [puppet.mxid]
                else:
                    invitees = []
                
                # Crear sala
                self.room_id = await intent.create_room(
                    name=room_name,
                    invitees=invitees,
                    is_direct=self.is_direct,
                )
                
                # Registrar en caché
                self.by_room_id[self.room_id] = self
                
                # Establecer tema si hay
                if self.topic:
                    await intent.set_room_topic(self.room_id, self.topic)
                
                log.info(f"Sala Matrix creada: {self.room_id} para {self.todus_jid}")
                await self.save()
                
                return self.room_id
                
            except Exception as e:
                log.error(f"Error al crear sala Matrix: {e}")
                return None

    async def handle_matrix_message(
        self,
        sender: User,
        event_id: EventID,
        content: MessageEventContent,
    ) -> None:
        """
        Maneja un mensaje enviado desde Matrix hacia toDus.
        
        Args:
            sender: Usuario que envió el mensaje
            event_id: ID del evento Matrix
            content: Contenido del mensaje
        """
        if not sender.is_logged_in or not sender.todus_client:
            log.warning(f"Usuario {sender.mxid} no está logueado en toDus")
            return
        
        async with self._sync_lock:
            try:
                # Extraer texto del mensaje
                text = content.body
                
                # Enviar a toDus
                success = await sender.send_message_to_todus(self.todus_jid, text)
                
                if success:
                    log.debug(f"Mensaje enviado a toDus {self.todus_jid}")
                else:
                    # Notificar error en la sala
                    await self._send_notice(f"Error al enviar mensaje a toDus")
                    
            except Exception as e:
                log.error(f"Error al manejar mensaje Matrix: {e}")
                await self._send_notice(f"Error: {str(e)}")

    async def handle_todus_message(
        self,
        sender_jid: str,
        message: str,
        timestamp: int,
    ) -> None:
        """
        Maneja un mensaje recibido desde toDus hacia Matrix.
        
        Args:
            sender_jid: JID del remitente en toDus
            message: Contenido del mensaje
            timestamp: Timestamp del mensaje
        """
        if not self.room_id:
            log.warning(f"Portal sin sala Matrix para {self.todus_jid}")
            return
        
        async with self._sync_lock:
            try:
                # Obtener puppet para el remitente
                puppet = await Puppet.get_by_todus_phone(
                    sender_jid.split("@")[0].split(".")[0]
                )
                
                if not puppet:
                    log.error(f"No se pudo obtener puppet para {sender_jid}")
                    return
                
                # Asegurar que el puppet esté registrado
                await puppet.ensure_registered()
                
                # Crear contenido del mensaje
                content = TextMessageEventContent(
                    msgtype=MessageType.TEXT,
                    body=message,
                )
                
                # Enviar mensaje como el puppet
                if puppet.intent:
                    await puppet.intent.send_message(self.room_id, content)
                    log.debug(f"Mensaje de {sender_jid} enviado a {self.room_id}")
                    
            except Exception as e:
                log.error(f"Error al manejar mensaje toDus: {e}")

    async def _send_notice(self, text: str) -> None:
        """Envía una notificación a la sala Matrix."""
        if not self.room_id:
            return
        
        try:
            intent = self.bridge.az.intent
            content = TextMessageEventContent(
                msgtype=MessageType.NOTICE,
                body=text,
            )
            await intent.send_message(self.room_id, content)
        except Exception as e:
            log.warning(f"Error al enviar notificación: {e}")

    async def handle_matrix_invite(
        self,
        invited_by: User,
        event_id: EventID | None = None,
    ) -> None:
        """
        Maneja una invitación a una sala Matrix.
        
        Args:
            invited_by: Usuario que invitó
            event_id: ID del evento de invitación
        """
        if not self.room_id:
            # Unirse a la sala
            try:
                await self.bridge.az.intent.join_room(self.room_id)
                self.by_room_id[self.room_id] = self
                log.info(f"Bridge se unió a la sala {self.room_id}")
            except Exception as e:
                log.error(f"Error al unirse a la sala: {e}")

    async def cleanup(self) -> None:
        """Limpia y elimina el portal."""
        if self.room_id:
            del self.by_room_id[self.room_id]
        if self.todus_jid:
            del self.by_todus_jid[self.todus_jid]
        
        log.info(f"Portal limpiado: {self.todus_jid}")

    def __repr__(self) -> str:
        return f"<Portal room={self.room_id} jid={self.todus_jid}>"
