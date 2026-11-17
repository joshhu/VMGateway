import json
import xml.etree.ElementTree as ET

import pytest

from vmgateway.backends.hyperv import HyperVBackend, allocate_ports, parse_mapping, ps, unattend_xml
from vmgateway.config import HyperVHost, SSHConfig, TemplateConfig, TemplateSource
from vmgateway.errors import BackendError
from vmgateway.models import Credentials, Resources, VMRef

NS = {"u": "urn:schemas-microsoft-com:unattend"}


def test_unattend_escapes_and_parses():
    xml = unattend_xml("VMGW-ABC", Credentials(username="agent", password="a<b&c'd\"e"), "arm64", "zh-TW")
    root = ET.fromstring(xml)
    pw = root.find(".//u:LocalAccount/u:Password/u:Value", NS).text
    assert pw == "a<b&c'd\"e"
    assert root.find(".//u:ComputerName", NS).text == "VMGW-ABC"
    assert root.find(".//u:UILanguage", NS).text == "zh-TW"
    assert all(
        c.get("processorArchitecture") == "arm64"
        for c in root.iter("{urn:schemas-microsoft-com:unattend}component")
    )


def test_ps_quote():
    assert ps("it's") == "'it''s'"


def test_allocate_ports():
    assert allocate_ports({40000, 40002}, 40000, 40010, 2) == [40001, 40003]
    with pytest.raises(BackendError):
        allocate_ports({1, 2}, 1, 2, 1)


def test_parse_mapping():
    assert parse_mapping("rdp=40001;ssh=40002") == {"rdp": 40001, "ssh": 40002}
    assert parse_mapping("garbage") == {}


class FakeRunner:
    def __init__(self):
        self.scripts = []

    async def run_ps(self, script, timeout=300, check=True):
        self.scripts.append(script)
        if "portproxy show" in script:
            return (
                "Listen on ipv4:             Connect to ipv4:\n\nAddress         Port        Address         Port\n"
                "--------------- ----------  --------------- ----------\n0.0.0.0         40000       172.26.1.5      3389\n"
                "rdp=40001;ssh=40002\n"
            )
        if "Win32_OperatingSystem" in script:
            return json.dumps({"free": 34 * 2**30, "running": 1})
        if "Get-VM | Where-Object Name -like" in script:
            return "vmgw-aaa\nvmgw-aaa\nvmgw-bbb\nvmgw-rdp\n"
        return ""


HOST = HyperVHost(
    name="rtx",
    type="hyperv",
    ssh=SSHConfig(host="h", user="u", key_path="k"),
    vm_root="C:\\VMs\\vmgw",
    public_ip="10.0.0.46",
)
WIN = TemplateConfig.model_validate(
    {
        "id": "win11-arm",
        "description": "w",
        "os": "windows",
        "arch": "arm64",
        "host_type": "hyperv",
        "sources": [{"host": "rtx", "ref": "C:\\T\\win.vhdx"}],
        "defaults": {"cpu": 4, "memory_gb": 8, "disk_gb": 64},
        "credentials": {"mode": "unattend", "username": "agent"},
        "endpoints": ["rdp", "ssh"],
        "windows_locale": "zh-TW",
    }
)


@pytest.fixture
def runner():
    return FakeRunner()


@pytest.fixture
def backend(runner):
    return HyperVBackend(HOST, runner=runner, poll_s=0)


async def test_capacity(backend):
    (cap,) = await backend.capacity()
    assert cap.free_memory_gb == 30 and cap.running_vms == 1


async def test_provision(backend, runner):
    ref = await backend.provision(
        "vmgw-abc",
        WIN,
        TemplateSource(host="rtx", ref="C:\\T\\win.vhdx"),
        Resources(cpu=4, memory_gb=8, disk_gb=64),
        Credentials(username="agent", password="Pw1-xyz"),
    )
    assert ref.extra == {"ports": "rdp=40003;ssh=40004"}  # 40000 proxied, 40001-2 held by another rule
    fw = next(s for s in runner.scripts if "New-NetFirewallRule -Name 'vmgw-abc'" in s)
    assert "-LocalPort 40003,40004" in fw and "-Profile Any" in fw
    main = runner.scripts[-1]
    assert "New-VHD -Path $vhd -ParentPath $parent -Differencing" in main
    assert "-MemoryStartupBytes 8GB" in main and "Set-VMProcessor -VMName $name -Count 4" in main
    assert "Panther\\\\unattend.xml" in main or "Panther\\unattend.xml" in main
    assert "Pw1-xyz" in main and "<UILanguage>zh-TW</UILanguage>" in main


async def test_wait_ready_requires_creds(backend):
    with pytest.raises(BackendError):
        await backend.wait_ready(VMRef(host="rtx", node="rtx", vm_id="vmgw-abc"), WIN, 1, None)


async def test_destroy_script_and_guard(backend, runner):
    await backend.destroy(VMRef(host="rtx", node="rtx", vm_id="vmgw-abc"))
    s = runner.scripts[-1]
    assert "portproxy delete" in s and "Remove-VM -Name $name -Force" in s and "Remove-NetFirewallRule" in s
    with pytest.raises(BackendError):
        await backend.destroy(VMRef(host="rtx", node="rtx", vm_id="Win11-26H2"))


async def test_list_managed_dedupes(backend):
    assert [r.name for r in await backend.list_managed()] == ["vmgw-aaa", "vmgw-bbb"]
