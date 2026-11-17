"""Proxmox VE backend (REST API with a scoped API token)."""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import time
from urllib.parse import quote, urlparse

from ..config import ProxmoxHost, TemplateConfig, TemplateSource
from ..errors import BackendError
from ..models import Access, Credentials, Endpoint, NodeCapacity, Resources, VMRef
from .base import DEFAULT_PORTS, MANAGED_PREFIX, tcp_open

log = logging.getLogger(__name__)
GiB = 1024**3
_DISK_KEY = re.compile(r"^(scsi|virtio|sata|ide)\d+$")


def _make_api(cfg: ProxmoxHost):
    from proxmoxer import ProxmoxAPI

    if not cfg.verify_ssl:
        import urllib3

        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    u = urlparse(cfg.url)
    user, token_name = cfg.token_id.split("!", 1)
    return ProxmoxAPI(
        u.hostname,
        port=u.port or 8006,
        user=user,
        token_name=token_name,
        token_value=cfg.token_secret.get_secret_value(),
        verify_ssl=cfg.verify_ssl,
        timeout=60,
    )


def _size_gb(spec: str) -> float:
    m = re.search(r"size=(\d+(?:\.\d+)?)([KMGT])", spec)
    if not m:
        return 0
    n, unit = float(m.group(1)), m.group(2)
    return n * {"K": 1 / 1024**2, "M": 1 / 1024, "G": 1, "T": 1024}[unit]


def boot_disk(config: dict) -> str | None:
    """The disk key the VM boots from (first disk in boot order, else first disk)."""
    m = re.search(r"order=([^,]+)", config.get("boot", ""))
    if m:
        for dev in m.group(1).split(";"):
            if _DISK_KEY.match(dev) and "media=cdrom" not in config.get(dev, ""):
                return dev
    disks = sorted(k for k, v in config.items() if _DISK_KEY.match(k) and "media=cdrom" not in str(v))
    return disks[0] if disks else None


def first_ipv4(ifaces: list[dict]) -> str | None:
    for iface in ifaces:
        name = iface.get("name", "")
        if name == "lo" or name.lower().startswith("loopback"):
            continue
        for a in iface.get("ip-addresses", []):
            ip = a.get("ip-address", "")
            if a.get("ip-address-type") == "ipv4" and not ip.startswith(("127.", "169.254.")):
                return ip
    return None


