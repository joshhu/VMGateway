import re
from datetime import UTC, datetime

import pytest

from vmgateway.auth import ApiKeyVerifier, authenticate, generate_key, hash_key
from vmgateway.config import CredentialsConfig
from vmgateway.creds import make_credentials
from vmgateway.errors import GatewayError
from vmgateway.models import User
from vmgateway.store import Store


@pytest.fixture
async def store(tmp_path):
    s = Store(tmp_path / "a.db")
    await s.init()
    yield s
    await s.close()


def test_generate_and_hash():
    k = generate_key()
    assert k.startswith("vmgw_") and len(k) >= 40
    assert generate_key() != k
    assert re.fullmatch(r"[0-9a-f]{64}", hash_key(k))


async def test_verifier(store):
    key = generate_key()
    await store.add_user(
        User(name="alice", key_hash=hash_key(key), is_admin=True, created_at=datetime.now(UTC))
    )
    v = ApiKeyVerifier(store)
    tok = await v.verify_token(key)
    assert tok.client_id == "alice" and "admin" in tok.scopes
    assert await v.verify_token("vmgw_wrong") is None
    await store.revoke_user("alice")
    assert await v.verify_token(key) is None


async def test_authenticate_header(store):
    key = generate_key()
    await store.add_user(User(name="bob", key_hash=hash_key(key), created_at=datetime.now(UTC)))
    assert (await authenticate(store, f"Bearer {key}")).name == "bob"
    for bad in (None, "", "Basic x", "Bearer nope"):
        with pytest.raises(GatewayError) as e:
            await authenticate(store, bad)
        assert e.value.status == 401


@pytest.mark.parametrize("mode", ["cloudinit", "unattend"])
def test_generated_credentials(mode):
    c = make_credentials(CredentialsConfig(mode=mode, username="agent"))
    assert c.username == "agent"
    pw = c.password
    assert len(pw) == 20
    # Windows complexity: upper, lower, digit, symbol
    assert re.search(r"[A-Z]", pw) and re.search(r"[a-z]", pw) and re.search(r"\d", pw)
    assert re.search(r"[-_.!]", pw)
    assert c.ssh_public_key.startswith("ssh-ed25519 ")
    assert "OPENSSH PRIVATE KEY" in c.ssh_private_key


def test_static_credentials(monkeypatch):
    monkeypatch.setenv("T_PW", "fixed")
    c = make_credentials(CredentialsConfig(mode="static", username="admin", password_env="T_PW"))
    assert (c.username, c.password, c.ssh_private_key) == ("admin", "fixed", None)
