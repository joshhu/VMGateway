"""Hyper-V backend: Windows VMs from a sysprepped VHDX, driven over SSH + PowerShell 7.

- Disk: differencing VHDX on top of the (read-only) template → new VM in seconds.
- Credentials: unattend.xml injected into the new disk skips OOBE and creates a per-lease local admin.
- Readiness: PowerShell Direct (Invoke-Command -VMName) with those credentials succeeds only once
  OOBE is done; the same session enables RDP / OpenSSH inside the guest.
- Network: NAT (Default Switch) + `netsh interface portproxy` on the host, so agents reach
  <public_ip>:<port>. Port mapping lives in a firewall rule named after the VM.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from xml.sax.saxutils import escape

from ..config import HyperVHost, TemplateConfig, TemplateSource
from ..errors import BackendError
from ..models import Access, Credentials, Endpoint, NodeCapacity, Resources, VMRef
from .base import DEFAULT_PORTS, MANAGED_PREFIX, tcp_open
from .ssh import SSHRunner

log = logging.getLogger(__name__)


def ps(s: str) -> str:
    """Single-quoted PowerShell string literal."""
    return "'" + str(s).replace("'", "''") + "'"


def _check_name(name: str) -> str:
    if not name.startswith(MANAGED_PREFIX) or not all(c.isalnum() or c == "-" for c in name):
        raise BackendError(f"refusing to touch Hyper-V VM {name!r}")
    return name


_COMPONENT = 'processorArchitecture="{arch}" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS"'


def unattend_xml(computer_name: str, creds: Credentials, arch: str = "arm64", locale: str = "en-US") -> str:
    a = _COMPONENT.format(arch="arm64" if arch == "arm64" else "amd64")
    user, pw, loc = escape(creds.username), escape(creds.password or ""), escape(locale)
    return f"""<?xml version="1.0" encoding="utf-8"?>
<unattend xmlns="urn:schemas-microsoft-com:unattend" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State">
  <settings pass="specialize">
    <component name="Microsoft-Windows-Shell-Setup" {a}>
      <ComputerName>{escape(computer_name)}</ComputerName>
    </component>
    <component name="Microsoft-Windows-TerminalServices-LocalSessionManager" {a}>
      <fDenyTSConnections>false</fDenyTSConnections>
    </component>
  </settings>
  <settings pass="oobeSystem">
    <component name="Microsoft-Windows-International-Core" {a}>
      <InputLocale>{loc}</InputLocale>
      <SystemLocale>{loc}</SystemLocale>
      <UILanguage>{loc}</UILanguage>
      <UserLocale>{loc}</UserLocale>
    </component>
    <component name="Microsoft-Windows-Shell-Setup" {a}>
      <OOBE>
        <HideEULAPage>true</HideEULAPage>
        <HideOEMRegistrationScreen>true</HideOEMRegistrationScreen>
        <HideOnlineAccountScreens>true</HideOnlineAccountScreens>
        <HideWirelessSetupInOOBE>true</HideWirelessSetupInOOBE>
        <HideLocalAccountScreen>true</HideLocalAccountScreen>
        <ProtectYourPC>3</ProtectYourPC>
      </OOBE>
      <UserAccounts>
        <LocalAccounts>
          <LocalAccount wcm:action="add">
            <Name>{user}</Name>
            <Group>Administrators</Group>
            <Password><Value>{pw}</Value><PlainText>true</PlainText></Password>
          </LocalAccount>
        </LocalAccounts>
      </UserAccounts>
    </component>
  </settings>
