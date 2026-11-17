from datetime import UTC, datetime, timedelta

import pytest

from vmgateway.backends import build_backends
from vmgateway.errors import GatewayError
from vmgateway.leases import LeaseManager
from vmgateway.models import Lease, LeaseState, Resources, User, VMRef
from vmgateway.store import Store

from .conftest import make_config

T0 = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


def user(name="alice", admin=False, max_leases=None):
    return User(name=name, key_hash=name, is_admin=admin, max_leases=max_leases, created_at=T0)


@pytest.fixture
async def env(tmp_path):
    config = make_config()
    store = Store(tmp_path / "l.db")
    await store.init()
    backends = build_backends(config)
    clock = Clock()
    mgr = LeaseManager(config, store, backends, now=clock)
    yield mgr, backends, clock, store
    await mgr.stop()
    await store.close()


async def ready(mgr, u, **kw):
    lz = await mgr.create(u, "ubuntu", **kw)
    return await mgr.wait(u, lz.id, timeout=5)


async def test_create_then_ready(env):
    mgr, backends, _, _ = env
    alice = user()
    lz = await mgr.create(alice, "ubuntu")
    assert lz.state == LeaseState.PROVISIONING and len(lz.id) == 8
    lz = await mgr.wait(alice, lz.id, timeout=5)
    assert lz.state == LeaseState.READY
    assert lz.access.ip.startswith("10.0.0.")
    assert lz.credentials.username == "agent" and lz.credentials.password
    assert lz.ref.name == f"vmgw-{lz.id}"
    assert f"vmgw-{lz.id}" in backends["pve"].vms
    assert lz.expires_at == T0 + timedelta(minutes=120)


async def test_unknown_template(env):
    mgr, *_ = env
    with pytest.raises(GatewayError) as e:
        await mgr.create(user(), "nope")
    assert (e.value.code, e.value.status) == ("unknown_template", 404)


async def test_resources_defaults_and_disk_clamp(env):
    mgr, backends, _, _ = env
    lz = await ready(mgr, user(), cpu=4, memory_gb=8, disk_gb=5)
    assert lz.resources == Resources(cpu=4, memory_gb=8, disk_gb=20)
    assert backends["pve"].vms[lz.ref.vm_id]["resources"].memory_gb == 8


@pytest.mark.parametrize("ttl", [5, 4321])
async def test_ttl_bounds(env, ttl):
    mgr, *_ = env
    with pytest.raises(GatewayError) as e:
        await mgr.create(user(), "ubuntu", ttl_minutes=ttl)
    assert e.value.code == "invalid_ttl"


async def test_ttl_max_is_three_days(env):
    mgr, *_ = env
    lz = await mgr.create(user(), "ubuntu", ttl_minutes=4320)
    assert lz.expires_at == T0 + timedelta(days=3)


async def test_provision_failure_marks_failed_and_cleans_up(env):
    mgr, backends, _, _ = env
    backends["pve"].fail_on.add("provision")
    lz = await mgr.create(user(), "ubuntu")
    lz = await mgr.wait(user(), lz.id, timeout=5)
    assert lz.state == LeaseState.FAILED and "fake provision failure" in lz.error
    assert backends["pve"].vms == {}


async def test_wait_failure_cleans_up(env):
    mgr, backends, _, _ = env
    backends["pve"].fail_on.add("wait")
    lz = await mgr.create(user(), "ubuntu")
    lz = await mgr.wait(user(), lz.id, timeout=5)
    assert lz.state == LeaseState.FAILED
    assert backends["pve"].vms == {}


async def test_renew(env):
    mgr, _, clock, _ = env
    alice = user()
    lz = await ready(mgr, alice)
    clock.t = T0 + timedelta(hours=1)
    lz = await mgr.renew(alice, lz.id, 600)
    assert lz.expires_at == clock.t + timedelta(minutes=600)
    with pytest.raises(GatewayError) as e:
        await mgr.renew(alice, lz.id, 5000)
    assert e.value.code == "invalid_ttl"


async def test_renew_inactive_conflict(env):
    mgr, *_ = env
    alice = user()
    lz = await ready(mgr, alice)
    await mgr.destroy(alice, lz.id)
    with pytest.raises(GatewayError) as e:
        await mgr.renew(alice, lz.id, 60)
    assert e.value.status == 409


async def test_destroy(env):
    mgr, backends, _, _ = env
    alice = user()
    lz = await ready(mgr, alice)
    lz = await mgr.destroy(alice, lz.id)
    assert lz.state == LeaseState.RELEASED
    assert backends["pve"].vms == {}
    assert (await mgr.destroy(alice, lz.id)).state == LeaseState.RELEASED  # idempotent


