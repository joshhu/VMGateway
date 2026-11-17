"""Per-lease guest credentials."""

from __future__ import annotations

import re
import secrets
import string

import asyncssh

from .config import CredentialsConfig
from .errors import GatewayError
from .models import Credentials

_SYMBOLS = "-_.!"
_ALPHABET = string.ascii_letters + string.digits + _SYMBOLS


def random_password(length: int = 20) -> str:
    """Password that satisfies Windows complexity and is safe in XML, shell and YAML."""
    while True:
        pw = "".join(secrets.choice(_ALPHABET) for _ in range(length))
        if (
            any(c.isupper() for c in pw)
            and any(c.islower() for c in pw)
            and any(c.isdigit() for c in pw)
            and any(c in _SYMBOLS for c in pw)
            and pw[0].isalnum()
        ):
            return pw


_USERNAME = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,19}$")


def validate_custom(os_: str, username: str | None, password: str | None) -> None:
    """Reject user-chosen credentials the guest OS would refuse (before any VM is created)."""

    def bad(detail: str):
        raise GatewayError("invalid_credentials", detail, 400)

    if username is not None and not _USERNAME.match(username):
        bad("username must start with a letter and use 1-20 of A-Z a-z 0-9 . _ -")
    if password is None:
        return
    if not 8 <= len(password) <= 64:
        bad("password must be 8-64 characters")
    if not all(c.isprintable() for c in password):  # rejects tabs/newlines too
        bad("password must be printable characters")
    if os_ == "windows":
        classes = sum(
            [
                any(c.isupper() for c in password),
                any(c.islower() for c in password),
                any(c.isdigit() for c in password),
                any(not c.isalnum() for c in password),
            ]
        )
        if classes < 3:
            bad("Windows password complexity: use 3 of upper, lower, digit, symbol")
        if username and username.lower() in password.lower():
            bad("Windows password must not contain the username")


def make_credentials(
    cfg: CredentialsConfig, username: str | None = None, password: str | None = None
) -> Credentials:
    """Credentials the VM is created with.

    cloudinit/unattend: custom values are baked in at creation. static: always the template's
    account; custom values are applied after boot by the backend (see Backend.set_credentials).
    """
    if cfg.mode == "static":
        return Credentials(username=cfg.username, password=cfg.password.get_secret_value())
    key = asyncssh.generate_private_key("ssh-ed25519", comment="vmgateway")
    return Credentials(
        username=username or cfg.username,
        password=password or random_password(),
        ssh_private_key=key.export_private_key().decode(),
        ssh_public_key=key.export_public_key().decode().strip(),
    )
