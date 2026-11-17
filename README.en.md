# VMGateway

[繁體中文](README.md) | **English**

Give your coding agent any operating system, on demand.

VMGateway is a small lease-based service that sits between AI agents (Claude Code or anything that speaks
MCP or HTTP) and your own hypervisors. An agent asks for "Windows 11 on ARM, 4 vCPU, 8 GB, for two hours";
the gateway picks a host with room, clones a template, injects fresh credentials, waits until the machine
is reachable, and hands back the IP, ports and login. When the lease ends, the VM is destroyed.

Agents never see hypervisor passwords, host IPs or API tokens. Each user has an API key.

```
 Claude Code ──MCP──┐                         ┌── Proxmox VE cluster  (Linux / Windows x86_64)
 scripts ─────REST──┼──► VMGateway ──────────┼── Tart on a Mac        (macOS / Linux ARM64)
                    │   leases · scheduler    └── Hyper-V on Windows   (Windows on ARM)
                    │   reaper · SQLite
```

## Features

- **One API, three hypervisors.** Proxmox VE (REST API token), Tart (SSH), Hyper-V (SSH + PowerShell 7).
- **Fast clones.** PVE linked clones, Tart APFS clones, Hyper-V differencing disks. Measured on the
  author's lab: Ubuntu on PVE ~19 s, Ubuntu ARM on Tart ~13 s, macOS ~26 s, Windows 11 on PVE ~25 s,
  Windows 11 ARM on Hyper-V (first boot through OOBE) ~3 min.
- **Unattended credentials.** cloud-init (Linux), unattend.xml (Windows VHDX), or a template's built-in
  account. Users can also choose their own username/password; the gateway creates the account after boot.
- **Leases.** TTL 10 min – 72 h (default 2 h), renewable. A reaper destroys expired VMs, and a reconciler
  removes orphaned `vmgw-*` VMs after crashes. VMs without the `vmgw-` prefix are never touched.
- **Scheduling.** Picks the node with the most free memory, respects per-node `max_vms` and Apple's
  two-running-macOS-VMs limit (counting VMs the gateway did not create).
- **Multi-user.** Per-user API keys (stored hashed), per-user lease quotas, admins see everything.
- **Screenshots.** `screenshot_vm` returns the VM's console as an image, even before anyone logs in:
  PVE through its VNC websocket, Tart through Virtualization.framework's VNC server, Hyper-V through WMI.
  Agents can see GUI state without an RDP/VNC client. Displays are kept awake so captures are never blank.
- **MCP + REST.** MCP over Streamable HTTP at `/mcp` (official Python SDK v2), REST at `/v1`.

## Quick start

```bash
git clone https://github.com/joshhu/VMGateway && cd VMGateway
uv sync
cp config.example.yaml config.yaml   # describe your hosts and templates
cp .env.example .env                 # secrets referenced by config.yaml
uv run vmgw user add alice --admin   # prints alice's API key once
uv run vmgw serve
```

Connect Claude Code:

```bash
claude mcp add --transport http vmgw http://<gateway>:8000/mcp \
  --header "Authorization: Bearer <api key>"
```

Then just ask: *"Spin up a Windows 11 ARM VM, take a screenshot of the Settings app, then destroy it."*

### Docker

```bash
mkdir -p data keys && cp ~/.ssh/<mac key> ~/.ssh/<windows key> keys/
docker compose up -d --build
docker compose exec vmgateway vmgw user add alice --admin
```

In `config.yaml`, point `key_path` at `/keys/<file>`.

## Preparing the hypervisors

The gateway clones existing templates. These helpers create what it needs.

### Proxmox VE

```bash
# Least-privilege user + API token. Leased VMs live in pool "vmgw", templates in "vmgw-templates".
ssh root@pve 'bash -s' < scripts/pve/bootstrap-token.sh        # prints TOKEN_SECRET=...  → .env

# Ubuntu 24.04 / 22.04 cloud-init templates with qemu-guest-agent baked in (edit the node list first).
ssh root@pve 'bash -s' < scripts/pve/build-ubuntu-templates.sh

# Existing templates you want to offer must be in the template pool:
ssh root@pve 'pveum pool modify vmgw-templates --vms 102,210'
```

`local-lvm` is node-local, so linked clones only work on the node that holds the template: list one
source per node in the template's `sources`. Windows templates need the QEMU guest agent installed
(the gateway reads the IP from it).

### Tart (macOS host)

- Install Tart, enable Remote Login for the gateway's SSH key.
- Bridged networking (`bridged_interface: en0`) gives VMs LAN addresses agents can reach.
- IPs are discovered via the Tart guest agent, falling back to ARP.
- For macOS templates with a `vnc` endpoint, the gateway turns on Screen Sharing in each new VM.
- VMs run with `--vnc-experimental` (host-side VNC, no window on the Mac) so `screenshot_vm` works for
  every guest; the gateway reaches it through its SSH connection.

### Hyper-V (Windows host)

- Enable OpenSSH Server, make PowerShell 7 available, and authorize the gateway's key for an account in
  *Hyper-V Administrators* and *Administrators*.
