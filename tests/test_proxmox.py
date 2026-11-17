from urllib.parse import unquote

import pytest

from vmgateway.backends.proxmox import ProxmoxBackend, boot_disk, first_ipv4
from vmgateway.config import ProxmoxHost, TemplateConfig, TemplateSource
from vmgateway.errors import BackendError
from vmgateway.models import Credentials, Resources, VMRef


class FakeAPI:
    """Records proxmoxer-style calls: api.nodes("n").qemu(1).config.put(**kw)."""

    def __init__(self, handler, path=()):
        self._handler, self._path = handler, path

    def __getattr__(self, name):
        if name in ("get", "post", "put", "delete"):
            return lambda *a, **kw: self._handler(name, "/".join(map(str, self._path)), kw)
        return FakeAPI(self._handler, (*self._path, name))

    def __call__(self, *args):
        return FakeAPI(self._handler, (*self._path, *args))


class PVE:
    def __init__(self):
        self.calls = []
        self.vm_config = {"name": "vmgw-abc", "boot": "order=scsi0;net0", "scsi0": "local-lvm:x,size=10G"}
        self.status = "running"
        self.agent_ip = "192.168.1.50"
        self.exists = True

    def __call__(self, method, path, kw):
        self.calls.append((method, path, kw))
        if path.endswith("tasks/UPID/status"):
            return {"status": "stopped", "exitstatus": "OK"}
        if path == "cluster/nextid":
            return "105"
        if path == "nodes" and method == "get":
            return [
                {"node": "n1", "status": "online", "maxmem": 64 * 2**30, "mem": 40 * 2**30},
                {"node": "n2", "status": "online", "maxmem": 64 * 2**30, "mem": 2 * 2**30},
                {"node": "n3", "status": "offline"},
            ]
        if path == "cluster/resources":
            return [
                {
                    "vmid": 105,
                    "name": "vmgw-abc",
                    "node": "n2",
                    "pool": "vmgw",
                    "status": "running",
                    "template": 0,
                },
                {"vmid": 9024, "name": "ubuntu-cloud", "node": "n2", "template": 1},
                {"vmid": 207, "name": "headscale", "node": "n1", "status": "running"},
            ]
        if path in ("nodes/n1/qemu", "nodes/n2/qemu") and method == "get":
            return (
                [
                    {"vmid": 105, "name": "vmgw-abc", "status": "running"},
                    {"vmid": 9024, "name": "ubuntu-cloud", "template": 1},
                ]
                if path.endswith("n2/qemu")
                else [{"vmid": 207, "name": "headscale"}]
            )
        if path.endswith("/config") and method == "get":
            if not self.exists:
                raise RuntimeError(
                    "500 Internal Server Error: Configuration file 'nodes/n2/qemu-server/105.conf' does not exist"
                )
            return self.vm_config
        if path.endswith("status/current"):
            return {"status": self.status}
        if "network-get-interfaces" in path:
            return {
                "result": [
                    {"name": "lo", "ip-addresses": [{"ip-address-type": "ipv4", "ip-address": "127.0.0.1"}]},
                    {
                        "name": "eth0",
                        "ip-addresses": [{"ip-address-type": "ipv4", "ip-address": self.agent_ip}],
                    },
                ]
            }
        if method in ("post", "delete") and (path.endswith(("clone", "start", "stop")) or method == "delete"):
            return "UPID"
        return None

    def find(self, method, suffix):
        return [kw for m, p, kw in self.calls if m == method and p.endswith(suffix)]


HOST = ProxmoxHost.model_construct(name="pve", type="proxmox", pool="vmgw", max_vms=None, storage=None)
TEMPLATE = TemplateConfig.model_validate(
    {
        "id": "ubuntu",
        "description": "u",
        "os": "linux",
        "arch": "amd64",
        "host_type": "proxmox",
        "sources": [{"host": "pve", "node": "n2", "ref": "9024"}],
        "defaults": {"cpu": 2, "memory_gb": 4, "disk_gb": 20},
        "credentials": {"mode": "cloudinit", "username": "agent"},
    }
)


@pytest.fixture
def pve():
    return PVE()


@pytest.fixture
def backend(pve):
    return ProxmoxBackend(HOST, api=FakeAPI(pve), poll_s=0)


async def test_capacity(backend):
    caps = {c.node: c for c in await backend.capacity()}
    assert set(caps) == {"n1", "n2"}
    assert caps["n2"].free_memory_gb == 62 and caps["n2"].running_vms == 1
    assert caps["n1"].running_vms == 0  # headscale is not ours