</unattend>
"""


def allocate_ports(used: set[int], lo: int, hi: int, n: int) -> list[int]:
    out = []
    for p in range(lo, hi + 1):
        if p not in used:
            out.append(p)
            if len(out) == n:
                return out
    raise BackendError(f"no free forwarding ports in {lo}-{hi}")


def parse_mapping(description: str) -> dict[str, int]:
    """'rdp=40001;ssh=40002' -> {'rdp': 40001, 'ssh': 40002}"""
    out = {}
    for part in description.split(";"):
        k, _, v = part.partition("=")
        if k and v.isdigit():
            out[k] = int(v)
    return out


GUEST_SETUP = r"""
param([bool]$wantSsh, [string]$pubKey)
Set-ItemProperty 'HKLM:\System\CurrentControlSet\Control\Terminal Server' -Name fDenyTSConnections -Value 0
Enable-NetFirewallRule -Group '@FirewallAPI.dll,-28752' -ErrorAction SilentlyContinue
Set-NetFirewallProfile -Profile Domain,Public,Private -Enabled True
New-NetFirewallRule -Name vmgw-rdp -DisplayName 'RDP (vmgw)' -Protocol TCP -LocalPort 3389 -Action Allow -Profile Any -ErrorAction SilentlyContinue | Out-Null
$ssh = 'not requested'
if ($wantSsh) {
  $svc = Get-Service sshd -ErrorAction SilentlyContinue
  if (-not $svc) {
    $cap = Get-WindowsCapability -Online -Name 'OpenSSH.Server*' -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($cap) { Add-WindowsCapability -Online -Name $cap.Name -ErrorAction SilentlyContinue | Out-Null }
    $svc = Get-Service sshd -ErrorAction SilentlyContinue
  }
  if ($svc) {
    Set-Service sshd -StartupType Automatic; Start-Service sshd
    New-NetFirewallRule -Name vmgw-ssh -DisplayName 'OpenSSH (vmgw)' -Protocol TCP -LocalPort 22 -Action Allow -Profile Any -ErrorAction SilentlyContinue | Out-Null
    if ($pubKey) {
      $f = Join-Path $env:ProgramData 'ssh/administrators_authorized_keys'
      Add-Content -Path $f -Value $pubKey -Encoding ascii
      icacls $f /inheritance:r /grant '*S-1-5-32-544:F' /grant '*S-1-5-18:F' | Out-Null
    }
    $ssh = 'running'
  } else { $ssh = 'unavailable' }
}
# Keep the console awake so screenshots are never a blank screen.
foreach ($k in 'monitor-timeout-ac', 'monitor-timeout-dc', 'standby-timeout-ac', 'standby-timeout-dc') { powercfg /change $k 0 }
try {
  $max = (Get-PartitionSupportedSize -DriveLetter C).SizeMax
  if ($max -gt (Get-Partition -DriveLetter C).Size + 1GB) { Resize-Partition -DriveLetter C -Size $max }
} catch {}
[pscustomobject]@{ ok = $true; ssh = $ssh; host = $env:COMPUTERNAME } | ConvertTo-Json -Compress
"""


SCREENSHOT_PS51 = r"""
$ns = 'root/virtualization/v2'
$vm = Get-WmiObject -Namespace $ns -Class Msvm_ComputerSystem -Filter "ElementName='__NAME__'"
if (-not $vm) { throw 'VM not found' }
$head = Get-WmiObject -Namespace $ns -Class Msvm_VideoHead | Where-Object { $_.SystemName -eq $vm.Name } | Select-Object -First 1
$w = [int]$head.CurrentHorizontalResolution; $h = [int]$head.CurrentVerticalResolution
if ($w -le 0 -or $h -le 0) { $w = 1024; $h = 768 }
$vssd = $vm.GetRelated('Msvm_VirtualSystemSettingData') | Where-Object { $_.VirtualSystemType -eq 'Microsoft:Hyper-V:System:Realized' }
$svc = Get-WmiObject -Namespace $ns -Class Msvm_VirtualSystemManagementService
$r = $svc.GetVirtualSystemThumbnailImage($vssd, $w, $h)
if ($r.ReturnValue -ne 0 -or -not $r.ImageData) { throw "thumbnail failed: $($r.ReturnValue)" }
Add-Type -AssemblyName System.Drawing
$bmp = New-Object System.Drawing.Bitmap($w, $h, [System.Drawing.Imaging.PixelFormat]::Format16bppRgb565)
$bits = $bmp.LockBits((New-Object System.Drawing.Rectangle(0, 0, $w, $h)), 'WriteOnly', $bmp.PixelFormat)
$n = [Math]::Min($r.ImageData.Length, $bits.Stride * $h)
[System.Runtime.InteropServices.Marshal]::Copy([byte[]]$r.ImageData, 0, $bits.Scan0, $n)
$bmp.UnlockBits($bits)
$ms = New-Object System.IO.MemoryStream
$bmp.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)
[Convert]::ToBase64String($ms.ToArray())
"""


class HyperVBackend:
    type = "hyperv"

    def __init__(self, cfg: HyperVHost, runner: SSHRunner | None = None, poll_s: float = 5.0):
        self.name = cfg.name
        self.cfg = cfg
        self.ssh = runner or SSHRunner(cfg.ssh)
        self.poll_s = poll_s
        self._port_lock = asyncio.Lock()

    async def _json(self, script: str, timeout: float = 300):
        out = (await self.ssh.run_ps(script, timeout)).strip()
        return json.loads(out) if out else None

    async def capacity(self) -> list[NodeCapacity]:
        d: dict = await self._json(
            "$os = Get-CimInstance Win32_OperatingSystem\n"
            "$vms = @(Get-VM | Where-Object State -eq 'Running')\n"
            "[pscustomobject]@{ free = $os.FreePhysicalMemory * 1KB; running = $vms.Count } | ConvertTo-Json -Compress"
        )
        return [
            NodeCapacity(
                host=self.name,
                node=self.name,
                free_memory_gb=d["free"] / 1024**3 - 4,  # keep 4 GB for the host
                running_vms=d["running"],
                max_vms=self.cfg.max_vms,
            )
        ]

    async def _used_ports(self) -> set[int]:
        out = await self.ssh.run_ps(
            "netsh interface portproxy show v4tov4\n"
            "Get-NetFirewallRule -Name 'vmgw-*' -ErrorAction SilentlyContinue | ForEach-Object { $_.Description }"
        )
        used: set[int] = set()
        for line in out.splitlines():
            cols = line.split()
            if len(cols) >= 2 and cols[1].isdigit():
                used.add(int(cols[1]))
            used.update(parse_mapping(line.strip()).values())
        return used

    async def provision(
        self,
        name: str,
        template: TemplateConfig,
        source: TemplateSource,
        resources: Resources,
        creds: Credentials,
    ) -> VMRef:
        _check_name(name)
        xml = unattend_xml(name.upper()[:15], creds, template.arch, template.windows_locale)
        protos = [p for p in template.endpoints if p in DEFAULT_PORTS]
        async with self._port_lock:
            lo, hi = self.cfg.port_range
            ports = allocate_ports(await self._used_ports(), lo, hi, len(protos))
            mapping = ";".join(f"{p}={port}" for p, port in zip(protos, ports, strict=True))
            port_list = ",".join(str(p) for p in ports)
            await self.ssh.run_ps(
                f"New-NetFirewallRule -Name {ps(name)} -DisplayName {ps(name)} -Description {ps(mapping)}"
                f" -Direction Inbound -Protocol TCP -LocalPort {port_list} -Action Allow -Profile Any | Out-Null"
            )
        script = f"""
