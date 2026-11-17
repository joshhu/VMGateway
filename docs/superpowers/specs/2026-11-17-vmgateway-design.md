# VMGateway 設計規格

日期：2026-11-17

## 目標

讓 Coding Agent（Claude Code 等）透過一個 lease-based 的 Gateway，隨時取得指定 OS 的全新 VM，用完銷毀。
使用者不需要知道各虛擬化平台的帳密或 IP，只需持有自己的 API Key。

支援平台（模板皆已存在，Gateway 只負責複製與生命週期）：

- Proxmox VE Cluster（Windows / Linux）
- Tart on Mac mini（macOS / ARM Linux）
- Hyper-V（Windows on Arm）

## 架構

單一 Python 服務（FastAPI），同一個 process 同時提供：

- REST API：`/v1/*`
- MCP（Streamable HTTP）：`/mcp`

兩者共用核心：`LeaseManager`、`Scheduler`、`Backends`、`Store`。
Agent 端用 `claude mcp add --transport http vmgw http://<gw>:8000/mcp --header "Authorization: Bearer <key>"` 連線，不需安裝任何東西。

部署：PVE 上的小 Linux VM，以 Docker（compose）執行。Python 以 `uv` 管理。

## 元件

```
src/vmgateway/
  config.py      讀 config.yaml（主機、模板目錄、政策；不含密碼）+ 環境變數（密碼）
  store.py       SQLite：leases、users
  auth.py        Bearer API Key（資料庫存 sha256 hash）、admin 旗標、每人配額
  scheduler.py   依模板挑主機
  leases.py      LeaseManager + reaper + 啟動時 reconcile
  backends/
    base.py      Backend 介面
    proxmox.py   PVE REST API（API Token）
    tart.py      SSH → tart CLI
    hyperv.py    SSH → PowerShell Hyper-V cmdlets
    ssh.py       共用 SSH 執行層（asyncssh）
    fake.py      測試用
  api.py         REST routes
  mcp_server.py  MCP tools
  app.py         組裝 FastAPI + 掛載 MCP + 啟動背景工作
  cli.py         vmgw serve | vmgw user add|list|revoke
```

## 設定

`config.yaml`（gitignore；repo 內附 `config.example.yaml`）：

- `hosts[]`：`name`、`type`（proxmox | tart | hyperv）、連線資訊、容量限制
  - 密碼與 token 只寫環境變數名稱，例如 `token_secret_env: PVE_TOKEN_SECRET`
- `templates[]`：`id`、`description`、`os`、`arch`、`host_type`、`source`（PVE VMID / tart VM 名稱 / VHDX 路徑）、
  預設資源（cpu、memory_gb、disk_gb）、`credentials`
  - `credentials.mode = cloudinit`：每個 lease 隨機產生密碼，並透過 cloud-init 注入（僅 PVE）
  - `credentials.mode = unattend`：每個 lease 隨機產生管理員密碼，寫入 unattend.xml 注入新磁碟（Hyper-V）
  - `credentials.mode = static`：模板內建的固定帳密，從環境變數讀取（PVE Windows、Tart macOS）
- `policy`：TTL 與配額

`.env`（gitignore；repo 內附 `.env.example`）放所有密碼、token、SSH key 路徑。

## Lease 生命週期

狀態：`provisioning → ready → releasing → released`，另有 `failed`、`expired`。

- `create` 立即建立 lease（`provisioning`），背景執行 clone → configure → start → 等 IP。
  成功變 `ready`，帶 IP 與帳密；失敗則盡力清除已建立的部分，標成 `failed` 並記錄錯誤
- MCP `create_vm` 預設 `wait=true`：等到 `ready` 或 `failed` 才回傳，有 timeout（預設 10 分鐘，可在模板設定覆寫）
- TTL：最短 10 分鐘，預設 120 分鐘，最長 72 小時。`renew` 設定新的到期時間 = 現在 + ttl，同樣受 72 小時上限約束
- reaper 每 60 秒執行一次：銷毀到期的 lease（→ `expired`），並重試先前 destroy 失敗的 lease
- 每台 VM 一律命名為 `vmgw-<lease_id>`
- 服務啟動時 reconcile：
  - 卡在 `provisioning`／`releasing` 的 lease：清除 VM，標成 `failed`／`released`
  - 後端上有 `vmgw-` 前綴、但資料庫裡沒有對應 active lease 的孤兒 VM：自動銷毀
  - reconcile 也會在 reaper 中定期執行（每 10 分鐘）
  - 沒有 `vmgw-` 前綴的 VM 一律不碰

## 排程

