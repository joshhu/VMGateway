"""Per-user API keys. Only sha256 hashes are stored."""

from __future__ import annotations

import hashlib
import secrets

from mcp.server.auth.provider import AccessToken

from .errors import GatewayError
from .models import User
from .store import Store


def generate_key() -> str:
    return "vmgw_" + secrets.token_urlsafe(32)


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


async def lookup(store: Store, key: str) -> User | None:
    user = await store.get_user_by_hash(hash_key(key))
    if user is None or user.revoked:
        return None
    return user


class ApiKeyVerifier:
    """MCP TokenVerifier backed by the user table."""

    def __init__(self, store: Store):
        self.store = store

    async def verify_token(self, token: str) -> AccessToken | None:
        user = await lookup(self.store, token)
        if user is None:
            return None
        return AccessToken(
            token=token, client_id=user.name, scopes=["admin" if user.is_admin else "user"], subject=user.name
        )


async def authenticate(store: Store, authorization: str | None) -> User:
    if not authorization or not authorization.startswith("Bearer "):
        raise GatewayError("unauthorized", "missing Bearer token", 401)
    user = await lookup(store, authorization.removeprefix("Bearer ").strip())
    if user is None:
        raise GatewayError("unauthorized", "invalid or revoked API key", 401)
    return user