$name = {ps(name)}; $parent = {ps(source.ref)}; $root = {ps(self.cfg.vm_root)}
$dir = Join-Path $root $name
New-Item -ItemType Directory -Force $dir | Out-Null
$vhd = Join-Path $dir "$name.vhdx"
New-VHD -Path $vhd -ParentPath $parent -Differencing | Out-Null
if ({resources.disk_gb}GB -gt (Get-VHD $vhd).Size) {{ Resize-VHD -Path $vhd -SizeBytes {resources.disk_gb}GB }}
$disk = Mount-VHD -Path $vhd -Passthru | Get-Disk
try {{
  $part = Get-Partition -DiskNumber $disk.Number | Where-Object Type -eq 'Basic' | Sort-Object Size -Descending | Select-Object -First 1
  if (-not $part.DriveLetter) {{
    $part | Add-PartitionAccessPath -AssignDriveLetter
    $part = Get-Partition -DiskNumber $disk.Number -PartitionNumber $part.PartitionNumber
  }}
  $win = "$($part.DriveLetter):\\Windows"
  if (-not (Test-Path $win)) {{ throw "no Windows directory on template partition" }}
  New-Item -ItemType Directory -Force "$win\\Panther" | Out-Null
  [IO.File]::WriteAllText("$win\\Panther\\unattend.xml", {ps(xml)}, [Text.UTF8Encoding]::new($false))
}} finally {{ Dismount-VHD -Path $vhd }}
New-VM -Name $name -Generation 2 -VHDPath $vhd -SwitchName {ps(self.cfg.switch)} -MemoryStartupBytes {resources.memory_gb}GB -Path $root | Out-Null
Set-VMProcessor -VMName $name -Count {resources.cpu}
Set-VMMemory -VMName $name -DynamicMemoryEnabled $false
Set-VMFirmware -VMName $name -EnableSecureBoot Off
Set-VMKeyProtector -VMName $name -NewLocalKeyProtector
Enable-VMTPM -VMName $name
Set-VM -Name $name -AutomaticCheckpointsEnabled $false -CheckpointType Disabled
Enable-VMIntegrationService -VMName $name -Name 'Guest Service Interface' -ErrorAction SilentlyContinue
# Offline during OOBE: otherwise OOBE's Zero-Day-Patch step can install an update, reboot and
# land on the interactive account page. PowerShell Direct needs no network; we connect it once ready.
Disconnect-VMNetworkAdapter -VMName $name
Start-VM -Name $name
"""
        try:
            await self.ssh.run_ps(script, timeout=600)
        except BackendError:
            await self.destroy(VMRef(host=self.name, node=self.name, vm_id=name, name=name))
            raise
        return VMRef(host=self.name, node=self.name, vm_id=name, name=name, extra={"ports": mapping})

    async def wait_ready(
        self, ref: VMRef, template: TemplateConfig, timeout: float, creds: Credentials | None = None
    ) -> Access:
        if creds is None or not creds.password:
            raise BackendError("Hyper-V readiness needs the lease credentials")
        name = _check_name(ref.vm_id)
        deadline = time.monotonic() + timeout
        want_ssh = "$true" if "ssh" in template.endpoints else "$false"
        cred = (
            f"$cred = [pscredential]::new({ps(creds.username)},"
            f" (ConvertTo-SecureString {ps(creds.password)} -AsPlainText -Force))\n"
        )
        # 1) Wait for OOBE to *finish*. The lease account can log on over PowerShell Direct while OOBE is
        #    still running, so check Windows Setup's ImageState rather than mere logon success.
        alive = cred + (
            f"Invoke-Command -VMName {ps(name)} -Credential $cred -ErrorAction Stop -ScriptBlock {{"
            " (Get-ItemProperty 'HKLM:/SOFTWARE/Microsoft/Windows/CurrentVersion/Setup/State').ImageState }"
        )
        state = ""
        while True:
            try:
                state = (await self.ssh.run_ps(alive, timeout=120)).strip()
                if "IMAGE_STATE_COMPLETE" in state:
                    break
            except BackendError as e:
                state = str(e)[-200:]
            if time.monotonic() > deadline:
                raise BackendError(f"{name}: OOBE not finished within {timeout:.0f}s (last: {state})")
            await asyncio.sleep(self.poll_s)
        # 2) Now it is safe to give the guest a network.
        await self.ssh.run_ps(
            f"Connect-VMNetworkAdapter -VMName {ps(name)} -SwitchName {ps(self.cfg.switch)}", timeout=60
        )
        # 3) Configure RDP / OpenSSH / power settings inside the guest.
        probe = cred + (
            f"Invoke-Command -VMName {ps(name)} -Credential $cred -ErrorAction Stop `\n"
            f"  -ScriptBlock ([scriptblock]::Create({ps(GUEST_SETUP)}))"
            f" -ArgumentList {want_ssh}, {ps(creds.ssh_public_key or '')}\n"
        )
        status = None
        last_err = ""
        while status is None:
            try:
                out = (await self.ssh.run_ps(probe, timeout=900)).strip().splitlines()
                status = json.loads(out[-1])
            except (BackendError, json.JSONDecodeError, IndexError) as e:
                last_err = str(e)[-300:]
                if time.monotonic() > deadline:
                    raise BackendError(f"{name}: guest not ready within {timeout:.0f}s: {last_err}") from e
                await asyncio.sleep(self.poll_s)
        log.info("hyperv %s guest ready: %s", name, status)

        ip = None
        while ip is None:
            ip = (
                await self.ssh.run_ps(
                    f"(Get-VMNetworkAdapter -VMName {ps(name)}).IPAddresses | Where-Object {{"
                    " $_ -match '^\\d+\\.\\d+\\.\\d+\\.\\d+$' -and $_ -notlike '169.254.*' } | Select-Object -First 1"
                )
            ).strip() or None
            if ip is None:
                if time.monotonic() > deadline:
                    raise BackendError(f"{name}: no IPv4 on VM network adapter")
                await asyncio.sleep(self.poll_s)

        mapping = parse_mapping(ref.extra.get("ports", ""))
        cmds = [
            f"netsh interface portproxy add v4tov4 listenaddress=0.0.0.0 listenport={port}"
            f" connectaddress={ip} connectport={DEFAULT_PORTS[proto]} | Out-Null"
            for proto, port in mapping.items()
        ]
        await self.ssh.run_ps("\n".join(cmds))
        endpoints = [
            Endpoint(proto=proto, host=self.cfg.public_ip, port=port)
            for proto, port in mapping.items()
            if proto != "ssh" or status.get("ssh") == "running"
        ]
        first = endpoints[0]
        while not await tcp_open(first.host, first.port):
            if time.monotonic() > deadline:
                raise BackendError(f"{name}: {first.proto} via {first.host}:{first.port} not reachable")
            await asyncio.sleep(self.poll_s)
        return Access(ip=ip, endpoints=endpoints)

    async def screenshot(self, ref: VMRef) -> bytes:
        import base64

        name = _check_name(ref.vm_id)
        out = await self.ssh.run_ps51(SCREENSHOT_PS51.replace("__NAME__", name), timeout=60)
        try:
            return base64.b64decode(out.strip().splitlines()[-1])
        except (ValueError, IndexError) as e:
            raise BackendError(f"{name}: bad screenshot data") from e

    async def destroy(self, ref: VMRef) -> None:
        name = _check_name(ref.vm_id)
        script = f"""
