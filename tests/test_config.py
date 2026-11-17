import textwrap

import pytest

from vmgateway.config import ConfigError, load_config

BASE = """
server:
  public_url: http://gw.lan:8000
hosts:
  - name: pve
    type: proxmox
    url: https://pve.lan:8006
    token_id: vmgw@pve!gateway
    token_secret_env: TEST_PVE_SECRET
  - name: mac
    type: tart
    ssh: {host: mac.lan, user: me, key_path: /keys/mac}
  - name: hv
    type: hyperv
    ssh: {host: hv.lan, user: me, key_path: /keys/hv}
    vm_root: 'C:\\VMs\\vmgw'
    public_ip: 10.0.0.5
templates:
  - id: ubuntu-24.04
    description: Ubuntu 24.04
    os: linux
    arch: amd64
    host_type: proxmox
    sources: [{host: pve, node: n1, ref: "9024"}]
    defaults: {cpu: 2, memory_gb: 4, disk_gb: 20}
    credentials: {mode: cloudinit, username: agent}
  - id: macos
    description: macOS
    os: macos
    arch: arm64
    host_type: tart
    sources: [{host: mac, ref: tpl}]
    defaults: {cpu: 4, memory_gb: 8, disk_gb: 100}
    credentials: {mode: static, username: admin, password_env: TEST_MAC_PW}
    endpoints: [ssh, vnc]
"""


def write(tmp_path, text):
    p = tmp_path / "config.yaml"
    p.write_text(textwrap.dedent(text), encoding="utf-8")
    return p


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("TEST_PVE_SECRET", "s3cret")
    monkeypatch.setenv("TEST_MAC_PW", "macpw")


def test_loads_and_resolves_secrets(tmp_path, env):
    cfg = load_config(write(tmp_path, BASE))
    pve = cfg.host("pve")
    assert pve.type == "proxmox"
    assert pve.token_secret.get_secret_value() == "s3cret"
    mac_tpl = cfg.template("macos")
    assert mac_tpl.credentials.password.get_secret_value() == "macpw"
    assert mac_tpl.endpoints == ["ssh", "vnc"]
    assert cfg.template("ubuntu-24.04").endpoints == ["ssh"]


def test_policy_defaults(tmp_path, env):
    p = load_config(write(tmp_path, BASE)).policy
    assert (p.ttl_min_minutes, p.ttl_default_minutes, p.ttl_max_minutes) == (10, 120, 4320)
    assert p.default_max_leases == 5


def test_missing_env_var_names_it(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_MAC_PW", "x")
    monkeypatch.delenv("TEST_PVE_SECRET", raising=False)
    with pytest.raises(ConfigError, match="TEST_PVE_SECRET"):
        load_config(write(tmp_path, BASE))


def test_template_source_must_reference_known_host(tmp_path, env):
    bad = BASE.replace("{host: mac, ref: tpl}", "{host: nope, ref: tpl}")
    with pytest.raises(ConfigError, match="nope"):
        load_config(write(tmp_path, bad))


def test_template_source_host_type_must_match(tmp_path, env):
    bad = BASE.replace("{host: mac, ref: tpl}", "{host: pve, node: n1, ref: tpl}")
    with pytest.raises(ConfigError, match="host_type"):
        load_config(write(tmp_path, bad))


def test_unknown_template_lookup(tmp_path, env):
    cfg = load_config(write(tmp_path, BASE))
    assert cfg.find_template("missing") is None
