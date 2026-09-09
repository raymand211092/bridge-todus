"""
Comandos misceláneos para el puente toDus-Matrix

Implementa comandos adicionales y utilitarios.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import logging

from mautrix.types import EventID

from .handler import SECTION_MISC, CommandEvent, command_handler

if TYPE_CHECKING:
    from ..bridge import ToDusBridge


log = logging.getLogger("todus_bridge.commands.misc")


@command_handler(
    name="version",
    help_text="Muestra la versión del puente",
    help_section=SECTION_MISC,
    management_only=True,
)
async def version_command(evt: CommandEvent) -> EventID:
    """Muestra información de versión del puente."""
    from .. import __version__
    
    return await evt.reply(
        "🌉 **Puente toDus-Matrix**\n\n"
        f"**Versión:** {__version__}\n"
        f"**Framework:** mautrix-python\n\n"
        "Un puente bidireccional entre toDus y Matrix."
    )


@command_handler(
    name="stats",
    help_text="Muestra estadísticas del puente",
    help_section=SECTION_MISC,
    management_only=True,
)
async def stats_command(evt: CommandEvent) -> EventID:
    """Muestra estadísticas de uso del puente."""
    if not hasattr(evt.bridge, 'db') or not evt.bridge.db:
        return await evt.reply("⚠️ Base de datos no disponible.")
    
    try:
        stats = await evt.bridge.db.get_stats()
        
        users_count = stats.get('users', 0)
        portals_count = stats.get('portals', 0)
        puppets_count = stats.get('puppets', 0)
        
        return await evt.reply(
            "📊 **Estadísticas del Puente**\n\n"
            f"**Usuarios activos:** {users_count}\n"
            f"**Portales:** {portals_count}\n"
            f"**Puppets:** {puppets_count}"
        )
    
    except Exception as e:
        log.exception(f"Error al obtener estadísticas: {e}")
        return await evt.reply(f"❌ Error: {type(e).__name__}")


@command_handler(
    name="echo",
    help_text="Envía un mensaje de prueba (solo admins)",
    help_args="<mensaje>",
    help_section=SECTION_MISC,
    management_only=True,
    needs_admin=True,
)
async def echo_command(evt: CommandEvent) -> EventID:
    """
    Comando de depuración que envía un mensaje de prueba.
    Solo disponible para administradores.
    """
    if not evt.args:
        return await evt.reply("Uso: `!todus echo <mensaje>`")
    
    message = " ".join(evt.args)
    return await evt.reply(f"🔊 Echo: {message}")