1. 篩選 `type == template.host_type`，且能提供該模板的主機（PVE 節點／Tart 主機／Hyper-V 主機）
2. 檢查容量：Tart 每台主機最多 2 台執行中的 macOS VM；所有主機都可在設定中指定 `max_vms`；
   剩餘記憶體需大於要求的記憶體
3. 從符合條件的主機中，挑剩餘記憶體最多的
4. 沒有主機可用時回傳 `503 no_capacity`

PVE cluster 的節點挑選：從 API 讀取每個節點的剩餘記憶體，clone 時帶 `target`。
linked 或 full clone 由模板設定決定（linked clone 需要模板所在 storage 支援）。

## 後端介面

```python
class Backend(Protocol):
    async def capacity(self) -> list[HostCapacity]
    async def provision(self, lease: Lease, template: Template, spec: Resources, node: str) -> ProvisionResult
        # clone + 設定資源 + 注入帳密 + 開機，回傳後端 VM 參考
    async def wait_ip(self, ref: VMRef, timeout: float) -> str
    async def destroy(self, ref: VMRef) -> None      # 冪等：VM 不存在時視為成功
    async def list_managed(self) -> list[VMRef]      # 列出 vmgw- 前綴的 VM
```

| | 執行方式 | 複製 | 資源 | IP |
|---|---|---|---|---|
| PVE | REST API Token（proxmoxer） | clone 模板 VMID，cloud-init 設 user／password／sshkey | cores、memory、擴充磁碟 | qemu-guest-agent `network-get-interfaces` |
| Tart | SSH → `tart` | `tart clone`；`nohup tart run --no-graphics --net-bridged=<if>` | `tart set --cpu --memory --disk-size` | `tart ip` |
| Hyper-V | SSH → PowerShell | differencing VHDX（parent = 模板）+ 注入 unattend.xml + `New-VM -Generation 2` | `Set-VMProcessor`、`Set-VMMemory`、`Resize-VHD` | `Get-VMNetworkAdapter`，再以 portproxy 對外 |

磁碟只能放大；要求的大小比模板小時，維持模板大小。
網路：Tart 使用 bridged，讓 VM 直接拿到區網 IP。Hyper-V 主機只有 Wi-Fi，使用 NAT（Default Switch）+ 主機上的
`netsh interface portproxy`：每個 lease 從設定的 port 範圍分配 RDP／SSH 對外 port，回傳「主機 IP:port」，destroy 時移除。

## API

Header：`Authorization: Bearer <api_key>`

| Method | Path | 說明 |
|---|---|---|
| GET | `/v1/templates` | 模板目錄 |
| GET | `/v1/hosts` | 主機容量（admin 看完整資訊；一般使用者只看彙總） |
| POST | `/v1/leases` | 建立：`{template, cpu?, memory_gb?, disk_gb?, ttl_minutes?}` |
| GET | `/v1/leases` | 列出自己的 lease（admin 可加 `?all=true`） |
| GET | `/v1/leases/{id}` | 查詢 |
| POST | `/v1/leases/{id}/renew` | `{ttl_minutes}` |
| DELETE | `/v1/leases/{id}` | 銷毀 |
| GET | `/healthz` | 不需驗證 |

MCP tools：`list_templates`、`list_hosts`、`create_vm(template, cpu?, memory_gb?, disk_gb?, ttl_minutes?, wait=true)`、
`get_vm`、`list_vms`、`renew_vm`、`destroy_vm`。tool 描述會說明各模板用途與資源建議，讓 Agent 自己判斷要多少 CPU、記憶體、磁碟。

權限：一般使用者只能操作自己的 lease；admin 可以操作全部。配額：每人同時存在的 active lease 上限（預設 5）。

## 錯誤處理

- 所有 SSH／API 呼叫都有 timeout
- provisioning 失敗：盡力 destroy，lease 標成 `failed` 並保留錯誤訊息
- destroy 失敗：lease 保持 `releasing`，由 reaper 重試
- 錯誤回應格式：`{"error": "<code>", "detail": "..."}`

## 測試

- pytest + fake backend：LeaseManager、Scheduler、TTL 規則、auth／權限、REST、MCP tools
- 後端單元測試：指令組裝與輸出解析（使用錄下的範例輸出）
- `@pytest.mark.live`：讀 `.env`，對真實主機開一台 VM、取得 IP、銷毀。預設不執行

## 交付

- Dockerfile、compose.yaml
- `config.example.yaml`、`.env.example`
- README.md（安裝、設定、Claude Code 連線方式、各平台前置需求）
- Public GitHub repo `VMGateway`；真實的 `config.yaml`、`.env`、資料庫一律 gitignore

## 不在範圍內（v1）

- Web UI
- VNC／console 代理（Agent 直接用 IP 透過 SSH／RDP／Screen Sharing 連線）
- 開機後自動更換 static 帳密
- 跨平台 VM 遷移
