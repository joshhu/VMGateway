import asyncio
from contextlib import asynccontextmanager

import pytest

from vmgateway.config import GatewayConfig


def make_config(**policy) -> GatewayConfig:
    return GatewayConfig.model_validate(
        {
            "policy": policy,
            "hosts": [
                {"name": "pve", "type": "fake", "nodes": ["n1", "n2"], "free_memory_gb": 32},
                {"name": "mac", "type": "fake", "nodes": ["mac"], "free_memory_gb": 24},
            ],
            "templates": [
                {
                    "id": "ubuntu",
                    "description": "Ubuntu 24.04",
                    "os": "linux",
                    "arch": "amd64",
                    "host_type": "fake",
                    "sources": [
                        {"host": "pve", "node": "n1", "ref": "9024"},
                        {"host": "pve", "node": "n2", "ref": "9124"},
                    ],
                    "defaults": {"cpu": 2, "memory_gb": 4, "disk_gb": 20},
                    "credentials": {"mode": "cloudinit", "username": "agent"},
                },
                {
                    "id": "macos",
                    "description": "macOS",
                    "os": "macos",
                    "arch": "arm64",
                    "host_type": "fake",
                    "sources": [{"host": "mac", "node": "mac", "ref": "tpl"}],
                    "defaults": {"cpu": 4, "memory_gb": 8, "disk_gb": 100},
                    "credentials": {"mode": "cloudinit", "username": "admin"},
                    "endpoints": ["ssh", "vnc"],
                },
            ],
        }
    )


@pytest.fixture
def config():
    return make_config()


@asynccontextmanager
async def running(app):
    """Run the app lifespan inside one task (anyio cancel scopes must exit where they were entered)."""
    started, stop = asyncio.Event(), asyncio.Event()

    async def runner():
        async with app.router.lifespan_context(app):
            started.set()
            await stop.wait()

    task = asyncio.create_task(runner())
    await asyncio.wait({task, asyncio.create_task(started.wait())}, return_when=asyncio.FIRST_COMPLETED)
    if task.done():
        task.result()
    try:
        yield
    finally:
        stop.set()
        await task
