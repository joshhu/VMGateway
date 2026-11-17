"""Tart backend: macOS / ARM Linux VMs on an Apple Silicon Mac, driven over SSH."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shlex
import time

from ..config import TartHost, TemplateConfig, TemplateSource
from ..errors import BackendError
from ..models import Access, Credentials, Endpoint, NodeCapacity, Resources, VMRef
from .base import DEFAULT_PORTS, MANAGED_PREFIX, tcp_open
from .ssh import SSHRunner, guest_run

log = logging.getLogger(__name__)
GiB = 1024**3
HOST_RESERVE = 0.15  # fraction of host RAM kept for macOS itself


def _check_name(name: str) -> str:
    if not name.startswith(MANAGED_PREFIX) or not all(c.isalnum() or c in "-_" for c in name):
        raise BackendError(f"refusing to touch tart VM {name!r}")
    return name


class TartBackend:
    type = "tart"

    def __init__(self, cfg: TartHost, runner: SSHRunner | None = None, poll_s: float = 3.0):
        self.name = cfg.name
        self.cfg = cfg
        self.ssh = runner or SSHRunner(cfg.ssh)
        self.poll_s = poll_s

    def _tart(self, *args: str) -> str:
        return shlex.join([self.cfg.tart_bin, *args])

    async def _inventory(self) -> tuple[int, list[dict]]:
        """Host RAM in bytes and `tart get` info for every local VM (plus its name)."""
        t = shlex.quote(self.cfg.tart_bin)
        script = (
            "sysctl -n hw.memsize; "
            f"for n in $({t} list --source local --quiet); do "
            f'printf "%s\\t" "$n"; {t} get "$n" --format json | tr -d "\\n"; echo; done'
        )
        out = await self.ssh.run(script, timeout=60)
        lines = [ln for ln in out.splitlines() if ln.strip()]
        memsize = int(lines[0])
        vms = []
        for ln in lines[1:]:
            name, _, js = ln.partition("\t")
            try:
                info = json.loads(js)
            except json.JSONDecodeError:
                continue
            info["Name"] = name
            vms.append(info)
        return memsize, vms

    async def capacity(self) -> list[NodeCapacity]:
        memsize, vms = await self._inventory()
        running = [v for v in vms if v.get("Running")]
        used_gb = sum(v.get("Memory", 0) for v in running) / 1024
        return [
            NodeCapacity(
                host=self.name,
                node=self.name,
                free_memory_gb=memsize * (1 - HOST_RESERVE) / GiB - used_gb,
                running_vms=len(running),
                max_vms=self.cfg.max_vms,
                running_macos=sum(1 for v in running if v.get("OS") == "darwin"),
                max_macos=self.cfg.max_macos_vms,
            )
        ]

    async def provision(
        self,
        name: str,
        template: TemplateConfig,
        source: TemplateSource,
        resources: Resources,
        creds: Credentials,
    ) -> VMRef:
        _check_name(name)
        await self.ssh.run(self._tart("clone", source.ref, name), timeout=900)
        info = json.loads(await self.ssh.run(self._tart("get", name, "--format", "json")))
        args = ["set", name, "--cpu", str(resources.cpu), "--memory", str(resources.memory_gb * 1024)]
        if resources.disk_gb > int(info.get("Disk", 0)):
            args += ["--disk-size", str(resources.disk_gb)]
        await self.ssh.run(self._tart(*args))
        # --vnc-experimental: host-side Virtualization.framework VNC, used for screenshots.
        run = ["run", name, "--no-graphics", "--vnc-experimental"]
        if self.cfg.bridged_interface:
            run.append(f"--net-bridged={self.cfg.bridged_interface}")
        log_file = f"~/.vmgw/{name}.log"
        await self.ssh.run(f"mkdir -p ~/.vmgw && nohup {self._tart(*run)} > {log_file} 2>&1 < /dev/null &")
        return VMRef(host=self.name, node=self.name, vm_id=name, name=name)

    async def _ip(self, name: str) -> str | None:
        for resolver in ("agent", "arp"):
            out = await self.ssh.run(self._tart("ip", "--resolver", resolver, name), timeout=20, check=False)
            ip = out.strip().splitlines()[0] if out.strip() else ""
            if ip and ip[0].isdigit():
                return ip
        return None

    async def wait_ready(
        self, ref: VMRef, template: TemplateConfig, timeout: float, creds: Credentials | None = None
    ) -> Access:
        deadline = time.monotonic() + timeout
        ip = None
        while ip is None:
            try:
                ip = await self._ip(ref.vm_id)
            except BackendError as e:
                log.debug("tart ip %s: %s", ref.vm_id, e)
            if ip is None:
                if time.monotonic() > deadline:
                    tail = await self.ssh.run(f"tail -5 ~/.vmgw/{ref.vm_id}.log", check=False)
                    raise BackendError(f"{ref.vm_id}: no IP within {timeout:.0f}s; tart log: {tail.strip()}")
                await asyncio.sleep(self.poll_s)
        endpoints = [Endpoint(proto=p, host=ip, port=DEFAULT_PORTS[p]) for p in template.endpoints]
        first = endpoints[0]
        while not await tcp_open(first.host, first.port):
            if time.monotonic() > deadline:
                raise BackendError(f"{ref.vm_id}: {first.proto} on {ip}:{first.port} not reachable")
            await asyncio.sleep(self.poll_s)
        if template.os == "macos" and creds and creds.password:
            # Keep the display awake so screenshots are never black.
            await self._guest(ip, creds, "pmset -a displaysleep 0 sleep 0; true", sudo=True)
            if "vnc" in template.endpoints:
                await self._enable_screen_sharing(ip, creds, deadline)
        return Access(ip=ip, endpoints=endpoints)

    async def _guest(self, ip: str, creds: Credentials, command: str, sudo: bool = False) -> str:
        if sudo:
            pw = shlex.quote(creds.password)
            command = f"printf '%s\\n' {pw} | sudo -S -p '' sh -c {shlex.quote(command)}"
        return await guest_run(ip, DEFAULT_PORTS["ssh"], creds.username, creds.password, command)

    async def _enable_screen_sharing(self, ip: str, creds: Credentials, deadline: float) -> None:
        if await tcp_open(ip, DEFAULT_PORTS["vnc"]):
            return
        await self._guest(
            ip,
            creds,
            "launchctl enable system/com.apple.screensharing; "
            "launchctl bootstrap system /System/Library/LaunchDaemons/com.apple.screensharing.plist 2>/dev/null; true",
            sudo=True,
        )
        while not await tcp_open(ip, DEFAULT_PORTS["vnc"]):
            if time.monotonic() > deadline:
                raise BackendError(f"{ip}: screen sharing (VNC 5900) did not start")
            await asyncio.sleep(1)

    async def set_credentials(
        self, ref: VMRef, template: TemplateConfig, access: Access, current: Credentials, desired: Credentials
    ) -> Credentials:
        """Create or update the guest account the user asked for, logging in with the template account."""
        u, pw = desired.username, desired.password
        q = shlex.quote
        if template.os == "macos":
            admin = f"-adminUser {q(current.username)} -adminPassword {q(current.password)}"
            if u == current.username:
                cmd = f"sysadminctl -resetPasswordFor {q(u)} -newPassword {q(pw)} {admin}"
            else:
                cmd = (
                    f"sysadminctl -addUser {q(u)} -password {q(pw)} -admin {admin} && "
                    f"(dseditgroup -o edit -a {q(u)} -t user com.apple.access_ssh 2>/dev/null; true)"
                )
        else:  # linux
            cmd = (
                f"id {q(u)} >/dev/null 2>&1 || useradd -m -s /bin/bash {q(u)}; "
                f"usermod -aG sudo {q(u)} 2>/dev/null || usermod -aG wheel {q(u)}; "
                f"echo {q(f'{u}:{pw}')} | chpasswd"
            )
        await self._guest(access.ip, current, cmd, sudo=True)
        await guest_run(access.ip, DEFAULT_PORTS["ssh"], u, pw, "true")  # prove the new login works
        return Credentials(username=u, password=pw)

    async def screenshot(self, ref: VMRef) -> bytes:
        from .vnc import StreamReaderWriter, capture

        name = _check_name(ref.vm_id)
        log_text = await self.ssh.run(f"cat ~/.vmgw/{name}.log", check=False)
        m = re.search(r"vnc://:([^@\s]*)@127\.0\.0\.1:(\d+)", log_text)
        if not m:
            raise BackendError(f"{name}: no VNC server (VM started without --vnc-experimental?)")
        reader, writer = await self.ssh.open_tcp("127.0.0.1", int(m.group(2)))
        try:
            return await capture(StreamReaderWriter(reader, writer), m.group(1))
        finally:
            writer.close()

    async def destroy(self, ref: VMRef) -> None:
        name = _check_name(ref.vm_id)
        await self.ssh.run(self._tart("stop", name, "--timeout", "20"), timeout=60, check=False)
        out = await self.ssh.run(f"{self._tart('delete', name)} 2>&1; rm -f ~/.vmgw/{name}.log", check=False)
        if out.strip() and "does not exist" not in out:
            # Verify: anything else that left the VM in place is a real failure.
            names = await self.ssh.run(self._tart("list", "--source", "local", "--quiet"))
            if name in names.split():
                raise BackendError(f"tart delete {name}: {out.strip()}")
        log.info("tart: destroyed %s", name)

    async def list_managed(self) -> list[VMRef]:
        out = await self.ssh.run(self._tart("list", "--source", "local", "--quiet"))
        return [
            VMRef(host=self.name, node=self.name, vm_id=n, name=n)
            for n in out.split()
            if n.startswith(MANAGED_PREFIX)
        ]