async def test_provision_sequence(backend, pve):
    creds = Credentials(username="agent", password="pw", ssh_public_key="ssh-ed25519 AAAA vmgateway")
    ref = await backend.provision(
        "vmgw-abc",
        TEMPLATE,
        TemplateSource(host="pve", node="n2", ref="9024"),
        Resources(cpu=4, memory_gb=8, disk_gb=30),
        creds,
    )
    assert ref == VMRef(host="pve", node="n2", vm_id="105", name="vmgw-abc")
    assert pve.find("post", "qemu/9024/clone") == [
        {"newid": 105, "name": "vmgw-abc", "pool": "vmgw", "full": 0}
    ]
    cfg = pve.find("put", "qemu/105/config")[0]
    assert cfg["cores"] == 4 and cfg["memory"] == 8192 and cfg["ciuser"] == "agent"
    assert cfg["ipconfig0"] == "ip=dhcp"
    assert "%20" in cfg["sshkeys"] and unquote(cfg["sshkeys"]) == creds.ssh_public_key
    assert pve.find("put", "qemu/105/resize") == [{"disk": "scsi0", "size": "30G"}]
    assert pve.find("post", "qemu/105/status/start")


async def test_no_resize_when_not_larger(backend, pve):
    await backend.provision(
        "vmgw-abc",
        TEMPLATE,
        TemplateSource(host="pve", node="n2", ref="9024"),
        Resources(cpu=2, memory_gb=4, disk_gb=10),
        Credentials(username="a", password="p"),
    )
    assert pve.find("put", "resize") == []


async def test_wait_ready(backend, monkeypatch):
    async def always_open(host, port, timeout=3):
        return True

    monkeypatch.setattr("vmgateway.backends.proxmox.tcp_open", always_open)
    access = await backend.wait_ready(VMRef(host="pve", node="n2", vm_id="105"), TEMPLATE, 5, None)
    assert access.ip == "192.168.1.50" and access.endpoints[0].port == 22


async def test_destroy_running(backend, pve):
    await backend.destroy(VMRef(host="pve", node="n2", vm_id="105"))
    assert pve.find("post", "qemu/105/status/stop")
    assert pve.find("delete", "qemu/105") == [{"purge": 1, "destroy-unreferenced-disks": 1}]


async def test_destroy_refuses_unmanaged(backend, pve):
    pve.vm_config = {"name": "headscale"}
    with pytest.raises(BackendError, match="refusing"):
        await backend.destroy(VMRef(host="pve", node="n1", vm_id="208"))
    assert pve.find("delete", "qemu/208") == []


async def test_destroy_missing_is_ok(backend, pve):
    pve.exists = False
    await backend.destroy(VMRef(host="pve", node="n2", vm_id="105"))


async def test_list_managed(backend):
    assert [r.name for r in await backend.list_managed()] == ["vmgw-abc"]


def test_boot_disk():
    assert (
        boot_disk({"boot": "order=scsi0;ide2;net0", "scsi0": "x,size=10G", "ide2": "x,media=cdrom"})
        == "scsi0"
    )
    assert boot_disk({"boot": "order=ide2;virtio0", "ide2": "none,media=cdrom", "virtio0": "x"}) == "virtio0"
    assert boot_disk({"sata0": "x"}) == "sata0"


def test_first_ipv4_skips_apipa():
    ifaces = [
        {"name": "Ethernet", "ip-addresses": [{"ip-address-type": "ipv4", "ip-address": "169.254.1.1"}]},
        {"name": "Ethernet 2", "ip-addresses": [{"ip-address-type": "ipv4", "ip-address": "10.1.1.1"}]},
    ]
    assert first_ipv4(ifaces) == "10.1.1.1"


async def test_task_warnings_are_success(backend, pve):
    orig = pve.__call__

    def warn(method, path, kw):
        if path.endswith("tasks/UPID/status"):
            return {"status": "stopped", "exitstatus": "WARNINGS: 1"}
        return orig(method, path, kw)

    backend.api = FakeAPI(warn)
    await backend._wait_task("n2", "UPID")


async def test_provision_cleans_up_after_partial_failure(backend, pve):
    orig = pve.__call__

    def fail_start(method, path, kw):
        if path.endswith("status/start"):
            raise RuntimeError("500 boom")
        return orig(method, path, kw)

    backend.api = FakeAPI(fail_start)
    with pytest.raises(BackendError, match="boom"):
        await backend.provision(
            "vmgw-abc",
            TEMPLATE,
            TemplateSource(host="pve", node="n2", ref="9024"),
            Resources(cpu=2, memory_gb=4, disk_gb=10),
            Credentials(username="a", password="p"),
        )
    assert pve.find("delete", "qemu/105")