async def test_destroy_failure_retried_by_reaper(env):
    mgr, backends, _, _ = env
    alice = user()
    lz = await ready(mgr, alice)
    backends["pve"].fail_on.add("destroy")
    lz = await mgr.destroy(alice, lz.id)
    assert lz.state == LeaseState.RELEASING and lz.error
    backends["pve"].fail_on.clear()
    await mgr.reap()
    assert (await mgr.get(alice, lz.id)).state == LeaseState.RELEASED


async def test_destroy_while_provisioning(env):
    mgr, backends, _, _ = env
    backends["pve"].delay = 0.3
    alice = user()
    lz = await mgr.create(alice, "ubuntu")
    lz = await mgr.destroy(alice, lz.id)
    assert lz.state == LeaseState.RELEASED
    assert backends["pve"].vms == {}


async def test_reaper_expires(env):
    mgr, backends, clock, _ = env
    alice = user()
    lz = await ready(mgr, alice, ttl_minutes=30)
    clock.t = T0 + timedelta(minutes=29)
    await mgr.reap()
    assert (await mgr.get(alice, lz.id)).state == LeaseState.READY
    clock.t = T0 + timedelta(minutes=31)
    await mgr.reap()
    assert (await mgr.get(alice, lz.id)).state == LeaseState.EXPIRED
    assert backends["pve"].vms == {}


async def test_reconcile_on_start(env):
    mgr, backends, _, store = env
    fake = backends["pve"]
    # A lease left in provisioning by a crashed process, with its VM half-built.
    stuck = Lease(
        id="dead0001",
        owner="alice",
        template_id="ubuntu",
        state=LeaseState.PROVISIONING,
        resources=Resources(cpu=2, memory_gb=4, disk_gb=20),
        created_at=T0,
        expires_at=T0 + timedelta(hours=1),
        host="pve",
        node="n1",
    )
    await store.insert_lease(stuck)
    fake.vms["vmgw-dead0001"] = {"node": "n1", "resources": stuck.resources, "running": True, "macos": False}
    fake.vms["vmgw-orphan01"] = {"node": "n2", "resources": stuck.resources, "running": True, "macos": False}
    fake.vms["not-ours"] = {"node": "n2", "resources": stuck.resources, "running": True, "macos": False}
    live = await ready(mgr, user())  # an active lease that must survive
    await mgr.reconcile(startup=True)
    assert (await store.get_lease("dead0001")).state == LeaseState.FAILED
    # The orphan survives the first pass (it might be mid-creation by someone else)...
    assert set(fake.vms) == {"not-ours", live.ref.vm_id, "vmgw-orphan01"}
    await mgr.reconcile()
    # ...and is removed on the second.
    assert set(fake.vms) == {"not-ours", live.ref.vm_id}


async def test_quota(env):
    mgr, *_ = env
    bob = user("bob", max_leases=1)
    await ready(mgr, bob)
    with pytest.raises(GatewayError) as e:
        await mgr.create(bob, "ubuntu")
    assert (e.value.code, e.value.status) == ("quota_exceeded", 429)


async def test_visibility(env):
    mgr, *_ = env
    alice, bob, root = user("alice"), user("bob"), user("root", admin=True)
    lz = await ready(mgr, alice)
    with pytest.raises(GatewayError) as e:
        await mgr.get(bob, lz.id)
    assert e.value.status == 404
    with pytest.raises(GatewayError):
        await mgr.destroy(bob, lz.id)
    assert (await mgr.get(root, lz.id)).id == lz.id
    assert [x.id for x in await mgr.list(bob)] == []
    assert [x.id for x in await mgr.list(root, all_users=True)] == [lz.id]


async def test_macos_reservation_prevents_overbooking(env):
    mgr, backends, _, _ = env
    backends["mac"].delay = 0.2
    alice = user(max_leases=10)
    await mgr.create(alice, "macos")
    await mgr.create(alice, "macos")
    with pytest.raises(GatewayError) as e:
        await mgr.create(alice, "macos")
    assert e.value.code == "no_capacity"


async def test_wait_timeout_returns_current(env):
    mgr, backends, _, _ = env
    backends["pve"].delay = 1
    alice = user()
    lz = await mgr.create(alice, "ubuntu")
    got = await mgr.wait(alice, lz.id, timeout=0.1)
    assert got.state == LeaseState.PROVISIONING


def test_vmref_name_default():
    assert VMRef(host="h", node="n", vm_id="1").name == ""
