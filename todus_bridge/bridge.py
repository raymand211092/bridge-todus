"""
Bridge principal del puente toDus-Matrix

Este módulo implementa la clase principal del puente que coordina
todos los componentes: configuración, usuarios, portales y handlers.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any

from mautrix.appservice import AppService, IntentAPI
from mautrix.bridge import Bridge, BaseMatrixHandler
from mautrix.types import UserID

from .config import Config
from .user import User
from .puppet import Puppet
from .portal import Portal
from .matrix_handler import MatrixHandler

log = logging.getLogger("todus_bridge")


class ToDusBridge(Bridge):
    """
    Clase principal del puente toDus-Matrix.
    
    Coordina todos los componentes del puente, incluyendo:
    - Carga de configuración
    - Registro del appservice
    - Manejo de eventos Matrix
    - Conexión con toDus
    - Gestión de usuarios y portales
    """

    # Instancia singleton para acceso global
    instance: "ToDusBridge | None" = None

    def __init__(
        self,
        config_path: str = "config.yaml",
    ) -> None:
        """
        Inicializa el puente toDus-Matrix.
        
        Args:
            config_path: Ruta al archivo de configuración YAML
        """
        super().__init__()
        
        # Cargar configuración
        self.config = Config.load_from_file(config_path)
        
        # Inicializar componentes
        self.az: AppService | None = None
        self.matrix: MatrixHandler | None = None
        
        # Estado
        self._started = False
        self._stop_event = asyncio.Event()
        
        # Registrar instancia global
        ToDusBridge.instance = self
        
        log.info("Puente toDus-Matrix inicializado")

    async def start(self) -> None:
        """
        Inicia el puente.
        
        Este método configura el appservice, registra handlers,
        y comienza a escuchar eventos de Matrix y toDus.
        """
        if self._started:
            log.warning("El puente ya está en ejecución")
            return
        
        log.info("Iniciando puente toDus-Matrix...")
        
        # Validar configuración
        if not self.config.is_valid():
            raise ValueError("Configuración inválida. Verifica el archivo config.yaml")
        
        # Crear appservice
        self.az = AppService(
            id="todus_bridge",
            url=self.config.appservice_address,
            token=self.config.appservice_token,
            bot_config={
                "username": self.config.bot_username,
                "displayname": "toDus Bridge Bot",
            },
        )
        
        # Crear matrix handler
        self.matrix = MatrixHandler(self)
        
        # Registrar handlers
        self._register_handlers()
        
        # Iniciar appservice
        await self.az.start()
        
        self._started = True
        log.info("Puente toDus-Matrix iniciado exitosamente")
        
        # Mantener ejecución
        await self._stop_event.wait()

    async def stop(self) -> None:
        """Detiene el puente."""
        if not self._started:
            return
        
        log.info("Deteniendo puente toDus-Matrix...")
        
        # Limpiar portales
        for portal in list(Portal.by_room_id.values()):
            await portal.cleanup()
        
        # Detener appservice
        if self.az:
            await self.az.stop()
        
        self._started = False
        self._stop_event.set()
        
        log.info("Puente detenido")

    def _register_handlers(self) -> None:
        """Registra los handlers de eventos."""
        if not self.az or not self.matrix:
            return
        
        # Handler de mensajes
        @self.az.event.register(EventType.ROOM_MESSAGE)
        async def on_message(event: Any) -> None:
            if not self.matrix:
                return
            
            content = event.content
            if hasattr(content, "msgtype"):
                await self.matrix.handle_message(
                    room_id=event.room_id,
                    event_id=event.event_id,
                    sender=event.sender,
                    event_type=event.type,
                    content=content,
                )
        
        # Handler de invitaciones
        @self.az.event.register(EventType.ROOM_MEMBER)
        async def on_member(event: Any) -> None:
            if not self.matrix:
                return
            
            content = event.content
            membership = content.get("membership")
            
            if membership == "invite":
                target = content.get("state_key")
                bot_mxid = f"@{self.config.bot_username}:{self.config.domain}"
                
                if target == bot_mxid:
                    await self.matrix.handle_invite(
                        room_id=event.room_id,
                        event_id=event.event_id,
                        inviter=event.sender,
                        sender=target,
                    )

    async def get_user(self, mxid: UserID) -> User | None:
        """
        Obtiene o crea un usuario.
        
        Args:
            mxid: Matrix User ID
            
        Returns:
            El usuario correspondiente
        """
        if not self.matrix:
            return None
        
        return await self.matrix.get_user(mxid)

    async def get_portal_by_jid(self, jid: str) -> Portal | None:
        """
        Obtiene un portal por su JID de toDus.
        
        Args:
            jid: JID de toDus
            
        Returns:
            El portal correspondiente
        """
        return await Portal.get_by_todus_jid(jid)

    async def get_portal_by_room(self, room_id: str) -> Portal | None:
        """
        Obtiene un portal por su ID de sala Matrix.
        
        Args:
            room_id: RoomID de Matrix
            
        Returns:
            El portal correspondiente
        """
        return await Portal.get_by_room_id(room_id)

    @classmethod
    def get_instance(cls) -> "ToDusBridge | None":
        """Obtiene la instancia actual del puente."""
        return cls.instance


def main() -> None:
    """Función principal para ejecutar el puente."""
    import argparse
    
    parser = argparse.ArgumentParser(description="Puente toDus-Matrix")
    parser.add_argument(
        "-c", "--config",
        default="config.yaml",
        help="Ruta al archivo de configuración",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Habilitar logs detallados",
    )
    
    args = parser.parse_args()
    
    # Configurar logging
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    
    # Crear y ejecutar puente
    bridge = ToDusBridge(config_path=args.config)
    
    try:
        asyncio.run(bridge.start())
    except KeyboardInterrupt:
        log.info("Interrupción recibida")
    finally:
        asyncio.run(bridge.stop())


if __name__ == "__main__":
    main()
