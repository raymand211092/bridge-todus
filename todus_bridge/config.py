"""
Configuración del puente toDus-Matrix

Este módulo maneja la carga y validación de la configuración del puente,
incluyendo credenciales de Matrix, configuración de toDus, y opciones del bridge.
"""

from __future__ import annotations

from typing import Any
import logging

import yaml
from mautrix.bridge import BaseBridgeConfig
from mautrix.types import UserID

log = logging.getLogger("todus_bridge.config")


class Config(BaseBridgeConfig):
    """Configuración del puente toDus-Matrix."""

    @property
    def _forbidden(self) -> set[str]:
        return {
            "homeserver",
            "appservice",
            "bridge",
            "todus",
            "logging",
        }

    def __init__(self, path: str) -> None:
        super().__init__(path)
        self.homeserver_url: str = ""
        self.domain: str = ""
        self.appservice_address: str = ""
        self.appservice_token: str = ""
        self.bot_username: str = ""
        self.bot_password: str = ""
        
        # Configuración de toDus
        self.todus_proxy: str | None = None
        self.todus_verify_ssl: bool = False
        
        # Configuración de PostgreSQL
        self.db_host: str = "localhost"
        self.db_port: int = 5432
        self.db_name: str = "todus_bridge"
        self.db_user: str = "todus"
        self.db_password: str = ""
        
        # Opciones del bridge
        self.default_bridge_room_type: str = "dm"
        self.encryption_default: bool = False
        self.relay_mode_enabled: bool = False

    def load(self) -> None:
        """Carga la configuración desde el archivo YAML."""
        with open(self.path, "r") as f:
            data = yaml.safe_load(f)
        
        if not data:
            raise ValueError(f"Config file {self.path} is empty or invalid")
        
        # Homeserver
        hs_config = data.get("homeserver", {})
        self.homeserver_url = hs_config.get("address", "http://localhost:8008")
        self.domain = hs_config.get("domain", "localhost")
        
        # Appservice
        as_config = data.get("appservice", {})
        self.appservice_address = as_config.get("address", "http://localhost:29318")
        self.appservice_token = as_config.get("token", "")
        
        # Bot credentials
        self.bot_username = as_config.get("bot_username", "todusbot")
        self.bot_password = as_config.get("bot_password", "")
        
        # toDus configuration
        todus_config = data.get("todus", {})
        self.todus_proxy = todus_config.get("proxy")
        self.todus_verify_ssl = todus_config.get("verify_ssl", False)
        
        # PostgreSQL configuration
        db_config = data.get("database", {})
        self.db_host = db_config.get("host", "localhost")
        self.db_port = db_config.get("port", 5432)
        self.db_name = db_config.get("name", "todus_bridge")
        self.db_user = db_config.get("user", "todus")
        self.db_password = db_config.get("password", "")
        
        # Bridge options
        bridge_config = data.get("bridge", {})
        self.default_bridge_room_type = bridge_config.get("default_room_type", "dm")
        self.encryption_default = bridge_config.get("encryption_default", False)
        self.relay_mode_enabled = bridge_config.get("relay_mode_enabled", False)
        
        log.info("Configuración cargada exitosamente")

    def save(self) -> None:
        """Guarda la configuración actual al archivo YAML."""
        data = {
            "homeserver": {
                "address": self.homeserver_url,
                "domain": self.domain,
            },
            "appservice": {
                "address": self.appservice_address,
                "token": self.appservice_token,
                "bot_username": self.bot_username,
                "bot_password": self.bot_password,
            },
            "todus": {
                "proxy": self.todus_proxy,
                "verify_ssl": self.todus_verify_ssl,
            },
            "database": {
                "host": self.db_host,
                "port": self.db_port,
                "name": self.db_name,
                "user": self.db_user,
                "password": self.db_password,
            },
            "bridge": {
                "default_room_type": self.default_bridge_room_type,
                "encryption_default": self.encryption_default,
                "relay_mode_enabled": self.relay_mode_enabled,
            },
        }
        
        with open(self.path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True)
        
        log.info("Configuración guardada exitosamente")

    def is_valid(self) -> bool:
        """Valida que la configuración tenga los valores requeridos."""
        required = [
            self.homeserver_url,
            self.domain,
            self.appservice_address,
            self.appservice_token,
        ]
        return all(required)

    @classmethod
    def load_from_file(cls, path: str) -> "Config":
        """Crea una instancia de Config cargando desde un archivo."""
        config = cls(path)
        config.load()
        return config
