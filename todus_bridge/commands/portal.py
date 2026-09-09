"""
Comandos de portales y chats para el puente toDus-Matrix

Implementa los comandos: chat, search, contacts, list
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import logging

from mautrix.types import EventID

from .handler import SECTION_PORTAL, CommandEvent, command_handler

if TYPE_CHECKING:
    from ..portal import Portal


log = logging.getLogger("todus_bridge.commands.portal")


@command_handler(
    name="chat",
    help_text="Crea o abre un chat con un contacto de toDus",
    help_args="<número>",
    help_section=SECTION_PORTAL,
    management_only=True,
    needs_login=True,
)
async def chat_command(evt: CommandEvent) -> EventID:
    """
    Crea un nuevo chat o abre uno existente con un contacto de toDus.
    
    Uso: !todus chat <número>
    Ejemplo: !todus chat 51987654321
    """
    if len(evt.args) < 1:
        return await evt.reply(
            "❌ **Uso incorrecto**\n\n"
            f"`{evt.processor.command_prefix} chat <número>`\n\n"
            "Ejemplo:\n"
            "`!todus chat 51987654321`"
        )
    
    phone = evt.args[0]
    
    # Construir JID
    try:
        from todus.util import build_jid
        jid = build_jid(phone)
    except ImportError:
        # Fallback si la función no está disponible
        jid = f"{phone}@s.whatsapp.net"
    
    # Obtener o crear portal
    portal = await evt.bridge.get_portal_by_jid(jid)
    
    if not portal:
        # Crear nuevo portal
        from ..portal import Portal
        
        portal = Portal(
            todus_jid=jid,
            bridge=evt.bridge,
            portal_type="direct",
        )
        
        # Guardar en base de datos
        if hasattr(evt.bridge, 'db') and evt.bridge.db:
            await evt.bridge.db.save_portal(jid, portal_type="direct")
    
    # Crear sala Matrix
    try:
        room_id = await portal.create_matrix_room(evt.sender)
        
        if not room_id:
            return await evt.reply("❌ No se pudo crear la sala Matrix.")
        
        # Guardar relación portal-sala
        if hasattr(evt.bridge, 'db') and evt.bridge.db:
            await evt.bridge.db.add_portal_room(jid, room_id, evt.sender.mxid)
        
        return await evt.reply(
            "✅ **Chat creado exitosamente**\n\n"
            f"Contacto: `{phone}`\n"
            f"JID: `{jid}`\n"
            f"Sala: {room_id}\n\n"
            "Ahora puedes enviar mensajes directamente en esa sala."
        )
    
    except Exception as e:
        log.exception(f"Error al crear chat: {e}")
        return await evt.reply(f"❌ Error al crear chat: {type(e).__name__}: {str(e)}")


@command_handler(
    name="search",
    help_text="Busca contactos en toDus por nombre o número",
    help_args="<búsqueda>",
    help_section=SECTION_PORTAL,
    management_only=True,
    needs_login=True,
)
async def search_command(evt: CommandEvent) -> EventID:
    """
    Busca contactos en toDus.
    
    Uso: !todus search <nombre o número>
    Ejemplo: !todus search Juan Pérez
    """
    if len(evt.args) < 1:
        return await evt.reply(
            "❌ **Uso incorrecto**\n\n"
            f"`{evt.processor.command_prefix} search <búsqueda>`\n\n"
            "Ejemplo:\n"
            "`!todus search Juan Pérez`"
        )
    
    query = " ".join(evt.args)
    
    if not evt.sender.todus_client:
        return await evt.reply("❌ Cliente de toDus no disponible.")
    
    try:
        # Intentar buscar usando la API de toDus
        results = await evt.sender.todus_client.search_contacts(query)
        
        if not results:
            return await evt.reply(
                f"🔍 **No se encontraron resultados**\n\n"
                f"No hay contactos que coincidan con: `{query}`"
            )
        
        # Formatear resultados (limitar a 10)
        lines = [f"🔍 **Resultados para '{query}':**\n"]
        
        for contact in results[:10]:
            name = contact.get('name', 'Desconocido')
            contact_phone = contact.get('phone', 'N/A')
            lines.append(f"• **{name}** - `{contact_phone}`")
        
        if len(results) > 10:
            lines.append(f"\n_y más {len(results) - 10} resultados..._")
        
        lines.append("\n💡 Usa `!todus chat <número>` para chatear con un contacto.")
        
        return await evt.reply("\n".join(lines))
    
    except Exception as e:
        log.exception(f"Error al buscar contactos: {e}")
        return await evt.reply(f"❌ Error al buscar: {type(e).__name__}")


@command_handler(
    name="contacts",
    help_text="Lista todos tus contactos de toDus",
    help_section=SECTION_PORTAL,
    management_only=True,
    needs_login=True,
)
async def contacts_command(evt: CommandEvent) -> EventID:
    """Lista todos los contactos del usuario en toDus."""
    if not evt.sender.todus_client:
        return await evt.reply("❌ Cliente de toDus no disponible.")
    
    try:
        contacts = await evt.sender.todus_client.get_contacts()
        
        if not contacts:
            return await evt.reply("📞 **Sin contactos**\n\nNo tienes contactos en toDus.")
        
        # Formatear lista (limitar a 20)
        lines = [f"📞 **Tus contactos ({len(contacts)}):**\n"]
        
        for contact in contacts[:20]:
            name = contact.get('name', 'Desconocido')
            phone = contact.get('phone', 'N/A')
            lines.append(f"• **{name}** - `{phone}`")
        
        if len(contacts) > 20:
            lines.append(f"\n_y más {len(contacts) - 20} contactos..._")
        
        lines.append("\n💡 Usa `!todus chat <número>` para iniciar un chat.")
        
        return await evt.reply("\n".join(lines))
    
    except Exception as e:
        log.exception(f"Error al obtener contactos: {e}")
        return await evt.reply(f"❌ Error al obtener contactos: {type(e).__name__}")


@command_handler(
    name="list",
    help_text="Lista tus chats/portales activos",
    help_args="[tipo]",
    help_section=SECTION_PORTAL,
    management_only=True,
    needs_login=True,
)
async def list_command(evt: CommandEvent) -> EventID:
    """
    Lista todos los portales/chats activos del usuario.
    
    Uso: !todus list [tipo]
    Tipos: all, direct, group, channel
    """
    filter_type = evt.args[0] if evt.args else None
    
    # Mapear filtros
    type_map = {
        "all": None,
        "direct": "direct",
        "group": "group",
        "channel": "channel",
    }
    
    portal_filter = type_map.get(filter_type, filter_type) if filter_type else None
    
    if not hasattr(evt.bridge, 'db') or not evt.bridge.db:
        return await evt.reply("⚠️ Base de datos no disponible.")
    
    try:
        portals = await evt.bridge.db.get_all_portals_for_user(
            evt.sender.mxid,
            portal_filter,
        )
        
        if not portals:
            filter_msg = f" ({filter_type})" if filter_type else ""
            return await evt.reply(f"📋 **Sin chats{filter_msg}**\n\nNo tienes chats activos.")
        
        # Agrupar por tipo
        by_type: dict[str, list] = {}
        for portal in portals:
            ptype = portal.get('portal_type', 'direct')
            if ptype not in by_type:
                by_type[ptype] = []
            by_type[ptype].append(portal)
        
        lines = ["📋 **Tus chats activos:**\n"]
        
        icons = {
            "direct": "💬",
            "group": "👥",
            "channel": "📢",
        }
        
        for ptype, items in sorted(by_type.items()):
            icon = icons.get(ptype, "📍")
            type_name = {"direct": "Chats", "group": "Grupos", "channel": "Canales"}.get(ptype, ptype)
            lines.append(f"**{icon} {type_name} ({len(items)}):**")
            
            for portal in items[:10]:  # Limitar por tipo
                jid = portal.get('todus_jid', 'N/A')
                room_id = portal.get('room_id', 'N/A')
                name = portal.get('name', 'Sin nombre')
                lines.append(f"• **{name}** - `{jid}`")
                lines.append(f"  Sala: {room_id}")
            
            if len(items) > 10:
                lines.append(f"_y {len(items) - 10} más..._")
            
            lines.append("")
        
        return await evt.reply("\n".join(lines))
    
    except Exception as e:
        log.exception(f"Error al listar portales: {e}")
        return await evt.reply(f"❌ Error al listar chats: {type(e).__name__}")
