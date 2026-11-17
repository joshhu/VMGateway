"""ASGI app: REST under /v1, MCP at /mcp, background reaper."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from mcp.server.transport_security import TransportSecuritySettings

from . import __version__
from .api import install
from .backends import Backend, build_backends
from .config import GatewayConfig
from .leases import LeaseManager
from .mcp_server import build_mcp
from .store import Store


def create_app(config: GatewayConfig, backends: dict[str, Backend] | None = None) -> FastAPI:
    store = Store(config.server.db_path)
    manager = LeaseManager(config, store, backends if backends is not None else build_backends(config))
    mcp = build_mcp(manager)
    mcp_app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=config.server.allowed_hosts,
            allowed_origins=[f"http://{h}" for h in config.server.allowed_hosts],
        ),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await store.init()
        await manager.start()
        try:
            async with mcp.session_manager.run():
                yield
        finally:
            await manager.stop()
            await store.close()

    app = FastAPI(title="VMGateway", version=__version__, lifespan=lifespan)
    app.state.manager = manager
    app.state.mcp = mcp
    install(app)
    app.mount("/", mcp_app)
    return app
