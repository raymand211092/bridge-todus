"""
Módulo de base de datos para el puente toDus-Matrix

Este módulo maneja la persistencia de datos usando PostgreSQL,
incluyendo sesiones de usuarios, portales y configuración.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import asyncpg

from .config import Config

log = logging.getLogger("todus_bridge.db")


class Database:
    """
    Manejador de base de datos PostgreSQL para el puente.
    
    Proporciona métodos para guardar y recuperar:
    - Sesiones de usuarios de toDus
    - Portales (salas Matrix <-> Chats toDus)
    - Puppets (usuarios fantasma)
    """

    _instance: "Database | None" = None

    def __init__(self, config: Config) -> None:
        self.config = config
        self.pool: asyncpg.Pool | None = None
        self._lock = asyncio.Lock()
        self._initialized = False

    @classmethod
    def get_instance(cls, config: Config) -> "Database":
        """Obtiene la instancia singleton de la base de datos."""
        if cls._instance is None:
            cls._instance = cls(config)
        return cls._instance

    async def init(self) -> None:
        """Inicializa la conexión a la base de datos y crea las tablas."""
        if self._initialized:
            return

        async with self._lock:
            if self._initialized:
                return

            try:
                # Crear pool de conexiones
                self.pool = await asyncpg.create_pool(
                    host=self.config.db_host,
                    port=self.config.db_port,
                    database=self.config.db_name,
                    user=self.config.db_user,
                    password=self.config.db_password,
                    min_size=2,
                    max_size=10,
                )

                # Crear tablas
                await self._create_tables()

                self._initialized = True
                log.info("Base de datos inicializada exitosamente")

            except Exception as e:
                log.error(f"Error al inicializar base de datos: {e}")
                raise

    async def _create_tables(self) -> None:
        """Crea las tablas necesarias si no existen."""
        if not self.pool:
            return

        async with self.pool.acquire() as conn:
            # Tabla de usuarios
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    mxid TEXT PRIMARY KEY,
                    todus_phone TEXT,
                    todus_token TEXT,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    last_login TIMESTAMPTZ,
                    is_active BOOLEAN DEFAULT TRUE
                )
            """)

            # Tabla de portales
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS portals (
                    todus_jid TEXT PRIMARY KEY,
                    room_id TEXT,
                    portal_type TEXT DEFAULT 'direct',
                    name TEXT,
                    topic TEXT,
                    avatar_url TEXT,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    last_activity TIMESTAMPTZ DEFAULT NOW()
                )
            """)

            # Tabla de puppets
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS puppets (
                    phone TEXT PRIMARY KEY,
                    mxid TEXT UNIQUE,
                    displayname TEXT,
                    avatar_url TEXT,
                    about TEXT,
                    is_registered BOOLEAN DEFAULT FALSE,
                    last_update TIMESTAMPTZ DEFAULT NOW()
                )
            """)

            # Tabla de salas de portales (muchos-a-muchos)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS portal_rooms (
                    id SERIAL PRIMARY KEY,
                    todus_jid TEXT REFERENCES portals(todus_jid),
                    room_id TEXT UNIQUE,
                    user_mxid TEXT REFERENCES users(mxid),
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)

            # Índices
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_users_phone ON users(todus_phone);
                CREATE INDEX IF NOT EXISTS idx_portals_room ON portals(room_id);
                CREATE INDEX IF NOT EXISTS idx_portal_rooms_user ON portal_rooms(user_mxid);
                CREATE INDEX IF NOT EXISTS idx_portal_rooms_room ON portal_rooms(room_id);
            """)

            log.debug("Tablas creadas exitosamente")

    async def close(self) -> None:
        """Cierra la conexión a la base de datos."""
        if self.pool:
            await self.pool.close()
            self.pool = None
            self._initialized = False
            log.info("Conexión a base de datos cerrada")

    # Métodos de Usuario

    async def save_user_session(
        self,
        mxid: str,
        phone: str,
        token: str | None = None,
    ) -> None:
        """Guarda o actualiza la sesión de un usuario."""
        if not self.pool:
            return

        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO users (mxid, todus_phone, todus_token, last_login, is_active)
                VALUES ($1, $2, $3, NOW(), TRUE)
                ON CONFLICT (mxid) DO UPDATE SET
                    todus_phone = EXCLUDED.todus_phone,
                    todus_token = EXCLUDED.todus_token,
                    last_login = NOW(),
                    is_active = TRUE
            """, mxid, phone, token)

            log.debug(f"Sesión guardada para {mxid}")

    async def get_user_session(self, mxid: str) -> dict[str, Any] | None:
        """Recupera la sesión de un usuario."""
        if not self.pool:
            return None

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM users WHERE mxid = $1 AND is_active = TRUE",
                mxid,
            )

            if row:
                return dict(row)
            return None

    async def delete_user_session(self, mxid: str) -> None:
        """Elimina la sesión de un usuario."""
        if not self.pool:
            return

        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE users SET todus_token = NULL, is_active = FALSE
                WHERE mxid = $1
            """, mxid)

            log.debug(f"Sesión eliminada para {mxid}")

    async def get_user_by_phone(self, phone: str) -> list[dict[str, Any]]:
        """Obtiene todos los usuarios asociados a un número de teléfono."""
        if not self.pool:
            return []

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM users WHERE todus_phone = $1 AND is_active = TRUE",
                phone,
            )
            return [dict(row) for row in rows]

    # Métodos de Portal

    async def save_portal(
        self,
        todus_jid: str,
        room_id: str | None = None,
        portal_type: str = "direct",
        name: str | None = None,
        topic: str | None = None,
        avatar_url: str | None = None,
    ) -> None:
        """Guarda o actualiza un portal."""
        if not self.pool:
            return

        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO portals (todus_jid, room_id, portal_type, name, topic, avatar_url, last_activity)
                VALUES ($1, $2, $3, $4, $5, $6, NOW())
                ON CONFLICT (todus_jid) DO UPDATE SET
                    room_id = EXCLUDED.room_id,
                    portal_type = EXCLUDED.portal_type,
                    name = EXCLUDED.name,
                    topic = EXCLUDED.topic,
                    avatar_url = EXCLUDED.avatar_url,
                    last_activity = NOW()
            """, todus_jid, room_id, portal_type, name, topic, avatar_url)

    async def get_portal_by_jid(self, todus_jid: str) -> dict[str, Any] | None:
        """Obtiene un portal por su JID de toDus."""
        if not self.pool:
            return None

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM portals WHERE todus_jid = $1",
                todus_jid,
            )
            return dict(row) if row else None

    async def get_portal_by_room(self, room_id: str) -> dict[str, Any] | None:
        """Obtiene un portal por su ID de sala Matrix."""
        if not self.pool:
            return None

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM portals WHERE room_id = $1",
                room_id,
            )
            return dict(row) if row else None

    async def get_all_portals_for_user(
        self,
        user_mxid: str,
        portal_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Obtiene todos los portales de un usuario.
        
        Args:
            user_mxid: MXID del usuario
            portal_type: Filtrar por tipo ('direct', 'group', 'channel')
        """
        if not self.pool:
            return []

        query = """
            SELECT p.* FROM portals p
            JOIN portal_rooms pr ON p.todus_jid = pr.todus_jid
            WHERE pr.user_mxid = $1
        """
        params: list[Any] = [user_mxid]

        if portal_type:
            query += " AND p.portal_type = $2"
            params.append(portal_type)

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
            return [dict(row) for row in rows]

    async def add_portal_room(
        self,
        todus_jid: str,
        room_id: str,
        user_mxid: str,
    ) -> None:
        """Agrega una sala a un portal."""
        if not self.pool:
            return

        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO portal_rooms (todus_jid, room_id, user_mxid)
                VALUES ($1, $2, $3)
                ON CONFLICT (room_id) DO NOTHING
            """, todus_jid, room_id, user_mxid)

    # Métodos de Puppet

    async def save_puppet(
        self,
        phone: str,
        mxid: str,
        displayname: str | None = None,
        avatar_url: str | None = None,
        about: str | None = None,
        is_registered: bool = False,
    ) -> None:
        """Guarda o actualiza un puppet."""
        if not self.pool:
            return

        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO puppets (phone, mxid, displayname, avatar_url, about, is_registered, last_update)
                VALUES ($1, $2, $3, $4, $5, $6, NOW())
                ON CONFLICT (phone) DO UPDATE SET
                    mxid = EXCLUDED.mxid,
                    displayname = EXCLUDED.displayname,
                    avatar_url = EXCLUDED.avatar_url,
                    about = EXCLUDED.about,
                    is_registered = EXCLUDED.is_registered,
                    last_update = NOW()
            """, phone, mxid, displayname, avatar_url, about, is_registered)

    async def get_puppet_by_phone(self, phone: str) -> dict[str, Any] | None:
        """Obtiene un puppet por su número de teléfono."""
        if not self.pool:
            return None

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM puppets WHERE phone = $1",
                phone,
            )
            return dict(row) if row else None

    async def update_puppet_registration(
        self,
        phone: str,
        is_registered: bool,
    ) -> None:
        """Actualiza el estado de registro de un puppet."""
        if not self.pool:
            return

        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE puppets SET is_registered = $2, last_update = NOW()
                WHERE phone = $1
            """, phone, is_registered)

    # Métodos utilitarios

    async def get_stats(self) -> dict[str, int]:
        """Obtiene estadísticas de la base de datos."""
        if not self.pool:
            return {}

        async with self.pool.acquire() as conn:
            users_count = await conn.fetchval("SELECT COUNT(*) FROM users WHERE is_active = TRUE")
            portals_count = await conn.fetchval("SELECT COUNT(*) FROM portals")
            puppets_count = await conn.fetchval("SELECT COUNT(*) FROM puppets")

            return {
                "users": users_count or 0,
                "portals": portals_count or 0,
                "puppets": puppets_count or 0,
            }

    async def cleanup_inactive_users(self, days: int = 90) -> int:
        """Limpia usuarios inactivos."""
        if not self.pool:
            return 0

        async with self.pool.acquire() as conn:
            result = await conn.execute("""
                UPDATE users SET is_active = FALSE
                WHERE last_login < NOW() - INTERVAL '%s days'
            """ % days)

            # Extraer número de filas afectadas
            count = int(result.split()[-1]) if result else 0
            log.info(f"{count} usuarios marcados como inactivos")
            return count
