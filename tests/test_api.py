from datetime import UTC, datetime

import httpx
import pytest

from vmgateway.app import create_app
from vmgateway.auth import generate_key, hash_key
from vmgateway.models import User

from .conftest import make_config, running


@pytest.fixture
async def client(tmp_path):
    config = make_config()
    config.server.db_path = str(tmp_path / "api.db")
    app = create_app(config)
    async with running(app):
        store = app.state.manager.store
        keys = {}
        for name, admin in [("alice", False), ("bob", False), ("root", True)]:
            keys[name] = generate_key()
            await store.add_user(
                User(name=name, key_hash=hash_key(keys[name]), is_admin=admin, created_at=datetime.now(UTC))
            )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as c:
            c.keys = keys
            c.app = app
            yield c


def auth(c, who="alice"):
    return {"Authorization": f"Bearer {c.keys[who]}"}


async def test_healthz_no_auth(client):
    assert (await client.get("/healthz")).json() == {"ok": True}


async def test_requires_auth(client):
    r = await client.get("/v1/templates")
    assert r.status_code == 401 and r.json()["error"] == "unauthorized"


async def test_templates(client):
    r = await client.get("/v1/templates", headers=auth(client))
    assert {t["id"] for t in r.json()} == {"ubuntu", "macos"}


async def test_hosts(client):
    r = await client.get("/v1/hosts", headers=auth(client))
    pve = next(h for h in r.json() if h["name"] == "pve")
    assert pve["status"] == "ok" and "ubuntu" in pve["templates"]


async def test_lease_lifecycle(client):
    r = await client.post("/v1/leases", json={"template": "ubuntu", "wait": True}, headers=auth(client))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["state"] == "ready" and body["ip"]
    assert body["credentials"]["ssh_private_key"] == "(request with include_key)"
    assert body["connect"][0].startswith("ssh -i")
    lid = body["id"]

    r = await client.get(f"/v1/leases/{lid}?include_key=true", headers=auth(client))
    assert "OPENSSH PRIVATE KEY" in r.json()["credentials"]["ssh_private_key"]

    assert (await client.get(f"/v1/leases/{lid}", headers=auth(client, "bob"))).status_code == 404
    assert [x["id"] for x in (await client.get("/v1/leases", headers=auth(client))).json()] == [lid]
    assert (await client.get("/v1/leases?all=true", headers=auth(client, "bob"))).json() == []
    assert len((await client.get("/v1/leases?all=true", headers=auth(client, "root"))).json()) == 1

    r = await client.post(f"/v1/leases/{lid}/renew", json={"ttl_minutes": 600}, headers=auth(client))
    assert r.status_code == 200

    r = await client.delete(f"/v1/leases/{lid}", headers=auth(client))
    assert r.json()["state"] == "released"
    assert (await client.get("/v1/leases", headers=auth(client))).json() == []


async def test_error_shape(client):
    r = await client.post("/v1/leases", json={"template": "nope"}, headers=auth(client))
    assert r.status_code == 404 and r.json()["error"] == "unknown_template"
    r = await client.post("/v1/leases", json={"template": "ubuntu", "ttl_minutes": 1}, headers=auth(client))
    assert r.status_code == 400 and r.json()["error"] == "invalid_ttl"


async def test_mcp_requires_auth(client):
    r = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Accept": "application/json, text/event-stream"},
    )
    assert r.status_code == 401
