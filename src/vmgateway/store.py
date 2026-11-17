"""SQLite persistence for users and leases."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from pathlib import Path

import aiosqlite

from .models import Lease, LeaseState, User

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    name TEXT PRIMARY KEY,
    key_hash TEXT NOT NULL UNIQUE,
    is_admin INTEGER NOT NULL DEFAULT 0,
    max_leases INTEGER,
    created_at TEXT NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS leases (
    id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS leases_owner ON leases(owner);
CREATE INDEX IF NOT EXISTS leases_state ON leases(state);
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._db: aiosqlite.Connection | None = None

    async def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Store.init() not called")
        return self._db

    # ---- leases -------------------------------------------------------------

    async def insert_lease(self, lease: Lease) -> None:
        await self.db.execute(
            "INSERT INTO leases (id, owner, state, created_at, data) VALUES (?, ?, ?, ?, ?)",
            (lease.id, lease.owner, lease.state.value, lease.created_at.isoformat(), lease.model_dump_json()),
        )
        await self.db.commit()

    async def update_lease(self, lease: Lease) -> None:
        await self.db.execute(
            "UPDATE leases SET state = ?, data = ? WHERE id = ?",
            (lease.state.value, lease.model_dump_json(), lease.id),
        )
        await self.db.commit()

    async def get_lease(self, lease_id: str) -> Lease | None:
        async with self.db.execute("SELECT data FROM leases WHERE id = ?", (lease_id,)) as cur:
            row = await cur.fetchone()
        return Lease.model_validate_json(row["data"]) if row else None

    async def list_leases(
        self, owner: str | None = None, states: Iterable[LeaseState] | None = None
    ) -> list[Lease]:
        sql, args = "SELECT data FROM leases WHERE 1=1", []
        if owner is not None:
            sql += " AND owner = ?"
            args.append(owner)
        if states is not None:
            states = list(states)
            sql += f" AND state IN ({','.join('?' * len(states))})"
            args.extend(s.value for s in states)
        sql += " ORDER BY created_at, id"
        async with self.db.execute(sql, args) as cur:
            rows = await cur.fetchall()
        return [Lease.model_validate_json(r["data"]) for r in rows]

    # ---- users --------------------------------------------------------------

    async def add_user(self, user: User) -> None:
        try:
            await self.db.execute(
                "INSERT INTO users (name, key_hash, is_admin, max_leases, created_at, revoked)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    user.name,
                    user.key_hash,
                    int(user.is_admin),
                    user.max_leases,
                    user.created_at.isoformat(),
                    int(user.revoked),
                ),
            )
        except sqlite3.IntegrityError as e:
            raise ValueError(f"user {user.name} already exists") from e
        await self.db.commit()

    async def _user_where(self, clause: str, arg) -> User | None:
        async with self.db.execute(f"SELECT * FROM users WHERE {clause}", (arg,)) as cur:
            row = await cur.fetchone()
        return _row_user(row) if row else None

    async def get_user(self, name: str) -> User | None:
        return await self._user_where("name = ?", name)

    async def get_user_by_hash(self, key_hash: str) -> User | None:
        return await self._user_where("key_hash = ?", key_hash)

    async def list_users(self) -> list[User]:
        async with self.db.execute("SELECT * FROM users ORDER BY name") as cur:
            return [_row_user(r) for r in await cur.fetchall()]

    async def revoke_user(self, name: str) -> bool:
        cur = await self.db.execute("UPDATE users SET revoked = 1 WHERE name = ?", (name,))
        await self.db.commit()
        return cur.rowcount > 0


def _row_user(row) -> User:
    return User(
        name=row["name"],
        key_hash=row["key_hash"],
        is_admin=bool(row["is_admin"]),
        max_leases=row["max_leases"],
        created_at=row["created_at"],
        revoked=bool(row["revoked"]),
    )