- Template: a generalized (OOBE) Windows VHDX. Mark it read-only; every VM is a differencing disk on top.
- The gateway writes `unattend.xml` into the new disk (skip OOBE, create the lease's admin account), boots
  it with the network **disconnected** (otherwise OOBE may install a zero-day update, reboot and stop at the
  interactive account page), waits over PowerShell Direct, connects the network, then enables RDP (and
  OpenSSH if requested).
- OpenSSH Server, when a template lists the `ssh` endpoint but the VHDX lacks it, is installed from Windows
  Update on first boot (several minutes). Prefer a template with OpenSSH preinstalled.
- The lease unattend sets `LocalAccountTokenFilterPolicy=1` so the lease's local admin gets a full token over
  PowerShell Direct (needed for guest setup), and disables display/standby timeouts so screenshots stay live.

## Configuration

See [`config.example.yaml`](config.example.yaml). Key ideas:

| Section | Purpose |
|---|---|
| `server` | listen address, `public_url`, `allowed_hosts` (MCP DNS-rebinding allowlist), SQLite path |
| `policy` | TTL bounds and default, per-user lease quota, reaper/reconcile intervals |
| `hosts` | one entry per hypervisor (`proxmox`, `tart`, `hyperv`); secrets via `*_env` variable names |
| `templates` | catalog agents choose from: OS, arch, sources (host/node/ref), default size, credential mode, endpoints |

Credential modes:

| Mode | Used for | Credentials |
|---|---|---|
| `cloudinit` | PVE Linux | random password + ed25519 key per lease (or user-chosen) |
| `unattend` | Hyper-V Windows | random admin password per lease (or user-chosen) |
| `static` | templates with a fixed account | template account; user-chosen account created after boot |

## MCP tools

| Tool | What it does |
|---|---|
| `list_templates` | catalog with OS, arch, default resources |
| `list_hosts` | hosts, free memory, which templates they serve |
| `create_vm(template, cpu?, memory_gb?, disk_gb?, ttl_minutes?, username?, password?, wait=true)` | lease a VM; blocks until reachable |
| `get_vm(lease_id, wait_s?)` | state, IP, endpoints, credentials, connect hints |
| `list_vms(all_users?)` | your active leases |
| `renew_vm(lease_id, ttl_minutes)` | extend |
| `screenshot_vm(lease_id, max_width=1600)` | the VM's screen as a PNG image |
| `destroy_vm(lease_id)` | end the lease now |
| `create_api_key(name, admin?, max_leases?)` | admin: new key + ready-to-paste client setup |
| `list_api_keys()` / `revoke_api_key(name)` | admin: manage keys |

## REST API

All endpoints except `/healthz` need `Authorization: Bearer <api key>`.

| Method | Path | |
|---|---|---|
| GET | `/v1/templates` | catalog |
| GET | `/v1/hosts` | capacity |
| POST | `/v1/leases` | `{template, cpu?, memory_gb?, disk_gb?, ttl_minutes?, username?, password?, wait?}` |
| GET | `/v1/leases` | your active leases (`?all=true` for admins, `?include_inactive=true`) |
| GET | `/v1/leases/{id}` | details (`?include_key=true` returns the SSH private key, `?wait_s=` blocks) |
| POST | `/v1/leases/{id}/renew` | `{ttl_minutes}` |
| GET | `/v1/leases/{id}/screenshot` | PNG of the console (`?max_width=`, `0` = full size) |
| DELETE | `/v1/leases/{id}` | destroy |

Errors are JSON: `{"error": "no_capacity", "detail": "..."}`.

## User management

The easiest way: as an admin, just ask your agent — *"create an API key for my laptop"*. The
`create_api_key` tool returns the key plus a one-line `claude mcp add ...` command (and VS Code, Cursor,
Claude Desktop configs) to paste on the new machine. `list_api_keys` and `revoke_api_key` do the rest.
The same is available over REST (`POST/GET /v1/users`, `DELETE /v1/users/{name}`, admin only) and the CLI:

```bash
vmgw user add bob --max-leases 3
vmgw user list
vmgw user revoke bob
```

The last active admin cannot be revoked.

## Development

```bash
uv run pytest                       # unit tests (fake backend, recorded hypervisor calls)
VMGW_LIVE=1 VMGW_LIVE_TEMPLATES=ubuntu-24.04,macos uv run pytest -m live -s   # real hypervisors
VMGW_LIVE_USER='me:Secret-Pass1' ...                                         # with custom credentials
```

## Security notes

- Run exactly one gateway per set of hosts: the reconciler deletes `vmgw-*` VMs it has no lease for.
- Lease credentials are stored in the gateway's SQLite file; protect `data/`.
- The PVE token can only manage VMs in pool `vmgw` and clone templates in `vmgw-templates`.
- The gateway trusts the LAN: SSH host keys are not verified unless `known_hosts` is set per host.
- Hyper-V leases enable `LocalAccountTokenFilterPolicy` (remote admin for local accounts) inside the guest;
  each lease has its own random password.

## License

MIT
