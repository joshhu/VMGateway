"""The contract every hypervisor backend implements."""

from __future__ import annotations

import asyncio
from typing import Protocol, runtime_checkable

from ..config import TemplateConfig, TemplateSource
from ..models import Access, Credentials, NodeCapacity, Resources, VMRef

MANAGED_PREFIX = "vmgw-"

DEFAULT_PORTS = {"ssh": 22, "rdp": 3389, "vnc": 5900}


@runtime_checkable
class Backend(Protocol):
    name: str
    type: str

    async def capacity(self) -> list[NodeCapacity]:
        """Free resources per node. Counts every running VM, not only ours."""
        ...

    async def provision(
        self,
        name: str,
        template: TemplateConfig,
        source: TemplateSource,
        resources: Resources,
        creds: Credentials,
    ) -> VMRef:
        """Clone `source`, apply resources and credentials, start it. Returns as soon as it is booting."""
        ...

    async def wait_ready(
        self, ref: VMRef, template: TemplateConfig, timeout: float, creds: Credentials
    ) -> Access:
        """Block until the guest has an address and its first endpoint accepts TCP."""
        ...

    # Optional: backends serving `static` templates may implement
    #   async def set_credentials(self, ref, template, access, current, desired) -> Credentials
    # to create/modify the guest account after boot (user-chosen username/password).

    async def destroy(self, ref: VMRef) -> None:
        """Stop and delete. Must succeed if the VM is already gone."""
        ...

    async def list_managed(self) -> list[VMRef]:
        """VMs on this backend whose name starts with MANAGED_PREFIX."""
        ...


async def tcp_open(host: str, port: int, timeout: float = 3) -> bool:
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except (OSError, TimeoutError):
        return False
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return True
