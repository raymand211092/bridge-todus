"""
Comandos de autenticación para el puente toDus-Matrix

Implementa los comandos: login, logout, status, me, ping
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import logging

from mautrix.types import EventID

from .handler import SECTION_AUTH, CommandEvent, command_handler

if TYPE_CHECKING:
    from ..user import User


log = logging.getLogger("todus_bridge.commands.auth")


@command_handler(
    name="help",
    help_text="Muestra esta lista de comandos disponibles",
    help_section=SECTION_AUTH,
    management_only=True,
)
async def help_command(evt: CommandEvent) -> EventID:
    """Muestra la ayuda con todos los comandos disponibles."""
    help_text = await evt.processor.get_help_text(evt)
    return await evt.reply(help_text)


@command_handler(
    name="login",
    help_text="Inicia sesión en tu cuenta de toDus",
    help_args="<teléfono> <contraseña>",
    help_section=SECTION_AUTH,
    management_only=True,
)
async def login_command(evt: CommandEvent) -> EventID:
    """
    Inicia sesión en toDus con las credenciales proporcionadas.
    
    Uso: !todus login <teléfono> <contraseña>
    Ejemplo: !todus login 51987654321 mi_password_secreto
    """
    if len(evt.args) < 2:
        return await evt.reply(
            "❌ **Uso incorrecto**\n\n"
            f"`{evt.processor.command_prefix} login <teléfono> <contraseña>`\n\n"
            "Ejemplo:\n"
            "`!todus login 51987654321 mi_contraseña`"
        )
    
    phone = evt.args[0]
    password = evt.args[1]
    
    # Verificar si ya está logueado
    if await evt.sender.is_logged_in():
        return await evt.reply(
            "⚠️ Ya has iniciado sesión en toDus.\n\n"
            f"Teléfono: `{evt.sender.todus_phone}`\n\n"
            "Usa `!todus logout` para cerrar sesión primero."
        )
    
    # Intentar login
    success = await evt.sender.login_to_todus(phone, password)
    
    if not success:
        return await evt.reply(
            "❌ **Error de autenticación**\n\n"
            "No se pudo iniciar sesión en toDus. Verifica:\n"
            "• Tu número de teléfono es correcto\n"
            "• Tu contraseña es correcta\n"
            "• Tu cuenta de toDus está activa"
        )
    
    # Guardar sesión en la base de datos
    if hasattr(evt.bridge, 'db') and evt.bridge.db:
        await evt.bridge.db.save_user_session(evt.sender.mxid, phone, evt.sender.todus_token)
    
    # Actualizar información del puppet
    await evt.sender.update_puppet()
    
    return await evt.reply(
        "✅ **Inicio de sesión exitoso**\n\n"
        f"Has iniciado sesión como: `{phone}`\n"
        f"Estado: Conectado\n\n"
        "Ahora puedes usar comandos como:\n"
        "• `!todus chat <número>` - Crear un chat\n"
        "• `!todus contacts` - Ver tus contactos\n"
        "• `!todus groups` - Ver tus grupos\n"
        "• `!todus channels` - Ver tus canales\n"
        "• `!todus help` - Ver todos los comandos"
    )


@command_handler(
    name="logout",
    help_text="Cierra sesión en toDus",
    help_section=SECTION_AUTH,
    management_only=True,
    needs_login=True,
)
async def logout_command(evt: CommandEvent) -> EventID:
    """Cierra la sesión actual de toDus."""
    if not await evt.sender.is_logged_in():
        return await evt.reply("ℹ️ No has iniciado sesión en toDus.")
    
    # Cerrar sesión
    await evt.sender.logout_from_todus()
    
    # Eliminar sesión de la base de datos
    if hasattr(evt.bridge, 'db') and evt.bridge.db:
        await evt.bridge.db.delete_user_session(evt.sender.mxid)
    
    return await evt.reply(
        "✅ **Sesión cerrada**\n\n"
        "Has cerrado sesión en toDus exitosamente.\n"
        "Usa `!todus login` para iniciar sesión nuevamente."
    )


@command_handler(
    name="status",
    help_text="Muestra el estado de tu conexión a toDus",
    help_section=SECTION_AUTH,
    management_only=True,
)
async def status_command(evt: CommandEvent) -> EventID:
    """Muestra el estado actual de la conexión del usuario."""
    if not await evt.sender.is_logged_in():
        return await evt.reply(
            "❌ **No conectado**\n\n"
            "No has iniciado sesión en toDus.\n\n"
            f"Usa `{evt.processor.command_prefix} login <teléfono> <contraseña>` para iniciar sesión."
        )
    
    # Obtener información adicional si está disponible
    profile_info = ""
    if evt.sender.todus_client:
        try:
            profile = await evt.sender.todus_client.get_profile()
            if profile:
                name = profile.get('name', 'N/A')
                about = profile.get('about', 'Sin estado')
                profile_info = (
                    f"\n**Perfil:**\n"
                    f"Nombre: {name}\n"
                    f"Estado: {about}"
                )
        except Exception as e:
            log.debug(f"No se pudo obtener perfil: {e}")
    
    return await evt.reply(
        "✅ **Conectado a toDus**\n\n"
        f"**Teléfono:** `{evt.sender.todus_phone}`\n"
        f"**Estado:** En línea\n"
        f"{profile_info}"
    )


@command_handler(
    name="me",
    help_text="Muestra tu información de perfil de toDus",
    help_section=SECTION_AUTH,
    management_only=True,
    needs_login=True,
)
async def me_command(evt: CommandEvent) -> EventID:
    """Muestra la información de perfil del usuario en toDus."""
    if not evt.sender.todus_client:
        return await evt.reply("❌ Cliente de toDus no disponible.")
    
    try:
        profile = await evt.sender.todus_client.get_profile()
        
        if not profile:
            return await evt.reply("⚠️ No se pudo obtener tu perfil.")
        
        name = profile.get('name', 'Desconocido')
        about = profile.get('about', 'Sin estado')
        phone = evt.sender.todus_phone or 'N/A'
        
        return await evt.reply(
            "👤 **Tu perfil en toDus**\n\n"
            f"**Nombre:** {name}\n"
            f"**Teléfono:** `{phone}`\n"
            f"**Estado:** {about}"
        )
    
    except Exception as e:
        log.exception("Error al obtener perfil")
        return await evt.reply(f"❌ Error al obtener perfil: {type(e).__name__}")


@command_handler(
    name="ping",
    help_text="Verifica si el puente está respondiendo",
    help_section=SECTION_AUTH,
    management_only=True,
)
async def ping_command(evt: CommandEvent) -> EventID:
    """Verifica que el puente esté funcionando correctamente."""
    return await evt.reply("🏓 **¡Pong!**\n\nEl puente toDus-Matrix está funcionando correctamente.")
