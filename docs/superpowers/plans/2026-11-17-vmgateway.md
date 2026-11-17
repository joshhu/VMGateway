# VMGateway Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A lease-based gateway (REST + MCP) that lets coding agents get a fresh VM of any OS from PVE, Tart, or Hyper-V, and destroys it when the lease ends.

**Architecture:** One FastAPI process. `/v1/*` REST and `/mcp` (MCP Streamable HTTP, `MCPServer` from mcp 2.x) share a core: `LeaseManager` (state machine, reaper, reconcile) → `Scheduler` (pick host/node) → `Backend` implementations (PVE via REST API token, Tart via SSH, Hyper-V via SSH+PowerShell). SQLite stores users and leases.

**Tech Stack:** Python 3.12, uv, FastAPI 0.163, uvicorn, mcp 2.3 (`MCPServer`, `TokenVerifier`), proxmoxer 2.3 + requests, asyncssh 2.24, aiosqlite, pydantic 2 + PyYAML, typer, pytest + pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-11-17-vmgateway-design.md`

**Environment notes (learned while building against a real lab):**

- PVE `local-lvm` is node-local → linked clones only on the node that holds the template; keep one template per node.
  The scoped token (`scripts/pve/bootstrap-token.sh`) can only touch pool `vmgw` (leases) and clone from `vmgw-templates`.
- PVE API quirks: `sshkeys` must be URL-encoded *inside* the form value; `ipconfig0=ip=dhcp` must be form-encoded;
  task exit status `WARNINGS: n` means success; `cluster/resources` lags new clones by seconds (use per-node lists).
- Tart: Apple allows 2 running macOS guests per host (count all of them, not only ours). Bridged VMs: resolve the IP
  via the Tart guest agent, fall back to ARP.
- Hyper-V on a Wi-Fi-only host: use the NAT `Default Switch` + `netsh interface portproxy`. PowerShell Direct is the
  reliable readiness signal after unattended OOBE.

**Credential modes** (refines spec): `cloudinit` (PVE Linux, per-lease user/password/SSH key), `unattend` (Hyper-V, per-lease admin password injected via unattend.xml), `static` (PVE Windows, Tart macOS; from env vars named in config).

---

## File Structure

```
pyproject.toml
config.example.yaml            generic example (committed)
config.yaml                    real config (gitignored)
.env.example / .env            secrets (only .env.example committed)
src/vmgateway/
  __init__.py
  config.py        pydantic models for config.yaml; load_config(path) resolves *_env fields from os.environ
  models.py        LeaseState, Resources, VMRef, Endpoint, Credentials, Lease, NodeCapacity, Access
  store.py         Store(aiosqlite): users + leases CRUD
  auth.py          generate_key(), hash_key(), ApiKeyVerifier (TokenVerifier), FastAPI dependency
  creds.py         make_credentials(template, lease_id) -> Credentials (random pw, ed25519 keypair)
  scheduler.py     Scheduler.pick(template, resources, reserved) -> (backend, node)
  leases.py        LeaseManager: create / get / list / renew / destroy / wait / reaper / reconcile
  errors.py        GatewayError(code, detail, status)
  backends/
    __init__.py    build_backends(config) -> dict[str, Backend]
    base.py        Backend Protocol, MANAGED_PREFIX = "vmgw-"
    fake.py        FakeBackend (tests, `type: fake` in config)
    ssh.py         SSHRunner: run(cmd) -> str, run_ps(script) -> str
    proxmox.py     ProxmoxBackend
    tart.py        TartBackend
    hyperv.py      HyperVBackend (+ unattend.xml template, port pool)
  api.py           FastAPI router /v1
  mcp_server.py    build_mcp(manager, config) -> MCPServer with tools
  app.py           create_app(config) -> FastAPI (mounts MCP, lifespan starts reaper + session manager)
  cli.py           typer: serve, user add/list/revoke
tests/
  conftest.py      fake config, store in tmp_path, manager with FakeBackend
  test_config.py test_store.py test_auth.py test_scheduler.py test_leases.py
  test_api.py test_mcp.py test_proxmox.py test_tart.py test_hyperv.py
  live/test_live_*.py   @pytest.mark.live, skipped unless VMGW_LIVE=1
