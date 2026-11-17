import pytest

from vmgateway.backends import build_backends
from vmgateway.config import Resources
from vmgateway.errors import GatewayError
from vmgateway.scheduler import Reservation, Scheduler


@pytest.fixture
def setup(config):
    backends = build_backends(config)
    return config, backends, Scheduler(backends)


def res(mem=4):
    return Resources(cpu=2, memory_gb=mem, disk_gb=20)


async def test_picks_node_with_most_free_memory(setup):
    config, backends, sched = setup
    backends["pve"].vms["other"] = {"node": "n1", "resources": res(10), "running": True, "macos": False}
    backend, source = await sched.pick(config.template("ubuntu"), res(), {})
    assert backend.name == "pve" and source.node == "n2"


async def test_reservations_count(setup):
    config, _, sched = setup
    reserved = {("pve", "n2"): Reservation(vms=1, memory_gb=20)}
    _, source = await sched.pick(config.template("ubuntu"), res(), reserved)
    assert source.node == "n1"


async def test_skips_nodes_without_memory(setup):
    config, _, sched = setup
    with pytest.raises(GatewayError) as e:
        await sched.pick(config.template("ubuntu"), res(mem=64), {})
    assert e.value.code == "no_capacity" and e.value.status == 503


async def test_max_vms(setup):
    config, backends, sched = setup
    config.host("pve").max_vms = 1
    backends["pve"].vms["a"] = {"node": "n1", "resources": res(1), "running": True, "macos": False}
    _, source = await sched.pick(config.template("ubuntu"), res(), {})
    assert source.node == "n2"
    with pytest.raises(GatewayError):
        await sched.pick(config.template("ubuntu"), res(), {("pve", "n2"): Reservation(vms=1)})


async def test_macos_limit_counts_all_running_macos(setup):
    config, backends, sched = setup
    backends["mac"].vms["x"] = {"node": "mac", "resources": res(1), "running": True, "macos": True}
    await sched.pick(config.template("macos"), res(8), {})  # one slot left
    with pytest.raises(GatewayError, match="macOS"):
        await sched.pick(config.template("macos"), res(8), {("mac", "mac"): Reservation(vms=1, macos=1)})


async def test_backend_failure_skips_host(setup):
    config, backends, sched = setup

    async def boom():
        raise RuntimeError("down")

    backends["pve"].capacity = boom
    with pytest.raises(GatewayError, match="no_capacity|unreachable"):
        await sched.pick(config.template("ubuntu"), res(), {})
