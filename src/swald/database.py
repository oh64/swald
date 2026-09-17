import logging
from contextlib import asynccontextmanager
from typing import Any

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS bridges (
    id TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS bridge_rooms (
    bridge_id TEXT NOT NULL,
    connection_id TEXT NOT NULL,
    room_id TEXT NOT NULL,
    PRIMARY KEY (bridge_id, connection_id),
    FOREIGN KEY (bridge_id) REFERENCES bridges(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS message_links (
    connection_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    bridge_id TEXT NOT NULL,
    linked_connection_id TEXT NOT NULL,
    linked_message_id TEXT NOT NULL,
    PRIMARY KEY (connection_id, message_id, bridge_id, linked_connection_id)
);

CREATE INDEX IF NOT EXISTS idx_message_links_source
    ON message_links(connection_id, message_id);

CREATE TABLE IF NOT EXISTS traversed_bridges (
    connection_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    bridge_id TEXT NOT NULL,
    PRIMARY KEY (connection_id, message_id, bridge_id)
);

CREATE TABLE IF NOT EXISTS temporary_codes (
    code TEXT PRIMARY KEY,
    bridge_id TEXT NOT NULL,
    connection_id TEXT NOT NULL,
    room_id TEXT NOT NULL,
    expires_at DOUBLE PRECISION NOT NULL
);
"""


class SQLiteBackend:
    def __init__(self, path: str):
        self.path = path

    async def connect(self):
        import aiosqlite

        self.db = await aiosqlite.connect(self.path, isolation_level=None)
        await self.db.execute("PRAGMA foreign_keys = ON")
        await self.db.executescript(SCHEMA)
        await self.db.commit()

    async def close(self):
        await self.db.close()

    def _q(self, n: int) -> str:
        return ", ".join(["?"] * n)

    async def execute(self, sql: str, params: tuple = ()) -> Any:
        return await self.db.execute(sql, params)

    async def fetchone(self, sql: str, params: tuple = ()) -> tuple | None:
        cursor = await self.db.execute(sql, params)
        return await cursor.fetchone()

    async def fetchall(self, sql: str, params: tuple = ()) -> list[tuple]:
        cursor = await self.db.execute(sql, params)
        return await cursor.fetchall()

    async def execute_many(self, sql: str, params_list: list[tuple]):
        await self.db.executemany(sql, params_list)

    @asynccontextmanager
    async def transaction(self):
        await self.db.execute("BEGIN IMMEDIATE")
        try:
            yield self.db
        except Exception:
            await self.db.execute("ROLLBACK")
            raise
        else:
            await self.db.execute("COMMIT")

    def insert_ignore(self, table: str, columns: list[str], values: tuple) -> str:
        cols = ", ".join(columns)
        return (
            f"INSERT OR IGNORE INTO {table} ({cols}) "
            f"VALUES ({self._q(len(columns))})"
        )

    def upsert(
        self, table: str, columns: list[str], values: tuple,
        conflict_columns: list[str], update_columns: list[str]
    ) -> str:
        cols = ", ".join(columns)
        conflict = ", ".join(conflict_columns)
        updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_columns)
        return (
            f"INSERT INTO {table} ({cols}) VALUES ({self._q(len(columns))}) "
            f"ON CONFLICT ({conflict}) DO UPDATE SET {updates}"
        )


class PostgresBackend:
    def __init__(self, dsn: str):
        self.dsn = dsn

    async def connect(self):
        import asyncpg

        self.db = await asyncpg.connect(self.dsn)
        for statement in SCHEMA.split(";"):
            stmt = statement.strip()
            if stmt:
                await self.db.execute(stmt)

    async def close(self):
        await self.db.close()

    async def execute(self, sql: str, params: tuple = ()) -> Any:
        return await self.db.execute(sql, *params)

    async def fetchone(self, sql: str, params: tuple = ()) -> tuple | None:
        return await self.db.fetchrow(sql, *params)

    async def fetchall(self, sql: str, params: tuple = ()) -> list[tuple]:
        return await self.db.fetch(sql, *params)

    async def execute_many(self, sql: str, params_list: list[tuple]):
        await self.db.executemany(sql, params_list)

    @asynccontextmanager
    async def transaction(self):
        async with self.db.transaction():
            yield self.db

    def insert_ignore(self, table: str, columns: list[str], values: tuple) -> str:
        cols = ", ".join(columns)
        placeholders = ", ".join(f"${i+1}" for i in range(len(columns)))
        return (
            f"INSERT INTO {table} ({cols}) VALUES ({placeholders}) "
            f"ON CONFLICT DO NOTHING"
        )

    def upsert(
        self, table: str, columns: list[str], values: tuple,
        conflict_columns: list[str], update_columns: list[str]
    ) -> str:
        cols = ", ".join(columns)
        placeholders = ", ".join(f"${i+1}" for i in range(len(columns)))
        conflict = ", ".join(conflict_columns)
        updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_columns)
        return (
            f"INSERT INTO {table} ({cols}) VALUES ({placeholders}) "
            f"ON CONFLICT ({conflict}) DO UPDATE SET {updates}"
        )


class Database:
    def __init__(self, backend: SQLiteBackend | PostgresBackend):
        self.backend = backend

    @classmethod
    def from_config(cls, path: str | None = None, postgres_dsn: str | None = None):
        if postgres_dsn:
            logger.info("Using PostgreSQL backend")
            return cls(PostgresBackend(postgres_dsn))
        db_path = path or "swald.db"
        logger.info(f"Using SQLite backend at {db_path}")
        return cls(SQLiteBackend(db_path))

    async def connect(self):
        await self.backend.connect()
        logger.info("Database initialized")

    async def close(self):
        await self.backend.close()

    @asynccontextmanager
    async def transaction(self):
        async with self.backend.transaction():
            yield self.backend

    async def get_matching_bridges(
        self, connection_id: str, room_id: str
    ) -> list[str]:
        rows = await self.backend.fetchall(
            "SELECT bridge_id FROM bridge_rooms "
            "WHERE connection_id = $1 AND room_id = $2",
            (connection_id, room_id),
        )
        return [row[0] for row in rows]

    async def get_room_id(self, bridge_id: str, connection_id: str) -> str | None:
        row = await self.backend.fetchone(
            "SELECT room_id FROM bridge_rooms "
            "WHERE bridge_id = $1 AND connection_id = $2",
            (bridge_id, connection_id),
        )
        return row[0] if row else None

    async def get_bridge_connections(self, bridge_id: str) -> dict[str, str]:
        rows = await self.backend.fetchall(
            "SELECT connection_id, room_id FROM bridge_rooms "
            "WHERE bridge_id = $1",
            (bridge_id,),
        )
        return {row[0]: row[1] for row in rows}

    async def has_been_traversed(
        self, connection_id: str, message_id: str, bridge_id: str
    ) -> bool:
        row = await self.backend.fetchone(
            "SELECT 1 FROM traversed_bridges "
            "WHERE connection_id = $1 AND message_id = $2 AND bridge_id = $3",
            (connection_id, message_id, bridge_id),
        )
        return row is not None

    async def mark_traversed(
        self, connection_id: str, message_id: str, bridge_id: str
    ):
        sql = self.backend.insert_ignore(
            "traversed_bridges",
            ["connection_id", "message_id", "bridge_id"],
            (connection_id, message_id, bridge_id),
        )
        await self.backend.execute(sql, (connection_id, message_id, bridge_id))

    async def remove_traversed_bridges(self, connection_id: str, message_id: str):
        await self.backend.execute(
            "DELETE FROM traversed_bridges "
            "WHERE connection_id = $1 AND message_id = $2",
            (connection_id, message_id),
        )

    async def get_message_links(
        self, connection_id: str, message_id: str
    ) -> list[dict[str, Any]]:
        rows = await self.backend.fetchall(
            "SELECT bridge_id, linked_connection_id, linked_message_id "
            "FROM message_links "
            "WHERE connection_id = $1 AND message_id = $2",
            (connection_id, message_id),
        )
        return [
            {
                "bridge_id": row[0],
                "linked_connection_id": row[1],
                "linked_message_id": row[2],
            }
            for row in rows
        ]

    async def add_message_link(
        self,
        connection_id: str,
        message_id: str,
        bridge_id: str,
        linked_connection_id: str,
        linked_message_id: str,
    ):
        sql = self.backend.insert_ignore(
            "message_links",
            ["connection_id", "message_id", "bridge_id",
             "linked_connection_id", "linked_message_id"],
            (connection_id, message_id, bridge_id,
             linked_connection_id, linked_message_id),
        )
        await self.backend.execute(sql, (
            connection_id, message_id, bridge_id,
            linked_connection_id, linked_message_id,
        ))

    async def remove_message_links(self, connection_id: str, message_id: str):
        await self.backend.execute(
            "DELETE FROM message_links "
            "WHERE connection_id = $1 AND message_id = $2",
            (connection_id, message_id),
        )

    async def remove_specific_link(
        self,
        connection_id: str,
        message_id: str,
        bridge_id: str,
        linked_connection_id: str,
    ):
        await self.backend.execute(
            "DELETE FROM message_links "
            "WHERE connection_id = $1 AND message_id = $2 "
            "AND bridge_id = $3 AND linked_connection_id = $4",
            (connection_id, message_id, bridge_id, linked_connection_id),
        )

    async def add_temporary_code(
        self,
        code: str,
        bridge_id: str,
        connection_id: str,
        room_id: str,
        expires_at: float,
    ):
        sql = self.backend.upsert(
            "temporary_codes",
            ["code", "bridge_id", "connection_id", "room_id", "expires_at"],
            (code, bridge_id, connection_id, room_id, expires_at),
            ["code"],
            ["bridge_id", "connection_id", "room_id", "expires_at"],
        )
        await self.backend.execute(sql, (
            code, bridge_id, connection_id, room_id, expires_at,
        ))

    async def get_temporary_code(self, code: str) -> dict[str, Any] | None:
        row = await self.backend.fetchone(
            "SELECT bridge_id, connection_id, room_id, expires_at "
            "FROM temporary_codes WHERE code = $1",
            (code,),
        )
        if not row:
            return None
        return {
            "bridge_id": row[0],
            "connection_id": row[1],
            "room_id": row[2],
            "expires_at": row[3],
        }

    async def remove_temporary_code(self, code: str):
        await self.backend.execute(
            "DELETE FROM temporary_codes WHERE code = $1", (code,)
        )

    async def cleanup_expired_codes(self, now: float):
        await self.backend.execute(
            "DELETE FROM temporary_codes WHERE expires_at < $1", (now,)
        )

    async def record_bridged_message(
        self,
        source_connection_id: str,
        source_message_id: str,
        bridge_id: str,
        target_connection_id: str,
        target_message_id: str,
    ):
        async with self.transaction():
            await self.add_message_link(
                source_connection_id, source_message_id, bridge_id,
                target_connection_id, target_message_id,
            )
            await self.add_message_link(
                target_connection_id, target_message_id, bridge_id,
                source_connection_id, source_message_id,
            )
            await self.mark_traversed(source_connection_id, source_message_id, bridge_id)

    async def delete_bridged_message(
        self, connection_id: str, message_id: str
    ):
        links = await self.get_message_links(connection_id, message_id)
        if not links:
            return []

        async with self.transaction():
            await self.remove_message_links(connection_id, message_id)
            await self.remove_traversed_bridges(connection_id, message_id)
            for link in links:
                await self.remove_specific_link(
                    link["linked_connection_id"],
                    link["linked_message_id"],
                    link["bridge_id"],
                    connection_id,
                )

        return links

    async def create_bridge(
        self, bridge_id: str, connection_id: str, room_id: str
    ):
        async with self.transaction():
            await self.backend.execute(
                "INSERT INTO bridges (id) VALUES ($1)", (bridge_id,)
            )
            await self.backend.execute(
                "INSERT INTO bridge_rooms (bridge_id, connection_id, room_id) "
                "VALUES ($1, $2, $3)",
                (bridge_id, connection_id, room_id),
            )

    async def join_bridge(
        self, bridge_id: str, code: str, connection_id: str, room_id: str
    ):
        async with self.transaction():
            await self.add_room_to_bridge(bridge_id, connection_id, room_id)
            await self.remove_temporary_code(code)

    async def add_room_to_bridge(
        self, bridge_id: str, connection_id: str, room_id: str
    ):
        await self.backend.execute(
            "INSERT INTO bridge_rooms (bridge_id, connection_id, room_id) "
            "VALUES ($1, $2, $3)",
            (bridge_id, connection_id, room_id),
        )

    async def create_bridge_with_room(
        self, bridge_id: str, connection_id: str, room_id: str
    ):
        await self.backend.execute(
            "INSERT INTO bridges (id) VALUES ($1)", (bridge_id,)
        )
        await self.add_room_to_bridge(bridge_id, connection_id, room_id)

    async def leave_bridge(
        self, bridge_id: str, connection_id: str, room_id: str
    ) -> bool:
        async with self.transaction():
            await self.backend.execute(
                "DELETE FROM bridge_rooms "
                "WHERE bridge_id = $1 AND connection_id = $2 AND room_id = $3",
                (bridge_id, connection_id, room_id),
            )
            connections = await self.get_bridge_connections(bridge_id)
            if not connections:
                await self.backend.execute(
                    "DELETE FROM bridges WHERE id = $1", (bridge_id,)
                )
                return True
        return False
