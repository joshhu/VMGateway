"""Gateway configuration: config.yaml (no secrets) + environment variables (secrets)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, Field, SecretStr, ValidationError, model_validator


class ConfigError(ValueError):
    pass


def _env(name: str) -> SecretStr:
    value = os.environ.get(name)
    if not value:
        raise ValueError(f"environment variable {name} is not set")
    return SecretStr(value)


class Resources(BaseModel):
    cpu: int = Field(ge=1, le=128)
    memory_gb: int = Field(ge=1, le=1024)
    disk_gb: int = Field(ge=1, le=16384)


class ServerConfig(BaseModel):
    listen: str = "0.0.0.0"
    port: int = 8000
    public_url: str = "http://127.0.0.1:8000"
    # Host headers accepted by the MCP endpoint (DNS-rebinding protection). "host:*" matches any port.
    allowed_hosts: list[str] = ["127.0.0.1:*", "localhost:*"]
    db_path: str = "data/vmgw.db"


class PolicyConfig(BaseModel):
    ttl_min_minutes: int = 10
    ttl_default_minutes: int = 120
    ttl_max_minutes: int = 72 * 60
    default_max_leases: int = 5
    reaper_interval_s: float = 60
    reconcile_interval_s: float = 600


class SSHConfig(BaseModel):
    host: str
    user: str
    port: int = 22
    key_path: str
    known_hosts: str | None = None  # None = do not verify host keys (trusted LAN)


class _HostBase(BaseModel):
    name: str
    max_vms: int | None = None  # per node; None = limited only by memory


class ProxmoxHost(_HostBase):
    type: Literal["proxmox"]
    url: str
    token_id: str
    token_secret_env: str
    token_secret: SecretStr | None = None
    verify_ssl: bool = False
    pool: str = "vmgw"
    storage: str | None = None  # target storage for full clones; None = same as template

    @model_validator(mode="after")
    def _resolve(self):
        self.token_secret = _env(self.token_secret_env)
        return self


class TartHost(_HostBase):
    type: Literal["tart"]
    ssh: SSHConfig
    tart_bin: str = "/opt/homebrew/bin/tart"
    bridged_interface: str | None = "en0"  # None = Tart NAT (VM reachable only from the Mac)
    max_macos_vms: int = 2  # Apple licensing limit for concurrently running macOS guests


class HyperVHost(_HostBase):
    type: Literal["hyperv"]
    ssh: SSHConfig
    vm_root: str
    switch: str = "Default Switch"
    public_ip: str  # address agents use to reach forwarded ports
    port_range: tuple[int, int] = (40000, 40999)


class FakeHost(_HostBase):
    type: Literal["fake"]
    nodes: list[str] = ["fake1"]
    free_memory_gb: float = 64


HostConfig = Annotated[ProxmoxHost | TartHost | HyperVHost | FakeHost, Field(discriminator="type")]
HostType = Literal["proxmox", "tart", "hyperv", "fake"]


class TemplateSource(BaseModel):
    host: str
    node: str | None = None  # PVE node holding the template; other backends: the host itself
    ref: str  # PVE VMID / tart VM name / VHDX path


class CredentialsConfig(BaseModel):
    mode: Literal["cloudinit", "unattend", "static"]
    username: str = "agent"
    password_env: str | None = None
    password: SecretStr | None = None

    @model_validator(mode="after")
    def _resolve(self):
        if self.mode == "static":
            if not self.password_env:
                raise ValueError("static credentials need password_env")
            self.password = _env(self.password_env)
        return self


class TemplateConfig(BaseModel):
    id: str
    description: str
    os: Literal["linux", "windows", "macos"]
    arch: Literal["amd64", "arm64"]
    host_type: HostType
    sources: list[TemplateSource] = Field(min_length=1)
    defaults: Resources
    credentials: CredentialsConfig
    endpoints: list[Literal["ssh", "rdp", "vnc"]] = ["ssh"]
    full_clone: bool = False
    windows_locale: str = "en-US"  # unattend.xml locale (Hyper-V)
    ready_timeout_s: float = 600


class GatewayConfig(BaseModel):
    server: ServerConfig = ServerConfig()
    policy: PolicyConfig = PolicyConfig()
    hosts: list[HostConfig]
    templates: list[TemplateConfig]

    @model_validator(mode="after")
    def _cross_check(self):
        hosts = {h.name: h for h in self.hosts}
        if len(hosts) != len(self.hosts):
            raise ValueError("duplicate host names")
        ids = [t.id for t in self.templates]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate template ids")
        for t in self.templates:
            for s in t.sources:
                h = hosts.get(s.host)
                if h is None:
                    raise ValueError(f"template {t.id}: unknown host {s.host!r}")
                if h.type != t.host_type:
                    raise ValueError(
                        f"template {t.id}: host_type {t.host_type} but source host {s.host} is {h.type}"
                    )
        return self

    def host(self, name: str):
        return next(h for h in self.hosts if h.name == name)

    def find_template(self, template_id: str) -> TemplateConfig | None:
        return next((t for t in self.templates if t.id == template_id), None)

    def template(self, template_id: str) -> TemplateConfig:
        t = self.find_template(template_id)
        if t is None:
            raise KeyError(template_id)
        return t


def load_config(path: str | Path) -> GatewayConfig:
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except OSError as e:
        raise ConfigError(f"cannot read {path}: {e}") from e
    try:
        return GatewayConfig.model_validate(data)
    except ValidationError as e:
        raise ConfigError(str(e)) from e
