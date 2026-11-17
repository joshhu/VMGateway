"""MCP tools over Streamable HTTP. Callers authenticate with the same API keys as REST."""

from __future__ import annotations

from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import Image

from .api import collect_capacity
from .auth import ApiKeyVerifier
from .errors import GatewayError
from .leases import LeaseManager
from .models import LeaseState, User
from .views import hosts_view, lease_view, template_view

INSTRUCTIONS = """\
VMGateway leases fresh virtual machines (Linux, Windows, macOS) on the user's own hypervisors.
Workflow: list_templates → create_vm (blocks until the VM has an IP) → use the returned
connect hints / credentials (SSH, RDP or VNC) → destroy_vm as soon as you are done.
screenshot_vm shows you the VM's screen without needing an RDP/VNC client.
Leases expire automatically (default 2 h, max 72 h); call renew_vm for long jobs.
Pick resources for the job: 2 vCPU / 4 GB is plenty for CLI tests and screenshots; give
builds or IDEs 4+ vCPU / 8+ GB. Disk can only grow beyond the template default.
When a lease includes an ssh_private_key, save it to a file with mode 600 and pass it with -i.
"""


def _create_description(mgr: LeaseManager) -> str:
    lines = [
        "Create a new VM from a template and (by default) wait until it is reachable.",
        "Returns lease id, IP, endpoints, credentials and ready-to-use connect hints.",
        "Available templates:",
    ]
    for t in mgr.config.templates:
        d = t.defaults
        lines.append(
            f"- {t.id}: {t.description} [{t.os}/{t.arch} on {t.host_type}; defaults {d.cpu} vCPU,"
            f" {d.memory_gb} GB RAM, {d.disk_gb} GB disk; {', '.join(t.endpoints)}]"
        )
    p = mgr.policy
    lines.append(
        f"ttl_minutes: {p.ttl_min_minutes}-{p.ttl_max_minutes} (default {p.ttl_default_minutes})."
        " If the result state is still 'provisioning', poll get_vm."
        " username/password are optional: omit them to get generated (or template) credentials;"
        " set them only when the user asked for a specific login (8-64 chars; Windows needs 3 of"
        " upper/lower/digit/symbol)."
    )
    return "\n".join(lines)


def build_mcp(mgr: LeaseManager) -> MCPServer:
    public = mgr.config.server.public_url.rstrip("/")
    mcp = MCPServer(
        "vmgateway",
        instructions=INSTRUCTIONS,
        token_verifier=ApiKeyVerifier(mgr.store),
        auth=AuthSettings(
            issuer_url=public, resource_server_url=f"{public}/mcp", validate_token_resource=False
        ),
    )

    async def caller() -> User:
        tok = get_access_token()
        user = await mgr.store.get_user(tok.client_id) if tok else None
        if user is None or user.revoked:
            raise GatewayError("unauthorized", "invalid API key", 401)
        return user

    @mcp.tool(description="List the OS templates you can create VMs from.")
    async def list_templates() -> list[dict]:
        await caller()
        return [template_view(t) for t in mgr.config.templates]

    @mcp.tool(description="Show hypervisor hosts, their free memory and which templates they serve.")
    async def list_hosts() -> list[dict]:
        user = await caller()
        return hosts_view(mgr.config, await collect_capacity(mgr), detailed=user.is_admin)

    @mcp.tool(description=_create_description(mgr))
    async def create_vm(
        template: str,
        cpu: int | None = None,
        memory_gb: int | None = None,
        disk_gb: int | None = None,
        ttl_minutes: int | None = None,
        username: str | None = None,
        password: str | None = None,
        wait: bool = True,
    ) -> dict:
        user = await caller()
        lease = await mgr.create(user, template, cpu, memory_gb, disk_gb, ttl_minutes, username, password)
        if wait:
            t = mgr.config.template(template)
            lease = await mgr.wait(user, lease.id, t.ready_timeout_s)
        out = lease_view(lease, include_key=True)
        if lease.state == LeaseState.PROVISIONING:
            out["note"] = "Still provisioning. Call get_vm with this id in a minute."
        return out

    @mcp.tool(description="Get one lease (state, IP, credentials). wait_s>0 blocks until it is ready.")
    async def get_vm(lease_id: str, wait_s: float = 0) -> dict:
        user = await caller()
        lease = await (mgr.wait(user, lease_id, min(wait_s, 1800)) if wait_s > 0 else mgr.get(user, lease_id))
        return lease_view(lease, include_key=True)

    @mcp.tool(
        description=(
            "Capture the VM's screen (its console/desktop, even before anyone logs in) as a PNG image."
            " Use it to see GUI state, verify that an app opened, or grab product screenshots."
            " max_width downsizes wide screens (default 1600 px; 0 = full resolution)."
        )
    )
    async def screenshot_vm(lease_id: str, max_width: int = 1600) -> Image:
        user = await caller()
        png = await mgr.screenshot(user, lease_id, max_width or None)
        return Image(data=png, format="png")

    @mcp.tool(description="List your active leases (admins: all_users=true for everyone's).")
    async def list_vms(all_users: bool = False) -> list[dict]:
        user = await caller()
        return [lease_view(x, include_secrets=False) for x in await mgr.list(user, all_users)]

    @mcp.tool(description="Extend a lease: new expiry = now + ttl_minutes.")
    async def renew_vm(lease_id: str, ttl_minutes: int) -> dict:
        user = await caller()
        return lease_view(await mgr.renew(user, lease_id, ttl_minutes), include_secrets=False)

    @mcp.tool(description="Destroy a VM and end its lease. Do this as soon as you are finished.")
    async def destroy_vm(lease_id: str) -> dict:
        user = await caller()
        return lease_view(await mgr.destroy(user, lease_id), include_secrets=False)

    return mcp