Dockerfile, compose.yaml, README.md
scripts/pve/*.sh  (exists)
```

## Core interfaces (all tasks must match these)

```python
# models.py
class LeaseState(StrEnum): PROVISIONING="provisioning"; READY="ready"; RELEASING="releasing"; RELEASED="released"; FAILED="failed"; EXPIRED="expired"
ACTIVE_STATES = {PROVISIONING, READY, RELEASING}
@dataclass class Resources: cpu: int; memory_gb: int; disk_gb: int
@dataclass class VMRef: host: str; node: str; vm_id: str          # vm_id = PVE vmid / tart name / Hyper-V name
@dataclass class Endpoint: proto: str; host: str; port: int        # ssh | rdp | vnc
@dataclass class Credentials: username: str; password: str | None; ssh_private_key: str | None; ssh_public_key: str | None
@dataclass class Access: ip: str; endpoints: list[Endpoint]
@dataclass class NodeCapacity: host: str; node: str; free_memory_gb: float; running_vms: int; max_vms: int | None; running_macos: int = 0
@dataclass class Lease: id; owner; template_id; state; resources; created_at; expires_at; ref: VMRef|None; access: Access|None; credentials: Credentials|None; error: str|None; host: str|None

# backends/base.py
class Backend(Protocol):
    name: str; type: str
    async def capacity(self) -> list[NodeCapacity]: ...
    async def provision(self, name: str, template: TemplateConfig, source: TemplateSource, resources: Resources, creds: Credentials) -> VMRef: ...
    async def wait_ready(self, ref: VMRef, template: TemplateConfig, timeout: float) -> Access: ...
    async def destroy(self, ref: VMRef) -> None: ...          # idempotent
    async def list_managed(self) -> list[VMRef]: ...          # only names starting with MANAGED_PREFIX
```

Template config: `id, description, os (linux|windows|macos), arch (amd64|arm64), host_type, sources: [{host, node?, ref}], defaults: Resources, credentials: {mode, username?, password_env?}, ready_timeout_s=600, endpoints: [ssh|rdp|vnc]`.

---

### Task 1: Scaffold

**Files:** `pyproject.toml`, `src/vmgateway/__init__.py`, `tests/conftest.py`, `.gitignore`

- [ ] `uv init --lib --name vmgateway --python 3.12`, then `uv add fastapi uvicorn[standard] "mcp>=2.3,<3" proxmoxer requests asyncssh aiosqlite pydantic pyyaml typer` and `uv add --dev pytest pytest-asyncio httpx ruff`
- [ ] pyproject: `[project.scripts] vmgw = "vmgateway.cli:app"`; `[tool.pytest.ini_options] asyncio_mode="auto"; markers=["live: hits real hypervisors"]; addopts="-m 'not live'"`
- [ ] `uv run pytest` → "no tests ran"; commit `chore: scaffold`

### Task 2: config.py

Tests (`tests/test_config.py`): loads example YAML; `*_env` fields resolve from env; missing env var → `ConfigError` naming the variable; TTL policy defaults (min 10, default 120, max 4320 minutes); template with unknown `host_type` rejected; template source pointing at unknown host rejected.

- [ ] Write tests → fail → implement pydantic models `GatewayConfig{server, policy, hosts: list[HostConfig], templates: list[TemplateConfig]}` (HostConfig is a discriminated union on `type`: proxmox | tart | hyperv | fake) → pass → commit.

### Task 3: models.py + store.py

Tests: insert/get lease round-trips every field (incl. nested Access/Credentials as JSON); `list_leases(owner=None, states=ACTIVE_STATES)`; `update_lease` partial; users: add, get_by_hash, revoke, list.

- [ ] Tests → implement `Store(path)` with `async init()` creating tables `users(name PK, key_hash UNIQUE, is_admin, max_leases, created_at, revoked)` and `leases(id PK, owner, template_id, state, host, data JSON, created_at, expires_at)` → pass → commit.

### Task 4: auth.py + creds.py + cli user commands

Tests: `generate_key()` starts with `vmgw_` and ≥ 40 chars; `hash_key` is sha256 hex; `ApiKeyVerifier.verify_token` returns `AccessToken(client_id=name, scopes=["admin"] or ["user"])` for valid key, `None` for unknown/revoked; FastAPI dependency returns 401 JSON `{"error":"unauthorized"}`; `make_credentials` for `cloudinit`/`unattend` generates 20-char password meeting Windows complexity + ed25519 keypair; `static` reads env.

- [ ] Tests → implement → `vmgw user add <name> [--admin] [--max-leases N]` prints key once → commit.

### Task 5: backends/base.py + fake.py

FakeBackend: in-memory dict of VMs, configurable capacity, optional injected failure (`fail_on="provision"|"wait"|"destroy"`), `delay`. Used by all core tests.

- [ ] Implement with a tiny test → commit.

### Task 6: scheduler.py

Tests: picks only hosts whose type matches and that appear in template.sources; skips node where `free_memory_gb < requested` or `running_vms + reserved >= max_vms`; macOS template on tart skips host with `running_macos + reserved_macos >= 2`; picks max free memory; none → `GatewayError("no_capacity", 503)`; `reserved` = count of PROVISIONING leases per (host,node) passed in by LeaseManager.

- [ ] Tests → implement → commit.

### Task 7: leases.py (LeaseManager)

Tests with FakeBackend:
1. create → returns PROVISIONING immediately; background task → READY with access + credentials; VM named `vmgw-<lease_id>`
2. `wait(lease_id, timeout)` returns when READY/FAILED
3. provision failure → FAILED, error set, destroy called
4. TTL: below min → 400 `invalid_ttl`; above max → 400; default 120
5. renew: new expiry = now + ttl, capped at 72h from now; renew non-active → 409
6. destroy → RELEASING → RELEASED; destroy failure leaves RELEASING and reaper retries
7. reaper: expired READY lease → EXPIRED and VM destroyed
8. reconcile on start: PROVISIONING lease → FAILED + destroy; orphan `vmgw-*` VM with no active lease → destroyed; non-prefixed VM untouched
9. quota: user at max_leases → 429 `quota_exceeded`
10. permissions: non-owner non-admin get/destroy → 404
11. resources: request below template default disk → clamped to default; memory/cpu passed through

Lease id: 8 lowercase hex chars. Scheduling + insert under one `asyncio.Lock`. Clock injectable (`now: Callable[[], datetime]`) for TTL tests.

- [ ] Tests → implement → commit.

### Task 8: api.py

Routes per spec (templates, hosts, leases CRUD, renew, healthz). Lease JSON hides `ssh_private_key` unless `?include_key=true` for the owner. Error handler maps `GatewayError` → `{"error", "detail"}` + status.

- [ ] Tests via `httpx.AsyncClient(transport=ASGITransport(app))` covering auth, create→get ready, renew, delete, 404 for other user, admin `?all=true` → implement → commit.

### Task 9: mcp_server.py

`build_mcp(manager, config) -> MCPServer` with `token_verifier=ApiKeyVerifier(store)` and `auth=AuthSettings(issuer_url=..., resource_server_url=<public_url>/mcp)`; `transport_security` allowlist from `server.allowed_hosts`. Tools: `list_templates`, `list_hosts`, `create_vm(template, cpu=None, memory_gb=None, disk_gb=None, ttl_minutes=None, wait=True)`, `get_vm`, `list_vms`, `renew_vm`, `destroy_vm`. Each tool reads the caller via `get_access_token()`. Tool descriptions include template catalog guidance for sizing. Tool results are JSON with connection instructions (ssh command / RDP address).

- [ ] Tests: call the underlying tool functions with a patched `get_access_token`; plus one HTTP test that an unauthenticated POST /mcp gets 401 → implement → commit.

### Task 10: app.py + `vmgw serve`

Lifespan: `store.init()`, `manager.start()` (reconcile + reaper task), `async with mcp.session_manager.run()`. Mount MCP app at `/` with `streamable_http_path="/mcp"`.

- [ ] Smoke: config with `type: fake` host, `uv run vmgw serve`, `curl /v1/templates` with key, `claude mcp add --transport http` against it and list tools → commit.

### Task 11: ProxmoxBackend

Implementation notes: proxmoxer `ProxmoxAPI(host, user="vmgw@pve", token_name="gateway", token_value=..., verify_ssl=False)` run via `asyncio.to_thread`. Flow: `cluster/nextid` → `nodes/{node}/qemu/{tpl}/clone(newid, name, pool="vmgw", full=0|1)` → wait task (`nodes/{node}/tasks/{upid}/status`) → `config(cores, memory, [ciuser, cipassword, sshkeys=quote(pub, safe=""), ipconfig0="ip=dhcp"])` → `resize(disk="scsi0", size=f"{n}G")` if bigger → `status/start`. `wait_ready`: poll `agent/network-get-interfaces`, first non-loopback, non-169.254 IPv4; then TCP-probe ssh(22)/rdp(3389). `capacity`: `nodes` list → free mem, VM count in pool vmgw. `destroy`: stop (ignore if stopped) → wait → `DELETE qemu/{id}?purge=1&destroy-unreferenced-disks=1`; 404/“does not exist” → success. `list_managed`: `pools/vmgw` members with `vmgw-` name.

- [ ] Unit tests with a fake proxmoxer object asserting call sequence + sshkeys encoding → implement → live test (`VMGW_LIVE=1`): create ubuntu-24.04 lease, SSH `echo ok`, destroy → commit.

### Task 12: SSHRunner + TartBackend

SSHRunner: asyncssh connect (cached connection, reconnect on failure), `run(cmd, timeout)` raises `BackendError` on non-zero exit with stderr. `run_ps(script)` sends `pwsh -NoProfile -NonInteractive -EncodedCommand <base64-utf16le>`.

Tart flow (PATH=/opt/homebrew/bin): `tart clone <tpl> vmgw-<id>` → `tart set vmgw-<id> --cpu N --memory MB --disk-size GB` → `nohup tart run vmgw-<id> --no-graphics --net-bridged=<iface> >~/.vmgw/<id>.log 2>&1 &` → `wait_ready`: `tart ip vmgw-<id> --resolver arp --wait 5` loop, then TCP probe 22. `capacity`: `tart list --format json` (running count; all local VMs here are macOS — determine OS from `~/.tart/vms/<name>/config.json` `os` field), free mem from `vm_stat`/`sysctl hw.memsize`. `destroy`: `tart stop --timeout 30` (ignore), `tart delete`. Endpoints: ssh 22, vnc 5900 (macOS Screen Sharing).

- [ ] Unit tests (fake runner recording commands, canned JSON output) → implement → live test (macOS: clone, IP, SSH with static creds, delete) → commit.

### Task 13: HyperVBackend

Flow (PowerShell via run_ps): create dir `<vm_root>\vmgw-<id>`; `New-VHD -Path ..\disk.vhdx -ParentPath <template> -Differencing`; `Mount-VHD` → find Windows volume → write `Windows\Panther\unattend.xml` (skip OOBE, local admin `<username>`/`<password>`, autologon once, FirstLogonCommands: enable RDP + firewall, install/enable OpenSSH Server) → `Dismount-VHD`; `New-VM -Name vmgw-<id> -Generation 2 -VHDPath ... -SwitchName "Default Switch" -MemoryStartupBytes`; `Set-VMProcessor -Count`; `Set-VMMemory -DynamicMemoryEnabled $false`; disk resize if larger (`Resize-VHD` before boot); TPM if template requires (`Set-VMKeyProtector -NewLocalKeyProtector; Enable-VMTPM`); `Start-VM`. `wait_ready`: poll `(Get-VMNetworkAdapter -VMName).IPAddresses` IPv4, then add `netsh interface portproxy add v4tov4 listenport=<p> listenaddress=0.0.0.0 connectport=3389 connectaddress=<vm ip>` (+22), open firewall rule `vmgw-<id>`; Access.ip = VM NAT IP, endpoints = host LAN IP + allocated ports. Port pool from config (e.g. 40000–40999), allocated ports stored in VMRef/lease. `destroy`: `Stop-VM -TurnOff -Force`, `Remove-VM -Force`, delete portproxy entries + firewall rule, `Remove-Item -Recurse` the VM dir.

- [ ] Unit tests (generated PowerShell contains expected cmdlets, unattend.xml escapes password, port allocation avoids used ports) → implement → live test (create, RDP port TCP-reachable from LAN, SSH `whoami`, destroy) → commit.

### Task 14: Deploy

- [ ] `Dockerfile` (python:3.12-slim + uv, `uv sync --frozen --no-dev`, `CMD vmgw serve`), `compose.yaml` mounting `config.yaml`, `.env`, `./data`, `./keys` (read-only SSH keys).
- [ ] Create persistent gateway VM `vmgateway` on PVE (full clone of 9124 via root SSH, NOT in pool vmgw), install Docker, copy project, `docker compose up -d`.
- [ ] From this workstation: `claude mcp add --transport http vmgw http://<gw-ip>:8000/mcp --header "Authorization: Bearer <key>"`; ask Claude to create + destroy one VM per platform.

### Task 15: README + GitHub

- [ ] README: what/why, architecture diagram (text), per-platform prerequisites (PVE token script, templates script, Tart, Hyper-V OpenSSH), config reference, Claude Code setup, API reference, security notes.
- [ ] Verify no secrets/IPs in tracked files (`git grep` for token/password/192.168), create public repo `VMGateway` with `gh repo create`, push.
