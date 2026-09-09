"""
Comandos de grupos y canales para el puente toDus-Matrix

Implementa los comandos: groups, channels
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import logging

from mautrix.types import EventID

from .handler import SECTION_GROUPS, CommandEvent, command_handler

if TYPE_CHECKING:
    from ..portal import Portal


log = logging.getLogger("todus_bridge.commands.groups")


@command_handler(
    name="groups",
    help_text="Lista tus grupos de toDus",
    help_section=SECTION_GROUPS,
    management_only=True,
    needs_login=True,
)
async def groups_command(evt: CommandEvent) -> EventID:
    """Lista todos los grupos del usuario en toDus."""
    if not evt.sender.todus_client:
        return await evt.reply("❌ Cliente de toDus no disponible.")
    
    try:
        groups = await evt.sender.todus_client.get_groups()
        
        if not groups:
            return await evt.reply(
                "👥 **Sin grupos**\n\n"
                "No estás en ningún grupo de toDus."
            )
        
        # Formatear lista
        lines = [f"👥 **Tus grupos ({len(groups)}):**\n"]
        
        for group in groups:
            name = group.get('name', 'Sin nombre')
            jid = group.get('jid', 'N/A')
            participants = group.get('participants_count', '?')
            description = group.get('description', '')
            
            lines.append(f"**• {name}**")
            lines.append(f"  JID: `{jid}`")
            lines.append(f"  Participantes: {participants}")
            
            if description:
                # Truncar descripción larga
                desc_preview = description[:50] + "..." if len(description) > 50 else description
                lines.append(f"  Descripción: {desc_preview}")
            
            lines.append("")
        
        lines.append("💡 Usa `!todus chat <jid>` para unirse a un grupo.")
        
        return await evt.reply("\n".join(lines))
    
    except Exception as e:
        log.exception(f"Error al obtener grupos: {e}")
        return await evt.reply(f"❌ Error al obtener grupos: {type(e).__name__}: {str(e)}")


@command_handler(
    name="channels",
    help_text="Lista tus canales de toDus",
    help_section=SECTION_GROUPS,
    management_only=True,
    needs_login=True,
)
async def channels_command(evt: CommandEvent) -> EventID:
    """Lista todos los canales que el usuario sigue en toDus."""
    if not evt.sender.todus_client:
        return await evt.reply("❌ Cliente de toDus no disponible.")
    
    try:
        channels = await evt.sender.todus_client.get_channels()
        
        if not channels:
            return await evt.reply(
                "📢 **Sin canales**\n\n"
                "No sigues ningún canal de toDus."
            )
        
        # Formatear lista
        lines = [f"📢 **Tus canales ({len(channels)}):**\n"]
        
        for channel in channels:
            name = channel.get('name', 'Sin nombre')
            jid = channel.get('jid', 'N/A')
            subscribers = channel.get('subscribers_count', '?')
            description = channel.get('description', '')
            
            lines.append(f"**• {name}**")
            lines.append(f"  JID: `{jid}`")
            lines.append(f"  Suscriptores: {subscribers}")
            
            if description:
                # Truncar descripción larga
                desc_preview = description[:50] + "..." if len(description) > 50 else description
                lines.append(f"  Descripción: {desc_preview}")
            
            lines.append("")
        
        lines.append("💡 Usa `!todus chat <jid>` para seguir un canal.")
        
        return await evt.reply("\n".join(lines))
    
    except Exception as e:
        log.exception(f"Error al obtener canales: {e}")
        return await evt.reply(f"❌ Error al obtener canales: {type(e).__name__}: {str(e)}")
