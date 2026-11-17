"""End-to-end MCP over Streamable HTTP (in-process ASGI), with real Bearer-key auth."""

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx2
import pytest
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

from vmgateway.app import create_app
from vmgateway.auth import generate_key, hash_key
from vmgateway.models import User

from .conftest import make_config, running


@pytest.fixture
async def gw(tmp_path):
    config = make_config()
    config.server.db_path = str(tmp_path / "mcp.db")
    app = create_app(config)
    async with running(app):
        keys = {}
        for name in ("alice", "bob"):
            keys[name] = generate_key()
            await app.state.manager.store.add_user(
                User(name=name, key_hash=hash_key(keys[name]), created_at=datetime.now(UTC))
            )
        yield app, keys


@asynccontextmanager
async def mcp_client(app, key):
    http = httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app),
        base_url="http://127.0.0.1:8000",
        headers={"Authorization": f"Bearer {key}"},
    )
    async with http, Client(streamable_http_client("http://127.0.0.1:8000/mcp", http_client=http)) as c:
        yield c


def payload(result):
    assert not result.is_error, result.content
    if result.structured_content is not None:
        sc = result.structured_content
        return sc.get("result", sc) if isinstance(sc, dict) and set(sc) == {"result"} else sc
    return json.loads(result.content[0].text)


async def test_tools_listed_with_template_catalog(gw):
    app, keys = gw
    async with mcp_client(app, keys["alice"]) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
    assert set(tools) == {
        "list_templates",
        "list_hosts",
        "create_vm",
        "get_vm",
        "list_vms",
        "renew_vm",
        "destroy_vm",
    }
    assert "ubuntu: Ubuntu 24.04" in tools["create_vm"].description


async def test_create_get_destroy(gw):
    app, keys = gw
    async with mcp_client(app, keys["alice"]) as c:
        vm = payload(await c.call_tool("create_vm", {"template": "ubuntu", "memory_gb": 8}))
        assert vm["state"] == "ready" and vm["resources"]["memory_gb"] == 8
        assert "OPENSSH PRIVATE KEY" in vm["credentials"]["ssh_private_key"]
        assert payload(await c.call_tool("list_vms", {}))[0]["id"] == vm["id"]
        got = payload(await c.call_tool("get_vm", {"lease_id": vm["id"]}))
        assert got["ip"] == vm["ip"]
        done = payload(await c.call_tool("destroy_vm", {"lease_id": vm["id"]}))
        assert done["state"] == "released"

    async with mcp_client(app, keys["bob"]) as c:
        r = await c.call_tool("get_vm", {"lease_id": vm["id"]})
        assert r.is_error


async def test_bad_key_rejected(gw):
    app, _ = gw
    with pytest.raises(BaseExceptionGroup) as ei:
        async with mcp_client(app, "vmgw_bogus") as c:
            await c.list_tools()
    assert "Server returned an error response" in repr(ei.value)  # the 401, as the client reports it
