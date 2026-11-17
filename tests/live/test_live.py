"""Live end-to-end tests against real hypervisors.

    VMGW_LIVE=1 VMGW_LIVE_TEMPLATES=ubuntu-24.04,macos uv run pytest -m live -s

Uses ./config.yaml and ./.env. Each template: create lease → wait ready → log in → run a command → destroy.
"""

import asyncio
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import asyncssh
import pytest

from vmgateway.backends import build_backends
from vmgateway.cli import _load_dotenv
from vmgateway.config import load_config
from vmgateway.leases import LeaseManager
from vmgateway.models import LeaseState, User
from vmgateway.store import Store

ROOT = Path(__file__).resolve().parents[2]
pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("VMGW_LIVE") != "1", reason="set VMGW_LIVE=1"),
]
TEMPLATES = [t for t in os.environ.get("VMGW_LIVE_TEMPLATES", "ubuntu-24.04").split(",") if t]


async def login_and_run(lease, command: str) -> str:
    ep = next(e for e in lease.access.endpoints if e.proto == "ssh")
    c = lease.credentials
    kw = {"username": c.username, "known_hosts": None, "connect_timeout": 20, "password": c.password}
    kw["client_keys"] = [asyncssh.import_private_key(c.ssh_private_key)] if c.ssh_private_key else None
    last = None
    for _ in range(20):  # sshd may lag the port opening by a few seconds
        try:
            async with asyncssh.connect(ep.host, ep.port, **kw) as conn:
                r = await conn.run(command, check=True)
                return str(r.stdout)
        except (OSError, asyncssh.Error) as e:
            last = e
            await asyncio.sleep(3)
    raise AssertionError(f"ssh to {ep.host}:{ep.port} failed: {last}")


@pytest.mark.parametrize("template_id", TEMPLATES)
async def test_lease_end_to_end(tmp_path, template_id):
    os.chdir(ROOT)
    _load_dotenv(ROOT / ".env")
    config = load_config(ROOT / "config.yaml")
    store = Store(tmp_path / "live.db")
    await store.init()
    mgr = LeaseManager(config, store, build_backends(config))
    me = User(name="live", key_hash="x", created_at=datetime.now(UTC))
    t0 = time.monotonic()
    custom = os.environ.get("VMGW_LIVE_USER")  # "name:password" to exercise custom credentials
    username, password = custom.split(":", 1) if custom else (None, None)
    lease = await mgr.create(me, template_id, ttl_minutes=30, username=username, password=password)
    try:
        lease = await mgr.wait(me, lease.id, timeout=config.template(template_id).ready_timeout_s + 60)
        print(
            f"\n[{template_id}] {lease.state} in {time.monotonic() - t0:.0f}s on {lease.host}/{lease.node}"
            f" ip={lease.access and lease.access.ip} error={lease.error}"
        )
        assert lease.state == LeaseState.READY, lease.error
        if custom:
            assert (lease.credentials.username, lease.credentials.password) == (username, password)
        if not any(e.proto == "ssh" for e in lease.access.endpoints):
            from vmgateway.backends.base import tcp_open

            rdp = next(e for e in lease.access.endpoints if e.proto == "rdp")
            assert await tcp_open(rdp.host, rdp.port), "RDP not reachable"
            print(f"RDP reachable at {rdp.host}:{rdp.port} (no ssh endpoint)")
            return
        cmd = (
            "whoami"
            if config.template(template_id).os == "windows"
            else "uname -a; whoami; df -h / | tail -1"
        )
        out = await login_and_run(lease, cmd)
        print(out)
        assert lease.credentials.username.split("\\")[-1].lower() in out.lower()
    finally:
        done = await mgr.destroy(me, lease.id)
        print(f"[{template_id}] destroy -> {done.state} total {time.monotonic() - t0:.0f}s")
        await mgr.stop()
        await store.close()
    assert done.state == LeaseState.RELEASED
