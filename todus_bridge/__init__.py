"""
toDus-Matrix Bridge - Un puente entre toDus y Matrix al estilo mautrix

Este módulo implementa un puente bidireccional entre la red de mensajería toDus
y Matrix, permitiendo a los usuarios interactuar con contactos de toDus desde
clientes Matrix.
"""

from .bridge import ToDusBridge
from .config import Config
from .user import User
from .puppet import Puppet
from .portal import Portal
from .matrix_handler import MatrixHandler

__version__ = "0.1.0"
__all__ = [
    "ToDusBridge",
    "Config",
    "User",
    "Puppet",
    "Portal",
    "MatrixHandler",
]
