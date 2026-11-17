"""In-memory backend for tests and local demos (`type: fake` in config)."""

from __future__ import annotations

import asyncio
import itertools

from ..config import FakeHost, TemplateConfig, TemplateSource
from ..errors import BackendError
from ..models import Access, Credentials, Endpoint, NodeCapacity, Resources, VMRef
from .base import DEFAULT_PORTS, MANAGED_PREFIX


class FakeBackend:
    type = "fake"

    def __init__(self, cfg: FakeHost, delay: float = 0.0):
        self.name = cfg.name
        self.cfg = cfg
        self.delay = delay
        self.vms: dict[str, dict] = {}  # vm_id -> {"node", "resources", "running", "macos"}
        self.fail_on: set[str] = set()  # "provision" | "wait" | "destroy"
        self.destroyed: list[str] = []
        self.applied: list[tuple[str, str | None, str, str | None]] = []
        self._ip = itertools.count(10)

    async def capacity(self) -> list[NodeCapacity]:
        out = []
        for node in self.cfg.nodes:
            vms = [v for v in self.vms.values() if v["node"] == node and v["running"]]
            used = sum(v["resources"].memory_gb for v in vms)
            out.append(
                NodeCapacity(
                    host=self.name,
                    node=node,
                    free_memory_gb=self.cfg.free_memory_gb - used,
                    running_vms=len(vms),
                    max_vms=self.cfg.max_vms,
                    running_macos=sum(1 for v in vms if v["macos"]),
                    max_macos=2,
                )
            )
        return out

    async def provision(
        self,
        name: str,
        template: TemplateConfig,
        source: TemplateSource,
        resources: Resources,
        creds: Credentials,
    ) -> VMRef:
        await asyncio.sleep(self.delay)
        if "provision" in self.fail_on:
            # Simulate a half-created VM so callers must clean up.
            self.vms[name] = {"node": source.node, "resources": resources, "running": False, "macos": False}
            raise BackendError("fake provision failure")
        self.vms[name] = {
            "node": source.node or self.cfg.nodes[0],
            "resources": resources,
            "running": True,
            "macos": template.os == "macos",
            "creds": creds,
            "source": source.ref,
        }
        return VMRef(host=self.name, node=self.vms[name]["node"], vm_id=name, name=name)

    async def wait_ready(
        self, ref: VMRef, template: TemplateConfig, timeout: float, creds: Credentials | None = None
    ) -> Access:
        await asyncio.sleep(self.delay)
        if "wait" in self.fail_on:
            raise BackendError("fake wait timeout")
        ip = f"10.0.0.{next(self._ip)}"
        return Access(
            ip=ip, endpoints=[Endpoint(proto=p, host=ip, port=DEFAULT_PORTS[p]) for p in template.endpoints]
        )

    async def set_credentials(
        self, ref: VMRef, template: TemplateConfig, access: Access, current: Credentials, desired: Credentials
    ) -> Credentials:
        self.applied.append((current.username, current.password, desired.username, desired.password))
        return desired

    async def destroy(self, ref: VMRef) -> None:
        if "destroy" in self.fail_on:
            raise BackendError("fake destroy failure")
        self.vms.pop(ref.vm_id, None)
        self.destroyed.append(ref.vm_id)

    async def list_managed(self) -> list[VMRef]:
        return [
            VMRef(host=self.name, node=v["node"] or self.cfg.nodes[0], vm_id=k, name=k)
            for k, v in self.vms.items()
            if k.startswith(MANAGED_PREFIX)
        ]
