"""
Usuario del puente toDus-Matrix

Este módulo representa a un usuario de Matrix que usa el puente,
manejando su sesión de toDus y estado de autenticación.
"""

from __future__ import annotations

from typing import Any
import asyncio
import logging

from mautrix.bridge import BaseUser
from mautrix.types import UserID
from todus import ToDusClient2

from .config import Config
from .puppet import Puppet

log = logging.getLogger("todus_bridge.user")


class User(BaseUser):
    """
    Representa un usuario de Matrix que usa el puente toDus.
    
    Cada usuario puede autenticarse con toDus usando su número de teléfono
    y contraseña, lo que permite interactuar con contactos de toDus desde Matrix.
    """

    def __init__(
        self,
        mxid: UserID,
        bridge: "ToDusBridge",
        config: Config,
    ) -> None:
        super().__init__()
        self.mxid = mxid
        self.bridge = bridge
        self.config = config
        
        # Credenciales de toDus
        self.todus_phone: str | None = None
        self.todus_token: str | None = None
        
        # Cliente de toDus
        self.todus_client: ToDusClient2 | None = None
        
        # Estado
        self.is_logged_in_value: bool = False
        self._login_lock = asyncio.Lock()
        
        # Cache de portales
        self.portals: dict[str, "Portal"] = {}
        
        log.debug(f"Usuario {mxid} inicializado")

    @property
    def is_logged_in(self) -> bool:
        """Verifica si el usuario ha iniciado sesión en toDus."""
        return self.is_logged_in_value and self.todus_client is not None

    async def login_to_todus(self, phone: str, password: str) -> bool:
        """
        Inicia sesión en toDus con las credenciales proporcionadas.
        
        Args:
            phone: Número de teléfono de toDus
            password: Contraseña de toDus
            
        Returns:
            True si el login fue exitoso, False en caso contrario
        """
        async with self._login_lock:
            try:
                log.info(f"Iniciando sesión en toDus para {self.mxid}")
                
                # Crear cliente de toDus
                self.todus_client = ToDusClient2(
                    phone_number=phone,
                    password=password,
                    proxy=self.config.todus_proxy,
                    verify_ssl=self.config.todus_verify_ssl,
                )
                
                # Realizar login
                await self.todus_client.login()
                
                self.todus_phone = phone
                self.is_logged_in_value = True
                
                log.info(f"Login exitoso para {self.mxid} con teléfono {phone}")
                
                # Actualizar puppet
                await self.update_puppet()
                
                return True
                
            except Exception as e:
                log.error(f"Error al iniciar sesión en toDus: {e}")
                self.todus_client = None
                self.is_logged_in_value = False
                return False

    async def logout_from_todus(self) -> None:
        """Cierra la sesión de toDus del usuario."""
        async with self._login_lock:
            if self.todus_client:
                try:
                    await self.todus_client.logout()
                except Exception as e:
                    log.warning(f"Error al cerrar sesión: {e}")
                
                self.todus_client = None
                self.todus_phone = None
                self.is_logged_in_value = False
                
                log.info(f"Sesión cerrada para {self.mxid}")

    async def get_puppet(self) -> Puppet | None:
        """
        Obtiene el puppet que representa a este usuario en toDus.
        
        Returns:
            El puppet del usuario o None si no está logueado
        """
        if not self.is_logged_in or not self.todus_phone:
            return None
        
        return await Puppet.get_by_todus_phone(self.todus_phone)

    async def update_puppet(self) -> None:
        """Actualiza la información del puppet del usuario."""
        puppet = await self.get_puppet()
        if puppet and self.todus_client:
            try:
                # Obtener perfil de toDus
                profile = await self.todus_client.get_profile()
                await puppet.update_info(profile)
            except Exception as e:
                log.warning(f"Error al actualizar puppet: {e}")

    async def send_message_to_todus(self, jid: str, message: str) -> bool:
        """
        Envía un mensaje a través de toDus.
        
        Args:
            jid: JID del destinatario en toDus
            message: Contenido del mensaje
            
        Returns:
            True si el mensaje se envió correctamente
        """
        if not self.is_logged_in or not self.todus_client:
            log.error(f"Usuario {self.mxid} no está logueado en toDus")
            return False
        
        try:
            await self.todus_client.send_message(jid, message)
            log.debug(f"Mensaje enviado a {jid} por {self.mxid}")
            return True
        except Exception as e:
            log.error(f"Error al enviar mensaje a {jid}: {e}")
            return False

    async def send_file_to_todus(
        self,
        jid: str,
        file_path: str,
        file_type: str = "auto",
        caption: str | None = None,
    ) -> bool:
        """
        Envía un archivo a través de toDus.
        
        Args:
            jid: JID del destinatario
            file_path: Ruta del archivo local
            file_type: Tipo de archivo (image, video, audio, document, auto)
            caption: Pie de foto opcional
            
        Returns:
            True si el archivo se envió correctamente
        """
        if not self.is_logged_in or not self.todus_client:
            return False
        
        try:
            from todus.types import FileType
            
            file_type_map = {
                "image": FileType.IMAGE,
                "video": FileType.VIDEO,
                "audio": FileType.AUDIO,
                "document": FileType.DOCUMENT,
                "auto": FileType.AUTO,
            }
            
            ft = file_type_map.get(file_type.lower(), FileType.AUTO)
            await self.todus_client.send_file(jid, file_path, ft, caption)
            log.debug(f"Archivo enviado a {jid} por {self.mxid}")
            return True
        except Exception as e:
            log.error(f"Error al enviar archivo a {jid}: {e}")
            return False

    def __repr__(self) -> str:
        return f"<User mxid={self.mxid} logged_in={self.is_logged_in}>"

# Import deferred to avoid circular imports
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .bridge import ToDusBridge
    from .portal import Portal
