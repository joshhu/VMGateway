"""Per-user API keys. Only sha256 hashes are stored."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import UTC, datetime

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


# ---- key management (CLI, REST and MCP share these) ------------------------------------------------

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def require_admin(user: User) -> None:
    if not user.is_admin:
        raise GatewayError("forbidden", "only admins can manage API keys", 403)


async def create_user(store: Store, name: str, admin: bool = False, max_leases: int | None = None) -> str:
    """Create a user and return its API key (the only time the plain key exists)."""
    if not _NAME.match(name):
        raise GatewayError(
            "invalid_name", "name: 1-64 of A-Z a-z 0-9 . _ - (starting with a letter or digit)"
        )
    if max_leases is not None and not 0 < max_leases <= 1000:
        raise GatewayError("invalid_quota", "max_leases must be 1-1000")
    key = generate_key()
    user = User(
        name=name, key_hash=hash_key(key), is_admin=admin, max_leases=max_leases, created_at=datetime.now(UTC)
    )
    try:
        await store.add_user(user)
    except ValueError as e:
        raise GatewayError(
            "exists", f"user {name} already exists (revoke it or pick another name)", 409
        ) from e
    return key


async def revoke_user(store: Store, name: str) -> None:
    user = await store.get_user(name)
    if user is None:
        raise GatewayError("not_found", f"no user {name}", 404)
    if user.is_admin and not user.revoked:
        admins = [u for u in await store.list_users() if u.is_admin and not u.revoked]
        if len(admins) <= 1:
            raise GatewayError("last_admin", "refusing to revoke the last active admin", 409)
    await store.revoke_user(name)


def user_view(u: User) -> dict:
    return {
        "name": u.name,
        "admin": u.is_admin,
        "max_leases": u.max_leases,
        "revoked": u.revoked,
        "created_at": u.created_at.isoformat(),
    }


def install_snippets(public_url: str, key: str) -> dict[str, str]:
    """Ready-to-paste client configuration for a new key."""
    url = public_url.rstrip("/") + "/mcp"
    auth = f"Bearer {key}"
    return {
        "claude_code": (
            f'claude mcp add --transport http --scope user vmgw {url} --header "Authorization: {auth}"'
        ),
        "vscode_mcp_json": json.dumps(
            {"servers": {"vmgw": {"type": "http", "url": url, "headers": {"Authorization": auth}}}}, indent=2
        ),
        "cursor_mcp_json": json.dumps(
            {"mcpServers": {"vmgw": {"url": url, "headers": {"Authorization": auth}}}}, indent=2
        ),
        "claude_desktop_json": json.dumps(
            {
                "mcpServers": {
                    "vmgw": {
                        "command": "npx",
                        "args": ["mcp-remote", url, "--allow-http", "--header", "Authorization:${VMGW_AUTH}"],
                        "env": {"VMGW_AUTH": auth},
                    }
                }
            },
            indent=2,
        ),
    }
