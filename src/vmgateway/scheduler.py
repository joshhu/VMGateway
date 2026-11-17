"""Choose where a new VM goes."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from .backends.base import Backend
from .config import Resources, TemplateConfig, TemplateSource
from .errors import GatewayError
from .models import NodeCapacity

log = logging.getLogger(__name__)


@dataclass
class Reservation:
    """Resources held by leases that are still provisioning (not yet visible in backend capacity)."""

    vms: int = 0
    memory_gb: float = 0
    macos: int = 0


class Scheduler:
    def __init__(self, backends: dict[str, Backend]):
        self.backends = backends

    async def pick(
        self,
        template: TemplateConfig,
        resources: Resources,
        reserved: dict[tuple[str, str], Reservation],
    ) -> tuple[Backend, TemplateSource]:
        hosts = sorted({s.host for s in template.sources})
        results = await asyncio.gather(*(self.backends[h].capacity() for h in hosts), return_exceptions=True)
        caps: dict[tuple[str, str], NodeCapacity] = {}
        unreachable = []
        for h, r in zip(hosts, results, strict=True):
            if isinstance(r, BaseException):
                log.warning("capacity check failed for %s: %s", h, r)
                unreachable.append(h)
                continue
            for c in r:
                caps[(c.host, c.node)] = c

        reasons: list[str] = []
        best: tuple[float, TemplateSource] | None = None
        for src in template.sources:
            node = src.node or src.host
            cap = caps.get((src.host, node))
            if cap is None:
                if src.host not in unreachable:
                    reasons.append(f"{src.host}/{node}: node not found")
                continue
            r = reserved.get((src.host, node), Reservation())
            free = cap.free_memory_gb - r.memory_gb
            if free < resources.memory_gb:
                reasons.append(f"{src.host}/{node}: {free:.0f} GB free < {resources.memory_gb} GB")
                continue
            if cap.max_vms is not None and cap.running_vms + r.vms >= cap.max_vms:
                reasons.append(f"{src.host}/{node}: max_vms {cap.max_vms} reached")
                continue
            if (
                template.os == "macos"
                and cap.max_macos is not None
                and cap.running_macos + r.macos >= cap.max_macos
            ):
                reasons.append(f"{src.host}/{node}: macOS limit {cap.max_macos} running VMs reached")
                continue
            if best is None or free > best[0]:
                best = (free, src)

        if best is None:
            if unreachable:
                reasons.append(f"unreachable: {', '.join(unreachable)}")
            raise GatewayError("no_capacity", "; ".join(reasons) or "no source", 503)
        src = best[1]
        if src.node is None:
            src = src.model_copy(update={"node": src.host})
        return self.backends[src.host], src
