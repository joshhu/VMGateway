"""Admin self-service API keys over REST and MCP (no SSH to the gateway needed)."""

import json

import pytest

from vmgateway.auth import create_user, install_snippets
from vmgateway.errors import GatewayError

from .test_api import auth, client  # noqa: F401  (fixture reuse)
from .test_mcp import gw, mcp_client, payload  # noqa: F401


def test_install_snippets():
    s = install_snippets("http://gw.lan:8000/", "vmgw_abc")
    assert s["claude_code"] == (
        "claude mcp add --transport http --scope user vmgw http://gw.lan:8000/mcp"
        ' --header "Authorization: Bearer vmgw_abc"'
    )
    vscode = json.loads(s["vscode_mcp_json"])
    assert vscode["servers"]["vmgw"]["headers"]["Authorization"] == "Bearer vmgw_abc"
    assert json.loads(s["cursor_mcp_json"])["mcpServers"]["vmgw"]["url"] == "http://gw.lan:8000/mcp"
    desktop = json.loads(s["claude_desktop_json"])["mcpServers"]["vmgw"]
    assert "--allow-http" in desktop["args"] and desktop["env"]["VMGW_AUTH"] == "Bearer vmgw_abc"


async def test_create_user_validates_name(tmp_path):
    from vmgateway.store import Store

    store = Store(tmp_path / "u.db")
    await store.init()
    try:
        with pytest.raises(GatewayError, match="name"):
            await create_user(store, "bad name!")
        key = await create_user(store, "laptop")
        assert key.startswith("vmgw_")
        with pytest.raises(GatewayError) as e:
            await create_user(store, "laptop")
        assert e.value.status == 409
    finally:
        await store.close()


async def test_rest_user_admin(client):  # noqa: F811
    c = client
    r = await c.post("/v1/users", json={"name": "laptop", "max_leases": 2}, headers=auth(c, "root"))
    assert r.status_code == 201, r.text
    body = r.json()
    key = body["api_key"]
    assert key.startswith("vmgw_") and "claude mcp add" in body["install"]["claude_code"]

    # The new key works immediately.
    new = {"Authorization": f"Bearer {key}"}
    assert (await c.get("/v1/templates", headers=new)).status_code == 200

    names = {u["name"] for u in (await c.get("/v1/users", headers=auth(c, "root"))).json()}
    assert {"alice", "bob", "root", "laptop"} <= names

    # Non-admins can't manage keys.
    assert (await c.post("/v1/users", json={"name": "x"}, headers=auth(c, "alice"))).status_code == 403
    assert (await c.get("/v1/users", headers=auth(c, "alice"))).status_code == 403

    assert (await c.delete("/v1/users/laptop", headers=auth(c, "root"))).status_code == 200
    assert (await c.get("/v1/templates", headers=new)).status_code == 401
    assert (await c.delete("/v1/users/nobody", headers=auth(c, "root"))).status_code == 404


async def test_cannot_revoke_last_admin(client):  # noqa: F811
    r = await client.delete("/v1/users/root", headers=auth(client, "root"))
    assert r.status_code == 409 and r.json()["error"] == "last_admin"


async def test_mcp_create_and_revoke_key(gw):  # noqa: F811
    app, keys = gw
    store = app.state.manager.store
    from datetime import UTC, datetime

    from vmgateway.auth import generate_key, hash_key
    from vmgateway.models import User

    admin_key = generate_key()
    await store.add_user(
        User(name="boss", key_hash=hash_key(admin_key), is_admin=True, created_at=datetime.now(UTC))
    )

    async with mcp_client(app, admin_key) as c:
        out = payload(await c.call_tool("create_api_key", {"name": "laptop"}))
        assert out["api_key"].startswith("vmgw_") and "claude_code" in out["install"]
        listed = payload(await c.call_tool("list_api_keys", {}))
        assert any(u["name"] == "laptop" and not u["revoked"] for u in listed)
        done = payload(await c.call_tool("revoke_api_key", {"name": "laptop"}))
        assert done["revoked"] is True

    async with mcp_client(app, keys["alice"]) as c:
        r = await c.call_tool("create_api_key", {"name": "sneaky"})
        assert r.is_error
