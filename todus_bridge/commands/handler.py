"""
Handler de comandos para el puente toDus-Matrix

Implementa el sistema de procesamiento de comandos similar a mautrix-telegram,
con soporte para secciones de ayuda, permisos y validación.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Awaitable, Callable, NamedTuple
import logging

from mautrix.bridge.commands import (
    CommandEvent as BaseCommandEvent,
    CommandHandler as BaseCommandHandler,
    CommandHandlerFunc,
    CommandProcessor as BaseCommandProcessor,
    HelpSection,
    command_handler as base_command_handler,
)
from mautrix.types import EventID, MessageEventContent, RoomID

if TYPE_CHECKING:
    from ..bridge import ToDusBridge
    from ..user import User
    from ..portal import Portal


log = logging.getLogger("todus_bridge.commands")


class HelpCacheKey(NamedTuple):
    """Clave para cachear el estado de permisos de ayuda."""
    is_management: bool
    is_portal: bool
    is_logged_in: bool
    is_admin: bool


# Secciones de ayuda ordenadas por prioridad
SECTION_AUTH = HelpSection("🔐 Autenticación", 10, "Comandos para iniciar/cerrar sesión")
SECTION_PORTAL = HelpSection("💬 Portales y Chats", 20, "Creación y gestión de portales")
SECTION_GROUPS = HelpSection("👥 Grupos y Canales", 30, "Gestión de grupos y canales")
SECTION_MISC = HelpSection("🔧 Misceláneo", 40, "Otros comandos y utilidades")


class CommandEvent(BaseCommandEvent):
    """
    Evento de comando extendido para el puente toDus.
    
    Proporciona acceso a objetos específicos del puente como
    el usuario, portal, configuración y cliente de toDus.
    """
    
    sender: "User"
    portal: "Portal | None"
    
    def __init__(
        self,
        processor: "CommandProcessor",
        room_id: RoomID,
        event_id: EventID,
        sender: "User",
        command: str,
        args: list[str],
        content: MessageEventContent,
        portal: "Portal | None",
        is_management: bool,
        has_bridge_bot: bool,
    ) -> None:
        super().__init__(
            processor,
            room_id,
            event_id,
            sender,
            command,
            args,
            content,
            portal,
            is_management,
            has_bridge_bot,
        )
        # Referencias a componentes del puente
        self.bridge: "ToDusBridge" = processor.bridge
        self.config = processor.config
        self.todus_client = sender.todus_client if sender else None
    
    @property
    def print_error_traceback(self) -> bool:
        """Mostrar traceback completo solo para admins."""
        return self.sender.is_admin if hasattr(self.sender, "is_admin") else False
    
    async def get_help_key(self) -> HelpCacheKey:
        """Obtiene la clave de caché para determinar qué ayuda mostrar."""
        return HelpCacheKey(
            self.is_management,
            self.portal is not None,
            await self.sender.is_logged_in() if hasattr(self.sender, "is_logged_in") else False,
            self.sender.is_admin if hasattr(self.sender, "is_admin") else False,
        )


class CommandHandler(BaseCommandHandler):
    """
    Handler de comandos con soporte para permisos específicos de toDus.
    """
    
    name: str
    needs_login: bool
    needs_puppeting: bool
    
    def __init__(
        self,
        handler: Callable[[CommandEvent], Awaitable[EventID]],
        management_only: bool,
        name: str,
        help_text: str,
        help_args: str = "",
        help_section: HelpSection = SECTION_MISC,
        needs_login: bool = False,
        needs_puppeting: bool = False,
        needs_admin: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            handler,
            management_only,
            name,
            help_text,
            help_args,
            help_section,
            needs_auth=needs_login,
            needs_puppeting=needs_puppeting,
            needs_matrix_puppeting=False,
            needs_admin=needs_admin,
            **kwargs,
        )
        self.needs_login = needs_login
        self.needs_puppeting = needs_puppeting
    
    async def get_permission_error(self, evt: CommandEvent) -> str | None:
        """Verifica permisos y retorna mensaje de error si aplica."""
        if self.needs_login:
            is_logged = await evt.sender.is_logged_in() if hasattr(evt.sender, "is_logged_in") else False
            if not is_logged:
                return "Debes iniciar sesión primero con `!todus login`"
        
        if self.needs_puppeting and not evt.sender.puppet_whitelisted if hasattr(evt.sender, "puppet_whitelisted") else False:
            return "Este comando requiere permisos de puppeting"
        
        return await super().get_permission_error(evt)
    
    def has_permission(self, key: HelpCacheKey) -> bool:
        """Verifica si el handler tiene permisos para ejecutarse."""
        if not super().has_permission(key):
            return False
        
        if self.needs_login and not key.is_logged_in:
            return False
        
        return True


class CommandProcessor(BaseCommandProcessor):
    """
    Procesador de comandos para el puente toDus.
    
    Maneja el registro y ejecución de comandos, así como
    la generación de mensajes de ayuda.
    """
    
    bridge: "ToDusBridge"
    config: Any
    commands: dict[str, CommandHandler]
    
    def __init__(self, bridge: "ToDusBridge") -> None:
        super().__init__(bridge)
        self.bridge = bridge
        self.config = bridge.config
        self.commands = {}
        self.command_prefix = "!todus"
        
        log.info("CommandProcessor inicializado")
    
    def register_command(self, handler: CommandHandler) -> None:
        """Registra un comando."""
        self.commands[handler.name] = handler
        log.debug(f"Comando registrado: {handler.name}")
    
    async def handle_command(self, evt: CommandEvent) -> EventID | None:
        """Maneja la ejecución de un comando."""
        handler = self.commands.get(evt.command)
        
        if not handler:
            return await evt.reply(
                f"Comando desconocido: `{evt.command}`\n\n"
                f"Usa `{self.command_prefix} help` para ver la lista de comandos."
            )
        
        # Verificar permisos
        permission_error = await handler.get_permission_error(evt)
        if permission_error:
            return await evt.reply(f"❌ {permission_error}")
        
        try:
            return await handler.handler(evt)
        except Exception as e:
            log.exception(f"Error ejecutando comando {evt.command}")
            
            if evt.print_error_traceback:
                return await evt.reply(f"❌ Error:\n```\n{str(e)}\n```")
            else:
                return await evt.reply(f"❌ Error ejecutando comando: {type(e).__name__}")
    
    async def get_help_text(self, evt: CommandEvent) -> str:
        """Genera el texto de ayuda mostrando comandos disponibles."""
        help_key = await evt.get_help_key()
        
        lines = [
            "**🌉 Puente toDus-Matrix - Comandos Disponibles**\n",
            f"Prefijo: `{self.command_prefix}`\n",
        ]
        
        # Agrupar comandos por sección
        sections: dict[HelpSection, list[CommandHandler]] = {}
        for cmd in sorted(self.commands.values(), key=lambda c: c.help_section.order):
            if cmd.has_permission(help_key):
                if cmd.help_section not in sections:
                    sections[cmd.help_section] = []
                sections[cmd.help_section].append(cmd)
        
        # Generar texto para cada sección
        for section in sorted(sections.keys(), key=lambda s: s.order):
            cmds = sections[section]
            if not cmds:
                continue
            
            lines.append(f"**{section.name}** - {section.description}")
            
            for cmd in cmds:
                args = f" {cmd.help_args}" if cmd.help_args else ""
                lines.append(f"• `{self.command_prefix} {cmd.name}{args}` - {cmd.help_text}")
            
            lines.append("")
        
        # Agregar ejemplos
        lines.extend([
            "**Ejemplos de uso:**",
            f"• `{self.command_prefix} login <teléfono> <contraseña>`",
            f"• `{self.command_prefix} chat <número>`",
            f"• `{self.command_prefix} search <nombre>`",
        ])
        
        return "\n".join(lines)


def command_handler(
    name: str | None = None,
    help_text: str = "",
    help_args: str = "",
    help_section: HelpSection = SECTION_MISC,
    management_only: bool = True,
    needs_login: bool = False,
    needs_puppeting: bool = False,
    needs_admin: bool = False,
    aliases: list[str] | None = None,
) -> Callable[[CommandHandlerFunc], CommandHandler]:
    """
    Decorador para registrar comandos.
    
    Args:
        name: Nombre del comando (sin prefijo)
        help_text: Descripción del comando
        help_args: Argumentos esperados
        help_section: Sección de ayuda donde aparecerá
        management_only: Solo disponible en salas de gestión
        needs_login: Requiere autenticación en toDus
        needs_puppeting: Requiere permisos de puppeting
        needs_admin: Solo para administradores
        aliases: Lista de alias alternativos
    """
    def decorator(func: CommandHandlerFunc) -> CommandHandler:
        handler_name = name or func.__name__.replace("_", "-")
        
        handler = CommandHandler(
            handler=func,
            management_only=management_only,
            name=handler_name,
            help_text=help_text,
            help_args=help_args,
            help_section=help_section,
            needs_login=needs_login,
            needs_puppeting=needs_puppeting,
            needs_admin=needs_admin,
        )
        
        if aliases:
            handler.aliases = aliases
        
        return handler
    
    return decorator