$name = {ps(name)}; $root = {ps(self.cfg.vm_root)}
$rule = Get-NetFirewallRule -Name $name -ErrorAction SilentlyContinue
if ($rule) {{
  foreach ($p in ($rule.Description -split ';')) {{
    $port = ($p -split '=')[1]
    if ($port) {{ netsh interface portproxy delete v4tov4 listenaddress=0.0.0.0 listenport=$port | Out-Null }}
  }}
  Remove-NetFirewallRule -Name $name
}}
$vm = Get-VM -Name $name -ErrorAction SilentlyContinue
if ($vm) {{
  if ($vm.State -ne 'Off') {{ Stop-VM -Name $name -TurnOff -Force }}
  Remove-VM -Name $name -Force
}}
$dir = Join-Path $root $name
if (Test-Path $dir) {{
  Get-ChildItem $dir -Recurse -Filter *.vhdx | ForEach-Object {{ Dismount-VHD -Path $_.FullName -ErrorAction SilentlyContinue }}
  Remove-Item -Recurse -Force $dir
}}
"""
        await self.ssh.run_ps(script, timeout=300)
        log.info("hyperv: destroyed %s", name)

    async def list_managed(self) -> list[VMRef]:
        out = await self.ssh.run_ps(
            "Get-VM | Where-Object Name -like 'vmgw-*' | ForEach-Object Name\n"
            f"Get-ChildItem {ps(self.cfg.vm_root)} -Directory -Filter 'vmgw-*' -ErrorAction SilentlyContinue"
            " | ForEach-Object Name\n"
            "Get-NetFirewallRule -Name 'vmgw-*' -ErrorAction SilentlyContinue | ForEach-Object Name"
        )
        names = sorted({n.strip() for n in out.splitlines() if n.strip().startswith(MANAGED_PREFIX)})
        names = [n for n in names if n not in ("vmgw-rdp", "vmgw-ssh")]
        return [VMRef(host=self.name, node=self.name, vm_id=n, name=n) for n in names]
