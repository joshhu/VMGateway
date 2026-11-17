"""`vmgw` command line: run the server and manage API keys."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

import typer

from .auth import generate_key, hash_key
from .config import ConfigError, GatewayConfig, load_config
from .models import User
from .store import Store

app = typer.Typer(help="VMGateway: lease VMs to coding agents.", no_args_is_help=True)
user_app = typer.Typer(help="Manage API keys.", no_args_is_help=True)
app.add_typer(user_app, name="user")

ConfigOpt = typer.Option(
    Path(os.environ.get("VMGW_CONFIG", "config.yaml")),
    "--config",
    "-c",
    help="config.yaml path ($VMGW_CONFIG)",
)


def _load_dotenv(path: Path = Path(".env")) -> None:
    """Minimal .env loader (KEY=VALUE lines); real environment variables win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _config(path: Path) -> GatewayConfig:
    _load_dotenv()
    try:
        return load_config(path)
    except ConfigError as e:
        typer.secho(f"config error: {e}", fg="red", err=True)
        raise typer.Exit(2) from e


def _store_call(config: GatewayConfig, fn):
    async def run():
        s = Store(config.server.db_path)
        await s.init()
        try:
            return await fn(s)
        finally:
            await s.close()

    return asyncio.run(run())


@app.command()
def serve(config: Path = ConfigOpt, log_level: str = "info"):
    """Run the gateway (REST /v1, MCP /mcp)."""
    import uvicorn

    from .app import create_app

    logging.basicConfig(level=log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("asyncssh").setLevel(logging.WARNING)  # it logs every channel at INFO
    cfg = _config(config)
    uvicorn.run(create_app(cfg), host=cfg.server.listen, port=cfg.server.port, log_level=log_level)


@user_app.command("add")
def user_add(
    name: str,
    admin: bool = typer.Option(False, help="May see and manage everyone's leases"),
    max_leases: int | None = typer.Option(None, help="Concurrent lease limit (default: policy)"),
    config: Path = ConfigOpt,
):
    """Create a user and print its API key (shown once)."""
    cfg = _config(config)
    key = generate_key()
    user = User(
        name=name, key_hash=hash_key(key), is_admin=admin, max_leases=max_leases, created_at=datetime.now(UTC)
    )
    try:
        _store_call(cfg, lambda s: s.add_user(user))
    except ValueError as e:
        typer.secho(str(e), fg="red", err=True)
        raise typer.Exit(1) from e
    typer.echo(key)


@user_app.command("list")
def user_list(config: Path = ConfigOpt):
    cfg = _config(config)
    for u in _store_call(cfg, lambda s: s.list_users()):
        flags = ",".join(f for f, on in [("admin", u.is_admin), ("revoked", u.revoked)] if on)
        typer.echo(
            f"{u.name}\t{flags or '-'}\tmax_leases={u.max_leases or 'default'}\t{u.created_at:%Y-%m-%d}"
        )


@user_app.command("revoke")
def user_revoke(name: str, config: Path = ConfigOpt):
    cfg = _config(config)
    if not _store_call(cfg, lambda s: s.revoke_user(name)):
        typer.secho(f"no such user {name}", fg="red", err=True)
        raise typer.Exit(1)
    typer.echo(f"revoked {name}")
