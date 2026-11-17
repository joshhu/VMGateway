from datetime import UTC, datetime, timedelta

import pytest

from vmgateway.models import (
    ACTIVE_STATES,
    Access,
    Credentials,
    Endpoint,
    Lease,
    LeaseState,
    Resources,
    User,
    VMRef,
)
from vmgateway.store import Store

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)


def lease(id="abc12345", owner="alice", state=LeaseState.PROVISIONING, **kw):
    return Lease(
        id=id,
        owner=owner,
        template_id="ubuntu-24.04",
        state=state,
        resources=Resources(cpu=2, memory_gb=4, disk_gb=20),
        created_at=NOW,
        expires_at=NOW + timedelta(hours=2),
        **kw,
    )


@pytest.fixture
async def store(tmp_path):
    s = Store(tmp_path / "t.db")
    await s.init()
    yield s
    await s.close()


async def test_lease_roundtrip_all_fields(store):
    full = lease(
        host="pve",
        ref=VMRef(host="pve", node="n1", vm_id="105", extra={"rdp": "40001"}),
        access=Access(ip="10.0.0.9", endpoints=[Endpoint(proto="ssh", host="10.0.0.9", port=22)]),
        credentials=Credentials(username="agent", password="pw", ssh_private_key="PRIV"),
        error=None,
    )
    await store.insert_lease(full)
    assert await store.get_lease("abc12345") == full


async def test_get_missing_returns_none(store):
    assert await store.get_lease("nope") is None


async def test_update_lease(store):
    await store.insert_lease(lease())
    lz = await store.get_lease("abc12345")
    lz.state = LeaseState.READY
    lz.access = Access(ip="1.2.3.4", endpoints=[])
    await store.update_lease(lz)
    got = await store.get_lease("abc12345")
    assert got.state == LeaseState.READY and got.access.ip == "1.2.3.4"


async def test_list_leases_filters(store):
    await store.insert_lease(lease("a0000001", "alice", LeaseState.READY))
    await store.insert_lease(lease("a0000002", "alice", LeaseState.RELEASED))
    await store.insert_lease(lease("b0000001", "bob", LeaseState.PROVISIONING))
    active = await store.list_leases(states=ACTIVE_STATES)
    assert {x.id for x in active} == {"a0000001", "b0000001"}
    alice = await store.list_leases(owner="alice")
    assert {x.id for x in alice} == {"a0000001", "a0000002"}
    assert [x.id for x in await store.list_leases(owner="alice", states=ACTIVE_STATES)] == ["a0000001"]


async def test_users(store):
    u = User(name="alice", key_hash="h1", is_admin=True, max_leases=3, created_at=NOW)
    await store.add_user(u)
    assert await store.get_user_by_hash("h1") == u
    assert await store.get_user("alice") == u
    await store.revoke_user("alice")
    assert (await store.get_user("alice")).revoked
    assert [x.name for x in await store.list_users()] == ["alice"]


async def test_duplicate_user_rejected(store):
    u = User(name="alice", key_hash="h1", created_at=NOW)
    await store.add_user(u)
    with pytest.raises(ValueError, match="alice"):
        await store.add_user(u)
