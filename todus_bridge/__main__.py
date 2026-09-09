"""
Punto de entrada principal del puente toDus-Matrix

Este módulo implementa la clase ToDusBridge que extiende la clase Bridge
de mautrix, siguiendo el patrón de mautrix-telegram.
"""

from __future__ import annotations

from typing import Any
import logging

from mautrix.bridge import Bridge
from mautrix.types import RoomID, UserID

from .commands import CommandProcessor
from .config import Config
from .db import Database
from .matrix_handler import MatrixHandler
from .portal import Portal
from .puppet import Puppet
from .user import User
from .version import version, linkified_version


log = logging.getLogger("todus_bridge")


class ToDusBridge(Bridge):
    """
    Clase principal del puente toDus-Matrix.
    
    Extiende la clase Bridge de mautrix y coordina todos los componentes
    del puente siguiendo el patrón de mautrix-telegram.
    """
    
    module = "todus_bridge"
    name = "todus-bridge"
    beeper_service_name = "todus"
    beeper_network_name = "todus"
    command = "python -m todus_bridge"
    description = "Un puente bidireccional entre toDus y Matrix"
    repo_url = "https://github.com/nyxthor-dev/todus-bridge"
    version = version
    markdown_version = linkified_version
    config_class = Config
    
    # Componentes
    config: Config
    matrix: MatrixHandler | None
    db: Database | None
    commands: CommandProcessor | None
    
    def prepare_db(self) -> None:
        """Prepara la base de datos."""
        super().prepare_db()
        self.db = Database.get_instance(self.config)
    
    def prepare_bridge(self) -> None:
        """Prepara el puente después de que la configuración y DB estén listas."""
        log.info("Preparando puente toDus-Matrix...")
        
        # Inicializar clases base
        User.init_cls(self)
        Puppet.init_cls(self)
        Portal.init_cls(self)
        
        # Crear handler de Matrix
        self.matrix = MatrixHandler(self)
        
        # Crear procesador de comandos
        self.commands = CommandProcessor(self)
        
        # Registrar comandos
        self._register_commands()
        
        # Inicializar base de datos
        self.add_startup_actions(self._init_database())
        
        log.info("Puente preparado exitosamente")
    
    def _register_commands(self) -> None:
        """Registra todos los comandos disponibles."""
        if not self.commands:
            return
        
        # Importar comandos para registrarlos
        from .commands import auth, portal, groups, misc
        
        # Los comandos se registran automáticamente vía decoradores
        log.info(f"Comandos registrados: {len(self.commands.commands)}")
    
    async def _init_database(self) -> None:
        """Inicializa la base de datos."""
        if self.db:
            try:
                await self.db.init()
                log.info("Base de datos inicializada")
            except Exception as e:
                log.error(f"Error al inicializar base de datos: {e}")
                raise
    
    async def get_user(self, user_id: UserID, create: bool = True) -> User | None:
        """Obtiene o crea un usuario por su MXID."""
        user = await User.get_by_mxid(user_id, create=create)
        if user:
            await user.ensure_started()
        return user
    
    async def get_portal(self, room_id: RoomID) -> Portal | None:
        """Obtiene un portal por su RoomID."""
        return await Portal.get_by_mxid(room_id)
    
    async def get_puppet(self, user_id: UserID, create: bool = False) -> Puppet | None:
        """Obtiene o crea un puppet."""
        return await Puppet.get_by_mxid(user_id, create=create)
    
    async def get_double_puppet(self, user_id: UserID) -> Puppet | None:
        """Obtiene un double puppet (usuario Matrix autenticado)."""
        return await Puppet.get_by_custom_mxid(user_id)
    
    def is_bridge_ghost(self, user_id: UserID) -> bool:
        """Verifica si un user_id es un ghost user del puente."""
        return bool(Puppet.get_id_from_mxid(user_id))
    
    async def count_logged_in_users(self) -> int:
        """Cuenta usuarios logueados en toDus."""
        return len([user for user in User.by_mxid.values() if user.is_logged_in])
    
    async def manhole_global_namespace(self, user_id: UserID) -> dict[str, Any]:
        """Proporciona namespace global para el manhole."""
        return {
            **await super().manhole_global_namespace(user_id),
            "User": User,
            "Portal": Portal,
            "Puppet": Puppet,
            "Database": Database,
        }
    
    def prepare_stop(self) -> None:
        """Prepara la detención del puente."""
        log.info("Deteniendo puente...")
        
        # Cerrar sesiones de usuarios
        if hasattr(User, 'by_mxid'):
            for user in User.by_mxid.values():
                if user.is_logged_in:
                    self.add_shutdown_actions(user.logout_from_todus())
        
        # Cerrar base de datos
        if self.db:
            self.add_shutdown_actions(self.db.close())


def main() -> None:
    """Función principal para ejecutar el puente."""
    ToDusBridge().run()


if __name__ == "__main__":
    main()
