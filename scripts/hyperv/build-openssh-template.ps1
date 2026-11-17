<#
.SYNOPSIS
  Build a generalized Windows VHDX with OpenSSH Server preinstalled, from an existing OOBE-ready VHDX.

.DESCRIPTION
  Run on the Hyper-V host (PowerShell 7, elevated / Hyper-V Administrators):
    pwsh -File build-openssh-template.ps1 -SourceVhdx C:\VMs\Templates\win11.vhdx -OutVhdx C:\VMs\Templates\win11-openssh.vhdx

  1. Boots a throw-away VM on a differencing disk of SourceVhdx, with an unattend.xml that creates a
     temporary local admin "tplbuild" (random password, never printed).
  2. Over PowerShell Direct: installs OpenSSH Server (sshd set to Automatic) and enables RDP.
  3. A one-shot SYSTEM scheduled task deletes "tplbuild" and its profile, removes the unattend file,
     unregisters itself and runs sysprep /generalize /oobe /shutdown /mode:vm.
  4. Merges the differencing disk into a standalone dynamic VHDX at OutVhdx (read-only) and cleans up.
  The source VHDX is never modified.
#>
param(
    [Parameter(Mandatory)][string]$SourceVhdx,
    [Parameter(Mandatory)][string]$OutVhdx,
    [ValidateSet('arm64', 'amd64')][string]$Arch = 'arm64',
    [string]$Locale = 'en-US',
    [string]$Switch = 'Default Switch',
    [int]$MemoryGB = 8,
    [int]$Cpu = 4,
    [int]$TimeoutMinutes = 60
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [Text.Encoding]::UTF8

function Log($msg) { Write-Host ("[{0:HH:mm:ss}] {1}" -f (Get-Date), $msg) }

if (Test-Path $OutVhdx) { throw "$OutVhdx already exists" }
if (-not (Test-Path $SourceVhdx)) { throw "$SourceVhdx not found" }

$name = 'tplbuild-' + (Get-Random -Maximum 99999)
$work = Join-Path (Split-Path $OutVhdx) "$name-work"
$vhd = Join-Path $work "$name.vhdx"
$user = 'tplbuild'
$chars = 'abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789'
$pw = 'Tb-' + (-join (1..20 | ForEach-Object { $chars[(Get-Random -Maximum $chars.Length)] })) + '9a'
$a = "processorArchitecture=`"$Arch`" publicKeyToken=`"31bf3856ad364e35`" language=`"neutral`" versionScope=`"nonSxS`""
$unattend = @"
<?xml version="1.0" encoding="utf-8"?>
<unattend xmlns="urn:schemas-microsoft-com:unattend" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State">
  <settings pass="specialize">
    <component name="Microsoft-Windows-Shell-Setup" $a><ComputerName>TPLBUILD</ComputerName></component>
    <component name="Microsoft-Windows-TerminalServices-LocalSessionManager" $a><fDenyTSConnections>false</fDenyTSConnections></component>
  </settings>
  <settings pass="oobeSystem">
    <component name="Microsoft-Windows-International-Core" $a>
      <InputLocale>$Locale</InputLocale><SystemLocale>$Locale</SystemLocale><UILanguage>$Locale</UILanguage><UserLocale>$Locale</UserLocale>
    </component>
    <component name="Microsoft-Windows-Shell-Setup" $a>
      <OOBE>
        <HideEULAPage>true</HideEULAPage><HideOEMRegistrationScreen>true</HideOEMRegistrationScreen>
        <HideOnlineAccountScreens>true</HideOnlineAccountScreens><HideWirelessSetupInOOBE>true</HideWirelessSetupInOOBE>
        <HideLocalAccountScreen>true</HideLocalAccountScreen><ProtectYourPC>3</ProtectYourPC>
      </OOBE>
      <UserAccounts><LocalAccounts><LocalAccount wcm:action="add">
        <Name>$user</Name><Group>Administrators</Group><Password><Value>$pw</Value><PlainText>true</PlainText></Password>
      </LocalAccount></LocalAccounts></UserAccounts>
    </component>
  </settings>
</unattend>
"@

$seal = @'
Start-Sleep -Seconds 20
Unregister-ScheduledTask -TaskName vmgw-seal -Confirm:$false -ErrorAction SilentlyContinue
Remove-LocalUser -Name tplbuild -ErrorAction SilentlyContinue
Get-CimInstance Win32_UserProfile | Where-Object LocalPath -like '*\tplbuild*' | Remove-CimInstance -ErrorAction SilentlyContinue
Remove-Item "$env:WINDIR\Panther\unattend.xml" -Force -ErrorAction SilentlyContinue
Remove-Item "$env:WINDIR\Temp\vmgw-seal.ps1" -Force -ErrorAction SilentlyContinue
& "$env:WINDIR\System32\Sysprep\sysprep.exe" /generalize /oobe /shutdown /mode:vm /quiet
'@

$guestPrep = {
    param($sealScript)
    $ErrorActionPreference = 'Stop'
    # RDP is enabled by unattend (fDenyTSConnections); some builds refuse this write even for admins.
    Set-ItemProperty 'HKLM:\System\CurrentControlSet\Control\Terminal Server' -Name fDenyTSConnections -Value 0 `
        -ErrorAction SilentlyContinue
    if (-not (Get-Service sshd -ErrorAction SilentlyContinue)) {
        $cap = Get-WindowsCapability -Online -Name 'OpenSSH.Server*' | Select-Object -First 1
        Add-WindowsCapability -Online -Name $cap.Name | Out-Null
    }
    Set-Service sshd -StartupType Automatic
    if (-not (Get-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -DisplayName 'OpenSSH Server (sshd)' -Protocol TCP `
            -LocalPort 22 -Action Allow -Profile Any | Out-Null
    }
    # Make sure no host keys get baked into the image; sshd regenerates them on first start.
    Stop-Service sshd -ErrorAction SilentlyContinue
    Remove-Item "$env:ProgramData\ssh\ssh_host_*" -Force -ErrorAction SilentlyContinue
    Set-Content -Path "$env:WINDIR\Temp\vmgw-seal.ps1" -Value $sealScript -Encoding UTF8
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' `
        -Argument "-NoProfile -ExecutionPolicy Bypass -File $env:WINDIR\Temp\vmgw-seal.ps1"
    $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddSeconds(30)
    Register-ScheduledTask -TaskName vmgw-seal -Action $action -Principal $principal -Trigger $trigger -Force | Out-Null
    "sshd: $((Get-Service sshd).StartType); build: $([Environment]::OSVersion.Version)"
}

try {
    Log "creating $name from $SourceVhdx"
    New-Item -ItemType Directory -Force $work | Out-Null
    New-VHD -Path $vhd -ParentPath $SourceVhdx -Differencing | Out-Null
    $disk = Mount-VHD -Path $vhd -Passthru | Get-Disk
    try {
        $part = Get-Partition -DiskNumber $disk.Number | Where-Object Type -eq 'Basic' | Sort-Object Size -Descending | Select-Object -First 1
        if (-not $part.DriveLetter) {
            $part | Add-PartitionAccessPath -AssignDriveLetter
            $part = Get-Partition -DiskNumber $disk.Number -PartitionNumber $part.PartitionNumber
        }
        $win = "$($part.DriveLetter):\Windows"
        New-Item -ItemType Directory -Force "$win\Panther" | Out-Null
        [IO.File]::WriteAllText("$win\Panther\unattend.xml", $unattend, [Text.UTF8Encoding]::new($false))
    } finally { Dismount-VHD -Path $vhd }

    New-VM -Name $name -Generation 2 -VHDPath $vhd -SwitchName $Switch -MemoryStartupBytes ($MemoryGB * 1GB) -Path $work | Out-Null
    Set-VMProcessor -VMName $name -Count $Cpu
    Set-VMMemory -VMName $name -DynamicMemoryEnabled $false
    Set-VMFirmware -VMName $name -EnableSecureBoot Off
    Set-VMKeyProtector -VMName $name -NewLocalKeyProtector
    Enable-VMTPM -VMName $name
    Set-VM -Name $name -AutomaticCheckpointsEnabled $false -CheckpointType Disabled
    # Offline during OOBE so its Zero-Day-Patch step cannot install an update and reboot into the
    # interactive account page. PowerShell Direct does not need the network.
    Disconnect-VMNetworkAdapter -VMName $name
    Start-VM -Name $name
    Log "booting through OOBE (network disconnected)"

    $cred = [pscredential]::new($user, (ConvertTo-SecureString $pw -AsPlainText -Force))
    $deadline = (Get-Date).AddMinutes($TimeoutMinutes)
    # Logon over PowerShell Direct works while OOBE is still running; wait for Setup's ImageState instead.
    $state = ''
    while ($true) {
        try {
            $state = Invoke-Command -VMName $name -Credential $cred -ErrorAction Stop -ScriptBlock {
                (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Setup\State').ImageState
            }
            if ($state -eq 'IMAGE_STATE_COMPLETE') { break }
        } catch { $state = $_.Exception.Message }
        if ((Get-Date) -gt $deadline) { throw "OOBE did not finish (last state: $state)" }
        Start-Sleep -Seconds 10
    }
    Connect-VMNetworkAdapter -VMName $name -SwitchName $Switch
    Log "guest up; network connected; installing OpenSSH Server (downloads from Windows Update)"
    # Right after OOBE the guest briefly refuses privileged operations; retry until it settles.
    while ($true) {
        try {
            $out = Invoke-Command -VMName $name -Credential $cred -ScriptBlock $guestPrep -ArgumentList $seal -ErrorAction Stop
            break
        } catch {
            if ((Get-Date) -gt $deadline) { throw "guest preparation failed: $_" }
            Log "prep not ready yet: $($_.Exception.Message) [at: $($_.InvocationInfo.Line.Trim())]; retrying"
            Start-Sleep -Seconds 15
        }
    }
    Log $out

    Log "sealing: sysprep /generalize /oobe /shutdown"
    while ((Get-VM -Name $name).State -ne 'Off') {
        if ((Get-Date) -gt $deadline) { throw "VM did not shut down after sysprep (check C:\Windows\System32\Sysprep\Panther\setuperr.log in the guest)" }
        Start-Sleep -Seconds 10
    }

    Log "merging into $OutVhdx"
    Remove-VM -Name $name -Force
    Convert-VHD -Path $vhd -DestinationPath $OutVhdx -VHDType Dynamic
    Set-ItemProperty -Path $OutVhdx -Name IsReadOnly -Value $true
    Log ("done: {0} ({1:N1} GB)" -f $OutVhdx, ((Get-Item $OutVhdx).Length / 1GB))
} finally {
    if (Get-VM -Name $name -ErrorAction SilentlyContinue) {
        Stop-VM -Name $name -TurnOff -Force -ErrorAction SilentlyContinue
        Remove-VM -Name $name -Force
    }
    if (Test-Path $work) {
        Dismount-VHD -Path $vhd -ErrorAction SilentlyContinue
        Remove-Item -Recurse -Force $work
    }
}
