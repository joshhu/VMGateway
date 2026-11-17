"""JSON shapes shared by REST and MCP, so both surfaces describe leases identically."""

from __future__ import annotations

from .config import GatewayConfig, TemplateConfig
from .models import Lease, LeaseState, NodeCapacity


def template_view(t: TemplateConfig) -> dict:
    return {
        "id": t.id,
        "description": t.description,
        "os": t.os,
        "arch": t.arch,
        "platform": t.host_type,
        "defaults": t.defaults.model_dump(),
        "endpoints": t.endpoints,
    }


def _connect_hints(lease: Lease) -> list[str]:
    if not lease.access or not lease.credentials:
        return []
    user = lease.credentials.username
    hints = []
    for ep in lease.access.endpoints:
        if ep.proto == "ssh":
            port = "" if ep.port == 22 else f" -p {ep.port}"
            key = " -i <saved private key>" if lease.credentials.ssh_private_key else ""
            hints.append(f"ssh{key}{port} {user}@{ep.host}")
        elif ep.proto == "rdp":
            hints.append(f"RDP {ep.host}:{ep.port} as {user}")
        elif ep.proto == "vnc":
            hints.append(f"VNC vnc://{ep.host}:{ep.port} as {user}")
    return hints


def lease_view(lease: Lease, include_secrets: bool = True, include_key: bool = False) -> dict:
    out = {
        "id": lease.id,
        "owner": lease.owner,
        "template": lease.template_id,
        "state": lease.state.value,
        "resources": lease.resources.model_dump(),
        "created_at": lease.created_at.isoformat(),
        "expires_at": lease.expires_at.isoformat(),
        "host": lease.host,
        "error": lease.error,
    }
    if lease.access:
        out["ip"] = lease.access.ip
        out["endpoints"] = [e.model_dump() for e in lease.access.endpoints]
    if lease.credentials and include_secrets and lease.state == LeaseState.READY:
        c = {"username": lease.credentials.username, "password": lease.credentials.password}
        if lease.credentials.ssh_private_key:
            c["ssh_private_key"] = (
                lease.credentials.ssh_private_key if include_key else "(request with include_key)"
            )
        out["credentials"] = c
        out["connect"] = _connect_hints(lease)
    return out


def hosts_view(
    config: GatewayConfig, caps: dict[str, list[NodeCapacity] | str], detailed: bool
) -> list[dict]:
    out = []
    for h in config.hosts:
        c = caps.get(h.name)
        entry: dict = {"name": h.name, "platform": h.type}
        if isinstance(c, str):
            entry["status"] = f"unreachable: {c}"
        elif c is not None:
            entry["status"] = "ok"
            entry["nodes"] = [
                n.model_dump() if detailed else {"node": n.node, "free_memory_gb": round(n.free_memory_gb, 1)}
                for n in c
            ]
        entry["templates"] = [t.id for t in config.templates if any(s.host == h.name for s in t.sources)]
        out.append(entry)
    return out
