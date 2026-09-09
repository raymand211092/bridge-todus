"""
Puppet del puente toDus-Matrix

Este módulo representa a un usuario de toDus en Matrix (ghost user),
permitiendo que los contactos de toDus aparezcan como usuarios en Matrix.
"""

from __future__ import annotations

import asyncio
import logging

from mautrix.appservice import IntentAPI
from mautrix.bridge import BasePuppet
from mautrix.types import SyncToken, UserID
from todus.util import build_jid

from .config import Config

log = logging.getLogger("todus_bridge.puppet")

# Import deferred to avoid circular imports
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .bridge import ToDusBridge


class Puppet(BasePuppet):
    """
    Representa un usuario de toDus como un ghost en Matrix.
    
    Los puppets permiten que los contactos de toDus envíen y reciban
    mensajes en salas de Matrix como si fueran usuarios nativos.
    """

    # Cache global de puppets
    by_todus_phone: dict[str, "Puppet"] = {}
    by_mxid: dict[UserID, "Puppet"] = {}
    
    _cache_lock = asyncio.Lock()

    def __init__(
        self,
        phone: str,
        bridge: ToDusBridge,
        config: Config,
    ) -> None:
        super().__init__()
        self.phone = phone
        self.bridge = bridge
        self.config = config
        
        # JID de toDus
        self.jid = build_jid(phone)
        
        # Matrix MXID
        self.mxid = self.get_mxid_from_phone(phone)
        
        # Intent API para acciones como este puppet
        self.intent: IntentAPI | None = None
        
        # Información del perfil
        self.displayname: str | None = None
        self.avatar_url: str | None = None
        self.about: str | None = None
        
        # Estado
        self.is_registered: bool = False
        self._update_lock = asyncio.Lock()

    @classmethod
    def get_mxid_from_phone(cls, phone: str) -> UserID:
        """
        Genera un MXID de Matrix a partir de un número de teléfono de toDus.
        
        Args:
            phone: Número de teléfono de toDus
            
        Returns:
            MXID formateado como @todus_<phone>:domain
        """
        from .bridge import ToDusBridge
        domain = ToDusBridge.instance.config.domain if ToDusBridge.instance else "localhost"
        return f"@todus_{phone}:{domain}"

    @classmethod
    async def get_by_todus_phone(cls, phone: str) -> Puppet | None:
        """
        Obtiene o crea un puppet para un número de teléfono dado.
        
        Args:
            phone: Número de teléfono de toDus
            
        Returns:
            El puppet correspondiente
        """
        async with cls._cache_lock:
            if phone in cls.by_todus_phone:
                return cls.by_todus_phone[phone]
            
            bridge = ToDusBridge.instance
            if not bridge:
                return None
            
            config = bridge.config
            puppet = cls(phone, bridge, config)
            
            cls.by_todus_phone[phone] = puppet
            cls.by_mxid[puppet.mxid] = puppet
            
            log.debug(f"Puppet creado para {phone} -> {puppet.mxid}")
            return puppet

    @classmethod
    async def get_by_mxid(cls, mxid: UserID) -> Puppet | None:
        """
        Obtiene un puppet por su MXID.
        
        Args:
            mxid: Matrix User ID
            
        Returns:
            El puppet correspondiente o None
        """
        async with cls._cache_lock:
            return cls.by_mxid.get(mxid)

    async def ensure_registered(self) -> None:
        """Asegura que el puppet esté registrado en Matrix."""
        if self.is_registered:
            return
        
        async with self._update_lock:
            if self.is_registered:
                return
            
            try:
                # Registrar puppet si no existe
                self.intent = self.bridge.az.intent.user(self.mxid)
                
                # Establecer perfil
                if self.displayname:
                    await self.intent.set_display_name(self.displayname)
                
                if self.avatar_url:
                    await self.intent.set_avatar_url(self.avatar_url)
                
                self.is_registered = True
                log.info(f"Puppet {self.mxid} registrado exitosamente")
                
            except Exception as e:
                log.error(f"Error al registrar puppet {self.mxid}: {e}")
                raise

    async def update_info(self, profile_data: dict | None = None) -> None:
        """
        Actualiza la información del perfil del puppet.
        
        Args:
            profile_data: Datos del perfil de toDus (opcional)
        """
        if not profile_data:
            return
        
        async with self._update_lock:
            try:
                # Extraer información del perfil
                new_displayname = profile_data.get("display_name") or profile_data.get("name")
                new_about = profile_data.get("about") or profile_data.get("status")
                new_avatar = profile_data.get("avatar_url") or profile_data.get("photo")
                
                changed = False
                
                if new_displayname and new_displayname != self.displayname:
                    self.displayname = new_displayname
                    changed = True
                
                if new_about and new_about != self.about:
                    self.about = new_about
                    changed = True
                
                if new_avatar and new_avatar != self.avatar_url:
                    self.avatar_url = new_avatar
                    changed = True
                
                # Actualizar en Matrix si hubo cambios
                if changed and self.is_registered and self.intent:
                    if self.displayname:
                        await self.intent.set_display_name(self.displayname)
                    
                    if self.about:
                        # Guardar 'about' en el estado de cuenta
                        await self.intent.set_account_data(
                            "com.todus_bridge.about", {"about": self.about}
                        )
                
                if changed:
                    log.debug(f"Información de puppet {self.mxid} actualizada")
                    
            except Exception as e:
                log.warning(f"Error al actualizar información de puppet {self.mxid}: {e}")

    async def default_puppet(self) -> "Puppet":
        """
        Obtiene el puppet predeterminado (para el propio usuario).
        
        Returns:
            Este mismo puppet
        """
        return self

    async def on_message_received(
        self,
        sender_jid: str,
        message: str,
        timestamp: int,
        room_id: str | None = None,
    ) -> None:
        """
        Maneja un mensaje recibido de toDus.
        
        Args:
            sender_jid: JID del remitente en toDus
            message: Contenido del mensaje
            timestamp: Timestamp del mensaje
            room_id: ID de la sala Matrix (opcional)
        """
        # Extraer teléfono del JID
        sender_phone = sender_jid.split("@")[0].split(".")[0]
        
        # Obtener o crear puppet para el remitente
        puppet = await self.get_by_todus_phone(sender_phone)
        if not puppet:
            log.error(f"No se pudo obtener puppet para {sender_jid}")
            return
        
        # Asegurar que el puppet esté registrado
        await puppet.ensure_registered()
        
        # Enviar mensaje a través del intent del puppet
        if puppet.intent and room_id:
            try:
                content = {
                    "msgtype": "m.text",
                    "body": message,
                }
                await puppet.intent.send_message(room_id, content)
                log.debug(f"Mensaje de {sender_jid} enviado a {room_id}")
            except Exception as e:
                log.error(f"Error al enviar mensaje desde puppet: {e}")

    def __repr__(self) -> str:
        return f"<Puppet phone={self.phone} mxid={self.mxid} registered={self.is_registered}>"
