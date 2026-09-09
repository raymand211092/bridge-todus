"""
Comandos del puente toDus-Matrix

Este módulo implementa el sistema de comandos similar a mautrix-telegram,
permitiendo a los usuarios interactuar con el bot mediante comandos como
!todus login, !todus help, etc.
"""

from .handler import (
    SECTION_AUTH,
    SECTION_GROUPS,
    SECTION_MISC,
    SECTION_PORTAL,
    CommandEvent,
    CommandHandler,
    CommandProcessor,
    command_handler,
)

# Importar todos los comandos después del handler para evitar circular imports
from . import auth, portal, groups, misc  # isort: skip

__all__ = [
    "command_handler",
    "CommandHandler",
    "CommandProcessor",
    "CommandEvent",
    "SECTION_AUTH",
    "SECTION_MISC",
    "SECTION_GROUPS",
    "SECTION_PORTAL",
]
