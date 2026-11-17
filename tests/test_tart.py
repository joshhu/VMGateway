import json

import pytest

from vmgateway.backends.tart import TartBackend
from vmgateway.config import TartHost, TemplateConfig, TemplateSource
from vmgateway.errors import BackendError
from vmgateway.models import Credentials, Resources, VMRef

T = "/opt/homebrew/bin/tart"


class FakeRunner:
    def __init__(self):
        self.cmds = []
        self.vms = {
            "gg-template": {"OS": "darwin", "Memory": 8192, "Disk": 100, "Running": False},
            "book": {"OS": "darwin", "Memory": 8192, "Disk": 100, "Running": True},
            "vmgw-old": {"OS": "linux", "Memory": 4096, "Disk": 20, "Running": True},
        }
        self.ip = {"agent": "", "arp": "192.168.1.29"}

    async def run(self, cmd, timeout=120, check=True):
        self.cmds.append(cmd)
        if cmd.startswith("sysctl"):
            lines = [str(24 * 2**30)] + [f"{n}\t{json.dumps(v)}" for n, v in self.vms.items()]
            return "\n".join(lines) + "\n"
        if cmd == f"{T} list --source local --quiet":
            return "\n".join(self.vms) + "\n"
        if cmd.startswith(f"{T} clone"):
            src, dst = cmd.split()[2:4]
            self.vms[dst] = dict(self.vms[src], Running=False)
            return ""
        if cmd.startswith(f"{T} get"):
            return json.dumps(self.vms[cmd.split()[2]])
        if cmd.startswith(f"{T} ip --resolver"):
            return self.ip[cmd.split()[3]] + "\n"
        if f"{T} delete" in cmd:
            name = cmd.split()[2]
            self.vms.pop(name, None)
            return ""
        return ""


HOST = TartHost.model_construct(
    name="mac", type="tart", tart_bin=T, bridged_interface="en0", max_vms=None, max_macos_vms=2
)
MACOS = TemplateConfig.model_validate(
    {
        "id": "macos",
        "description": "m",
        "os": "macos",
        "arch": "arm64",
        "host_type": "tart",
        "sources": [{"host": "mac", "ref": "gg-template"}],
        "defaults": {"cpu": 4, "memory_gb": 8, "disk_gb": 100},
        "credentials": {"mode": "cloudinit"},
        "endpoints": ["ssh", "vnc"],
    }
)


@pytest.fixture
def runner():
    return FakeRunner()


@pytest.fixture
def backend(runner):
    return TartBackend(HOST, runner=runner, poll_s=0)


async def test_capacity_counts_all_running_macos(backend):
    (cap,) = await backend.capacity()
    assert cap.running_vms == 2 and cap.running_macos == 1 and cap.max_macos == 2
    assert round(cap.free_memory_gb, 1) == round(24 * 0.85 - 12, 1)


async def test_provision(backend, runner):
    ref = await backend.provision(
        "vmgw-abc",
        MACOS,
        TemplateSource(host="mac", ref="gg-template"),
        Resources(cpu=6, memory_gb=12, disk_gb=120),
        Credentials(username="admin", password="x"),
    )
    assert ref.vm_id == "vmgw-abc"
    assert f"{T} clone gg-template vmgw-abc" in runner.cmds
    assert f"{T} set vmgw-abc --cpu 6 --memory 12288 --disk-size 120" in runner.cmds
    run = next(c for c in runner.cmds if "nohup" in c)
    assert f"{T} run vmgw-abc --no-graphics --net-bridged=en0" in run and run.endswith("&")


async def test_disk_not_shrunk(backend, runner):
    await backend.provision(
        "vmgw-abc",
        MACOS,
        TemplateSource(host="mac", ref="gg-template"),
        Resources(cpu=4, memory_gb=8, disk_gb=50),
        Credentials(username="admin", password="x"),
    )
    assert f"{T} set vmgw-abc --cpu 4 --memory 8192" in runner.cmds


async def test_wait_ready_falls_back_to_arp(backend, monkeypatch):
    async def ok(host, port, timeout=3):
        return True

    monkeypatch.setattr("vmgateway.backends.tart.tcp_open", ok)
    access = await backend.wait_ready(VMRef(host="mac", node="mac", vm_id="vmgw-abc"), MACOS, 5, None)
    assert access.ip == "192.168.1.29"
    assert [e.port for e in access.endpoints] == [22, 5900]


async def test_destroy_and_list(backend, runner):
    assert [r.name for r in await backend.list_managed()] == ["vmgw-old"]
    await backend.destroy(VMRef(host="mac", node="mac", vm_id="vmgw-old"))
    assert "vmgw-old" not in runner.vms
    await backend.destroy(VMRef(host="mac", node="mac", vm_id="vmgw-old"))  # idempotent


async def test_refuses_unmanaged_names(backend, runner):
    with pytest.raises(BackendError):
        await backend.destroy(VMRef(host="mac", node="mac", vm_id="book"))
    with pytest.raises(BackendError):
        await backend.destroy(VMRef(host="mac", node="mac", vm_id="vmgw-x; rm -rf ~"))
    assert "book" in runner.vms
