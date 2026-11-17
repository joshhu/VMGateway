from __future__ import annotations

from ..config import GatewayConfig
from .base import MANAGED_PREFIX, Backend

__all__ = ["MANAGED_PREFIX", "Backend", "build_backends"]


def build_backends(config: GatewayConfig) -> dict[str, Backend]:
    out: dict[str, Backend] = {}
    for h in config.hosts:
        if h.type == "fake":
            from .fake import FakeBackend

            out[h.name] = FakeBackend(h)
        elif h.type == "proxmox":
            from .proxmox import ProxmoxBackend

            out[h.name] = ProxmoxBackend(h)
        elif h.type == "tart":
            from .tart import TartBackend

            out[h.name] = TartBackend(h)
        elif h.type == "hyperv":
            from .hyperv import HyperVBackend

            out[h.name] = HyperVBackend(h)
    return out