class ProxmoxBackend:
    type = "proxmox"

    def __init__(self, cfg: ProxmoxHost, api=None, poll_s: float = 2.0):
        self.name = cfg.name
        self.cfg = cfg
        self.api = api if api is not None else _make_api(cfg)
        self.poll_s = poll_s
        self._clone_lock = asyncio.Lock()

    async def _q(self, fn, *args, **kw):
        """Run a blocking proxmoxer call in a thread, mapping its errors to BackendError."""
        try:
            return await asyncio.to_thread(fn, *args, **kw)
        except BackendError:
            raise
        except Exception as e:
            raise BackendError(f"PVE API: {e}") from e

    async def _wait_task(self, node: str, upid: str, timeout: float = 600) -> None:
        deadline = time.monotonic() + timeout
        while True:
            st = await self._q(self.api.nodes(node).tasks(upid).status.get)
            if st.get("status") == "stopped":
                exit_status = str(st.get("exitstatus", ""))
                if exit_status != "OK" and not exit_status.startswith("WARNINGS"):
                    raise BackendError(f"PVE task {upid} failed: {exit_status}")
                return
            if time.monotonic() > deadline:
                raise BackendError(f"PVE task {upid} timed out")
            await asyncio.sleep(self.poll_s)

    async def _vms(self) -> list[dict]:
        return await self._q(self.api.cluster.resources.get, type="vm")

    # ---- Backend protocol -----------------------------------------------------

    async def capacity(self) -> list[NodeCapacity]:
        nodes, vms = await asyncio.gather(self._q(self.api.nodes.get), self._vms())
        out = []
        for n in nodes:
            if n.get("status") != "online":
                continue
            ours = [
                v
                for v in vms
                if v.get("node") == n["node"]
                and v.get("pool") == self.cfg.pool
                and v.get("status") == "running"
                and str(v.get("name", "")).startswith(MANAGED_PREFIX)
            ]
            out.append(
                NodeCapacity(
                    host=self.name,
                    node=n["node"],
                    free_memory_gb=(n.get("maxmem", 0) - n.get("mem", 0)) / GiB,
                    running_vms=len(ours),
                    max_vms=self.cfg.max_vms,
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
        node, tpl = source.node, int(source.ref)
        clone_args: dict = {"name": name, "pool": self.cfg.pool, "full": int(template.full_clone)}
        if template.full_clone and self.cfg.storage:
            clone_args["storage"] = self.cfg.storage
        async with self._clone_lock:  # nextid is only reserved once the clone task holds the lock
            vmid = int(await self._q(self.api.cluster.nextid.get))
            upid = await self._q(self.api.nodes(node).qemu(tpl).clone.post, newid=vmid, **clone_args)
        log.info("pve: cloning %s -> %s (%s) on %s", tpl, vmid, name, node)
        ref = VMRef(host=self.name, node=node, vm_id=str(vmid), name=name)
        try:
            await self._wait_task(node, upid)
            await self._configure_and_start(ref, template, resources, creds)
        except Exception:
            # The cluster resource cache lags; clean up here rather than relying on list_managed.
            try:
                await self.destroy(ref)
            except Exception as e:  # noqa: BLE001
                log.warning("pve: cleanup of %s failed: %s", name, e)
            raise
        return ref

    async def _configure_and_start(
        self, ref: VMRef, template: TemplateConfig, resources: Resources, creds: Credentials
    ) -> None:
        vm = self.api.nodes(ref.node).qemu(int(ref.vm_id))

        settings: dict = {"cores": resources.cpu, "memory": resources.memory_gb * 1024}
        if template.credentials.mode == "cloudinit":
            settings.update(
                ciuser=creds.username,
                cipassword=creds.password,
                ipconfig0="ip=dhcp",
            )
            if creds.ssh_public_key:
                # PVE wants the value URL-encoded *inside* the form field.
                settings["sshkeys"] = quote(creds.ssh_public_key, safe="")
        await self._q(vm.config.put, **settings)

        cfg = await self._q(vm.config.get)
        disk = boot_disk(cfg)
        if disk and resources.disk_gb > _size_gb(cfg.get(disk, "")):
            await self._q(vm.resize.put, disk=disk, size=f"{resources.disk_gb}G")

        upid = await self._q(vm.status.start.post)
        await self._wait_task(ref.node, upid, timeout=120)

    async def wait_ready(
        self, ref: VMRef, template: TemplateConfig, timeout: float, creds: Credentials | None = None
    ) -> Access:
        deadline = time.monotonic() + timeout
        vm = self.api.nodes(ref.node).qemu(int(ref.vm_id))
        ip = None
        while ip is None:
            try:
                res = await self._q(vm.agent("network-get-interfaces").get)
                ip = first_ipv4(res.get("result", []))
            except BackendError:
                pass  # guest agent not up yet
            if ip is None:
                if time.monotonic() > deadline:
                    raise BackendError(f"{ref.name}: no IPv4 from guest agent within {timeout:.0f}s")
                await asyncio.sleep(self.poll_s)
        endpoints = [Endpoint(proto=p, host=ip, port=DEFAULT_PORTS[p]) for p in template.endpoints]
        first = endpoints[0]
        while not await tcp_open(first.host, first.port):
            if time.monotonic() > deadline:
                raise BackendError(f"{ref.name}: {first.proto} port {first.port} on {ip} not reachable")
            await asyncio.sleep(self.poll_s)
        if template.os == "windows":
            # Keep the console awake so screenshots are never a blank screen (best effort).
            try:
                await self._guest_exec(
                    ref,
                    [
                        "cmd.exe",
                        "/c",
                        "powercfg /change monitor-timeout-ac 0 & powercfg /change standby-timeout-ac 0",
                    ],
                    timeout=60,
                )
            except BackendError as e:
                log.info("pve %s: could not disable display timeout: %s", ref.name, e)
        return Access(ip=ip, endpoints=endpoints)

    async def _guest_exec(self, ref: VMRef, argv: list[str], timeout: float = 180) -> str:
        """Run a program inside the guest through qemu-guest-agent and return stdout."""
        vm = self.api.nodes(ref.node).qemu(int(ref.vm_id))
        res = await self._q(vm.agent.exec.post, command=argv)
        pid = res["pid"]
        deadline = time.monotonic() + timeout
        while True:
            st = await self._q(vm.agent("exec-status").get, pid=pid)
            if st.get("exited"):
                if st.get("exitcode", 0) != 0:
                    err = (st.get("err-data") or st.get("out-data") or "").strip()
                    raise BackendError(f"guest exec failed ({st.get('exitcode')}): {err[-500:]}")
                return st.get("out-data", "")
            if time.monotonic() > deadline:
                raise BackendError("guest exec timed out")
            await asyncio.sleep(1)

    async def set_credentials(
        self, ref: VMRef, template: TemplateConfig, access: Access, current: Credentials, desired: Credentials
    ) -> Credentials:
        if template.os != "windows":
            raise BackendError("custom credentials on PVE static templates are supported for Windows only")
        from .hyperv import ps  # PowerShell string quoting

        script = f"""$ErrorActionPreference = 'Stop'
$u = {ps(desired.username)}
$p = ConvertTo-SecureString {ps(desired.password)} -AsPlainText -Force
$acct = Get-LocalUser -Name $u -ErrorAction SilentlyContinue
if ($acct) {{ $acct | Set-LocalUser -Password $p; $acct | Enable-LocalUser }}
else {{ New-LocalUser -Name $u -Password $p -PasswordNeverExpires | Out-Null }}
foreach ($sid in 'S-1-5-32-544', 'S-1-5-32-555') {{
  if (-not (Get-LocalGroupMember -SID $sid -ErrorAction SilentlyContinue | Where-Object {{ $_.Name -like "*\\$u" }})) {{
    Add-LocalGroupMember -SID $sid -Member $u
  }}
}}
'ok'"""
        enc = base64.b64encode(script.encode("utf-16-le")).decode()
        out = await self._guest_exec(
            ref, ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", enc]
        )
        if "ok" not in out:
            raise BackendError(f"setting credentials failed: {out[-300:]}")
        return Credentials(username=desired.username, password=desired.password)

    async def screenshot(self, ref: VMRef) -> bytes:
        import ssl

        from websockets.asyncio.client import connect

        from .vnc import WebSocketStream, capture

        vm = self.api.nodes(ref.node).qemu(int(ref.vm_id))
        proxy = await self._q(vm.vncproxy.post, websocket=1)
        u = urlparse(self.cfg.url)
        url = (
            f"wss://{u.hostname}:{u.port or 8006}/api2/json/nodes/{ref.node}/qemu/{ref.vm_id}/vncwebsocket"
            f"?port={proxy['port']}&vncticket={quote(proxy['ticket'], safe='')}"
        )
        ctx = ssl.create_default_context()
        if not self.cfg.verify_ssl:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        auth = f"PVEAPIToken={self.cfg.token_id}={self.cfg.token_secret.get_secret_value()}"
        try:
            async with connect(
                url,
                additional_headers={"Authorization": auth},
                ssl=ctx,
                subprotocols=["binary"],
                max_size=None,
            ) as ws:
                return await capture(WebSocketStream(ws), proxy["ticket"])
        except BackendError:
            raise
        except Exception as e:
            raise BackendError(f"PVE VNC: {e}") from e

    async def destroy(self, ref: VMRef) -> None:
        vm = self.api.nodes(ref.node).qemu(int(ref.vm_id))
        try:
            cfg = await self._q(vm.config.get)
        except BackendError as e:
            if "does not exist" in str(e) or "500" in str(e) and "not exist" in str(e):
                return
            raise
        name = cfg.get("name", "")
        if not name.startswith(MANAGED_PREFIX):
            raise BackendError(f"refusing to destroy VM {ref.vm_id} named {name!r} (not {MANAGED_PREFIX}*)")
        st = await self._q(vm.status.current.get)
        if st.get("status") != "stopped":
            upid = await self._q(vm.status.stop.post)
            await self._wait_task(ref.node, upid, timeout=120)
        upid = await self._q(vm.delete, purge=1, **{"destroy-unreferenced-disks": 1})
        await self._wait_task(ref.node, upid, timeout=300)
        log.info("pve: destroyed %s (%s)", ref.vm_id, name)

    async def list_managed(self) -> list[VMRef]:
        # Per-node lists are live; cluster/resources is a cache that lags new clones by seconds.
        nodes = [n["node"] for n in await self._q(self.api.nodes.get) if n.get("status") == "online"]
        lists = await asyncio.gather(*(self._q(self.api.nodes(n).qemu.get) for n in nodes))
        return [
            VMRef(host=self.name, node=n, vm_id=str(v["vmid"]), name=v["name"])
            for n, vms in zip(nodes, lists, strict=True)
            for v in vms
            if str(v.get("name", "")).startswith(MANAGED_PREFIX) and not v.get("template")
        ]
