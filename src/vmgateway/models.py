"""Domain objects shared by the lease manager, store, backends, REST API and MCP tools."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel

from .config import Resources

__all__ = [
    "ACTIVE_STATES",
    "Access",
    "Credentials",
    "Endpoint",
    "Lease",
    "LeaseState",
    "NodeCapacity",
    "Resources",
    "User",
    "VMRef",
]


class LeaseState(StrEnum):
    PROVISIONING = "provisioning"
    READY = "ready"
    RELEASING = "releasing"
    RELEASED = "released"
    FAILED = "failed"
    EXPIRED = "expired"


ACTIVE_STATES = frozenset({LeaseState.PROVISIONING, LeaseState.READY, LeaseState.RELEASING})


class VMRef(BaseModel):
    host: str  # config host name
    node: str  # PVE node; for tart/hyperv the host name again
    vm_id: str  # PVE vmid / tart VM name / Hyper-V VM name
    name: str = ""  # VM display name (vmgw-<lease id>)
    extra: dict[str, str] = {}  # backend bookkeeping (e.g. forwarded ports)


class Endpoint(BaseModel):
    proto: str  # ssh | rdp | vnc
    host: str
    port: int


class Access(BaseModel):
    ip: str
    endpoints: list[Endpoint]


class Credentials(BaseModel):
    username: str
    password: str | None = None
    ssh_private_key: str | None = None
    ssh_public_key: str | None = None


class NodeCapacity(BaseModel):
    host: str
    node: str
    free_memory_gb: float
    running_vms: int
    max_vms: int | None = None
    running_macos: int = 0
    max_macos: int | None = None


class Lease(BaseModel):
    id: str
    owner: str
    template_id: str
    state: LeaseState
    resources: Resources
    created_at: datetime
    expires_at: datetime
    host: str | None = None
    node: str | None = None
    ref: VMRef | None = None
    access: Access | None = None
    credentials: Credentials | None = None
    error: str | None = None

    @property
    def vm_name(self) -> str:
        return f"vmgw-{self.id}"


class User(BaseModel):
    name: str
    key_hash: str
    is_admin: bool = False
    max_leases: int | None = None
    created_at: datetime
    revoked: bool = False
