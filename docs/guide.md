# VMGateway 使用與部署指南

[← 回到 README](../README.md)

- [給使用者：開始使用](#給使用者開始使用)：取得 key、安裝 MCP、工具、回傳格式、連線、自訂帳密、截圖、租約、REST API
- [給管理員：安裝與部署](#給管理員安裝與部署)：部署、準備虛擬化主機、設定檔、管理 key、維運
- [錯誤碼](#錯誤碼) · [疑難排解](#疑難排解) · [開發與測試](#開發與測試) · [安全注意事項](#安全注意事項)

## 給使用者：開始使用

### 1. 取得 API key

請 Gateway 管理員給你一把 key。管理員只要在自己的 Claude Code 裡說：

> 幫我建一把 VMGateway 的 key 給 alice 的筆電用

就會拿到 key 和下一步要貼的指令。**key 只會顯示這一次**，Gateway 只存雜湊值，遺失了只能撤銷重發。

### 2. 安裝 MCP

把 `<gateway>` 換成 Gateway 的位址（例如 `http://vmgateway.lan:8000`），`<key>` 換成你的 key。

**Claude Code**（一行指令）：

```bash
claude mcp add --transport http --scope user vmgw <gateway>/mcp --header "Authorization: Bearer <key>"
```

`--scope user` 讓你所有專案都能用。裝好後在 Claude Code 裡打 `/mcp`，應該會看到 `vmgw` 是 connected。

**VS Code**（`.vscode/mcp.json` 或使用者設定）：

```json
{
  "servers": {
    "vmgw": {
      "type": "http",
      "url": "<gateway>/mcp",
      "headers": { "Authorization": "Bearer <key>" }
    }
  }
}
```

**Cursor**（`~/.cursor/mcp.json`）：

```json
{
  "mcpServers": {
    "vmgw": {
      "url": "<gateway>/mcp",
      "headers": { "Authorization": "Bearer <key>" }
    }
  }
}
```

**Claude Desktop**（`claude_desktop_config.json`，透過 `mcp-remote` 轉接，需要 Node.js）：

```json
{
  "mcpServers": {
    "vmgw": {
      "command": "npx",
      "args": ["mcp-remote", "<gateway>/mcp", "--allow-http", "--header", "Authorization:${VMGW_AUTH}"],
      "env": { "VMGW_AUTH": "Bearer <key>" }
    }
  }
}
```

管理員用 `create_api_key` 發 key 時，以上四種設定都會一起回傳，網址和 key 已經填好。

### 3. 直接跟 agent 說

不用記工具名稱，用講的就好：

- 「開一台 Ubuntu 24.04，幫我測試這個 repo 的安裝腳本，測完把 VM 關掉。」
- 「開一台 Windows 11 ARM，截圖給我看設定 App 的畫面。」
- 「開一台 macOS，帳號 alice、密碼 `S3cure-Passw0rd`，用 4 小時。」
- 「我現在有哪些 VM？」
- 「那台 Windows 再延長 3 小時。」
- 「把我所有的 VM 都刪掉。」

Agent 會自己呼叫 `list_templates` → `create_vm` → 用回傳的連線資訊 SSH 進去工作 → `destroy_vm`。

### MCP 工具一覽

| 工具 | 用途 |
|---|---|
| `list_templates()` | 列出可用範本（作業系統、架構、平台、預設規格、連線方式） |
| `list_hosts()` | 列出主機、剩餘記憶體、各自支援哪些範本（管理員看得到更多細節） |
| `create_vm(template, cpu?, memory_gb?, disk_gb?, ttl_minutes?, username?, password?, wait=true)` | 開一台 VM，預設等到能連線才回傳 |
| `get_vm(lease_id, wait_s?)` | 查詢狀態、IP、端點、帳密、連線指令；`wait_s` 可以等到 ready |
| `list_vms(all_users?)` | 列出你目前的租約（管理員加 `all_users=true` 看全部） |
| `renew_vm(lease_id, ttl_minutes)` | 續租：從現在起再算 `ttl_minutes` 分鐘 |
| `screenshot_vm(lease_id, max_width=1600)` | 回傳 VM 螢幕畫面（PNG 圖片） |
| `destroy_vm(lease_id)` | 立刻結束租約並刪除 VM |
| `create_api_key(name, admin?, max_leases?)` | 管理員：發新 key，附上各種 client 的安裝設定 |
| `list_api_keys()` | 管理員：列出所有 key（不含 key 本身） |
| `revoke_api_key(name)` | 管理員：撤銷 key |

**規格怎麼選：** 跑 CLI 測試或截圖，2 vCPU / 4 GB 就夠；要編譯或開 IDE，給 4 vCPU / 8 GB 以上。磁碟只能比範本預設值大，不能縮小。

### 回傳內容長什麼樣子

`create_vm` 成功後會拿到類似這樣的內容：

```json
{
  "id": "3f9c2a1b",
  "owner": "alice",
  "template": "ubuntu-24.04",
  "state": "ready",
  "resources": {"cpu": 2, "memory_gb": 4, "disk_gb": 20},
  "created_at": "2026-11-17T08:00:00+00:00",
  "expires_at": "2026-11-17T10:00:00+00:00",
  "host": "pve",
  "error": null,
  "ip": "10.0.0.123",
  "endpoints": [{"proto": "ssh", "host": "10.0.0.123", "port": 22}],
  "credentials": {
    "username": "agent",
    "password": "a8Kp-2xQ...",
    "ssh_private_key": "-----BEGIN OPENSSH PRIVATE KEY-----\n..."
  },
  "connect": ["ssh -i <saved private key> agent@10.0.0.123"]
}
```

- `state` 可能是 `provisioning`、`ready`、`releasing`、`released`、`failed`、`expired`。如果回來時還是 `provisioning`（例如 Windows ARM 跑 OOBE 比較久），用 `get_vm(lease_id, wait_s=600)` 繼續等。
- `failed` 時 `error` 會寫原因，VM 已經自動清掉，不佔你的額度。
- 帳密只有在 `ready` 時才會出現；`list_vms` 不會顯示帳密。
- cloud-init 範本會多一把 `ssh_private_key`，存成檔案並 `chmod 600` 後用 `-i` 帶入。

### 連線到 VM

| 端點 | 連法 | 說明 |
|---|---|---|
| `ssh` | `ssh [-i key] [-p port] user@host` | Linux、macOS、有開 OpenSSH 的 Windows |
| `rdp` | 遠端桌面連線到 `host:port` | Windows。Hyper-V 的 VM 走 NAT，所以 port 會是 Gateway 分配的轉發埠（例如 40012） |
| `vnc` | `vnc://host:port` | macOS 螢幕共享 |

`connect` 欄位已經幫你組好指令，直接照抄即可。Windows VM 上的帳號是本機 Administrators 群組成員。

### 自訂帳號密碼

`create_vm` 的 `username` / `password` 都是選填：

- **都不填：** cloud-init / unattend 範本會產生隨機密碼；`static` 範本沿用範本內建帳號。
- **有填：** cloud-init / unattend 範本直接用你的帳密建立；`static` 範本會在開機後另外建一個管理員帳號（macOS 用 `sysadminctl`、Windows 用 PowerShell、Linux 用 `useradd`），並實際登入驗證過才算 ready。
- 只填 `username` 時，密碼會隨機產生。

規則：帳號以英文字母開頭，可含英數字、`.`、`_`、`-`；密碼 8 到 64 個可列印字元。Windows 另外要求密碼包含大寫、小寫、數字、符號其中 3 種，而且不能包含帳號名稱。不符合會回 `invalid_credentials`。

### 截圖

```
screenshot_vm(lease_id="3f9c2a1b", max_width=1600)
```

回傳 PNG 圖片，agent 可以直接「看」到畫面，用來確認安裝精靈、錯誤對話框、App 是否正常顯示等等。`max_width` 預設 1600，畫面比較大時會等比縮小；REST 版本可以設 `0` 拿原尺寸。VM 還沒 ready 時會回 `not_ready`。

### 租約期限與續租

- 預設 2 小時，最短 10 分鐘、最長 72 小時（管理員可在 `policy` 調整）。
- `renew_vm` 是「從現在起再算 N 分鐘」，不是在原本的到期時間往後加。
- 到期後 reaper 會在一分鐘內刪除 VM，裡面的資料不會保留，需要的東西請在到期前拿出來。
- 每人同時能持有的租約數量有上限（預設 5 台），超過會回 `quota_exceeded`。

### REST API

不用 MCP 的腳本可以直接打 REST。除了 `/healthz`，所有端點都要帶 `Authorization: Bearer <key>`。

```bash
GW=http://vmgateway.lan:8000
AUTH="Authorization: Bearer $VMGW_API_KEY"

# 看有哪些範本
curl -s -H "$AUTH" $GW/v1/templates | jq

# 開一台 Ubuntu，等到 ready 才回傳
curl -s -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"template": "ubuntu-24.04", "cpu": 2, "memory_gb": 4, "ttl_minutes": 60, "wait": true}' \
  $GW/v1/leases | tee lease.json | jq
ID=$(jq -r .id lease.json)

# 拿 SSH 私鑰
curl -s -H "$AUTH" "$GW/v1/leases/$ID?include_key=true" | jq -r .credentials.ssh_private_key > vm.key
chmod 600 vm.key
ssh -i vm.key agent@$(jq -r .ip lease.json)

# 截圖、續租、刪除
curl -s -H "$AUTH" -o screen.png "$GW/v1/leases/$ID/screenshot"
curl -s -H "$AUTH" -H 'Content-Type: application/json' -d '{"ttl_minutes": 180}' $GW/v1/leases/$ID/renew
curl -s -H "$AUTH" -X DELETE $GW/v1/leases/$ID
```

| 方法 | 路徑 | 說明 |
|---|---|---|
| GET | `/healthz` | 健康檢查（不需要 key） |
| GET | `/v1/templates` | 範本清單 |
| GET | `/v1/hosts` | 主機與剩餘容量 |
| POST | `/v1/leases` | 建立：`{template, cpu?, memory_gb?, disk_gb?, ttl_minutes?, username?, password?, wait?, wait_timeout_s?}`。REST 的 `wait` 預設是 `false`，會立刻回傳 `provisioning` |
| GET | `/v1/leases` | 你目前的租約（`?all=true` 管理員看全部、`?include_inactive=true` 含已結束的） |
| GET | `/v1/leases/{id}` | 詳細資料（`?include_key=true` 附 SSH 私鑰、`?wait_s=600` 等到 ready） |
| POST | `/v1/leases/{id}/renew` | 續租：`{ttl_minutes}` |
| GET | `/v1/leases/{id}/screenshot` | 螢幕 PNG（`?max_width=`，`0` 為原尺寸） |
| DELETE | `/v1/leases/{id}` | 刪除 |
| POST | `/v1/users` | 管理員：`{name, admin?, max_leases?}`，回傳 `api_key` 和 `install` 設定 |
| GET | `/v1/users` | 管理員：使用者清單 |
| DELETE | `/v1/users/{name}` | 管理員：撤銷 key |

錯誤一律是 JSON：`{"error": "no_capacity", "detail": "..."}`，代碼見[錯誤碼](#錯誤碼)。

## 給管理員：安裝與部署

### 需求

- Gateway：Python 3.12+ 和 [uv](https://docs.astral.sh/uv/)，或 Docker。一台 2 vCPU / 2 GB 的 Linux VM 就很夠用。
- Gateway 要能連到：PVE 的 API（8006）、Mac 和 Windows 主機的 SSH（22）。
- Agent 要能連到：Gateway 的 8000 埠，以及 VM 本身的 IP（PVE、Tart bridged）或 Hyper-V 主機的轉發埠範圍。
- 以上三種平台可以只用其中一種或兩種，`hosts` 和 `templates` 只寫你有的就好。

### 本機執行

```bash
git clone https://github.com/joshhu/VMGateway && cd VMGateway
uv sync
cp config.example.yaml config.yaml   # 描述你的主機和範本
cp .env.example .env                 # config.yaml 引用的密碼放這裡
uv run vmgw user add alice --admin   # 印出 alice 的 API key（只顯示一次）
uv run vmgw serve                    # REST: /v1，MCP: /mcp，預設 0.0.0.0:8000
```

`vmgw` 會自動讀取目前目錄的 `.env`；設定檔路徑可以用 `--config` 或環境變數 `VMGW_CONFIG` 指定。

### 用 Docker 部署

```bash
mkdir -p data keys
cp ~/.ssh/<mac 的 key> ~/.ssh/<windows 的 key> keys/   # Gateway 連 Tart / Hyper-V 主機用的 SSH 私鑰
# config.yaml 裡的 key_path 改成 /keys/<檔名>
docker compose up -d --build
docker compose exec vmgateway vmgw user add alice --admin
curl http://localhost:8000/healthz
```

`compose.yaml` 掛載三個東西：

| 路徑 | 內容 |
|---|---|
| `./config.yaml` → `/app/config.yaml`（唯讀） | 設定檔 |
| `./data` → `/app/data` | SQLite（使用者、租約、租約帳密），**請備份並限制權限** |
| `./keys` → `/keys`（唯讀） | 連 Tart / Hyper-V 主機的 SSH 私鑰 |

密碼從 `.env` 透過 `env_file` 注入，不會寫進映像檔。

**更新：** 拉新的程式碼後 `docker compose up -d --build`。資料庫在 `data/`，重建容器不會遺失租約；Gateway 啟動時會對帳，接手還在跑的 VM。

### 在 PVE 上建立 Gateway 專用 VM

如果想把 Gateway 本身也放在 PVE 上（作者的做法），可以用這支腳本從 Ubuntu cloud-init 範本 full clone 一台 2 vCPU / 2 GB / 16 GB 的 VM，建立 `vmgw` 使用者並放入你的 SSH 公鑰：

```bash
ssh root@pve 'VMID=150 NODE=pve1 TEMPLATE=9024 bash -s' \
  < <(cat scripts/pve/create-gateway-vm.sh; echo; cat ~/.ssh/id_ed25519.pub)
```

這台 VM **不在** `vmgw` pool 裡，名稱也不是 `vmgw-` 開頭，所以 Gateway 永遠不會把它當成租約刪掉。之後在裡面安裝 Docker，照上一節部署即可。

### 準備虛擬化主機

Gateway 只會 clone 現成的範本，以下說明每個平台需要先準備什麼。

#### Proxmox VE

```bash
# 1. 建立最小權限的使用者和 API token。租出去的 VM 放在 pool "vmgw"，範本放在 "vmgw-templates"。
ssh root@pve 'bash -s' < scripts/pve/bootstrap-token.sh
#    最後一行會印出 TOKEN_SECRET=...，填到 .env 的 PVE_TOKEN_SECRET

# 2. 建立 Ubuntu 24.04 / 22.04 cloud-init 範本（已內建 qemu-guest-agent）
#    格式：vmid:節點:版本:代號，SHARED 是放映像檔和 snippet 的共用儲存區
ssh root@pve 'SHARED=nas TEMPLATES="9024:pve1:24.04:noble 9124:pve2:24.04:noble" bash -s' \
  < scripts/pve/build-ubuntu-templates.sh

# 3. 既有的範本（例如 Windows）也要加進範本 pool，Gateway 才有權限 clone
ssh root@pve 'pveum pool modify vmgw-templates --vms 102,210'
```

注意事項：

- `local-lvm` 是節點本地儲存，linked clone 只能在範本所在的節點進行。每個節點各放一份範本，並在 `sources` 裡各列一筆，Gateway 才能把 VM 分散到不同節點。
- Windows 範本必須裝好 QEMU guest agent（Gateway 靠它拿 IP、執行開機後的設定）。
- API token 權限只限於 `vmgw` pool 裡的 VM，以及 clone `vmgw-templates` 裡的範本，碰不到其他 VM。

#### Tart（Apple Silicon Mac）

- 安裝 [Tart](https://tart.run)，在「系統設定 → 一般 → 共享」開啟「遠端登入」，並把 Gateway 的 SSH 公鑰加到 `~/.ssh/authorized_keys`。
- 建議用 bridged 網路（`bridged_interface: en0`），VM 會拿到區網 IP，agent 才連得到。設成 `null` 會改用 Tart NAT，VM 只有那台 Mac 連得到。
- IP 優先用 Tart guest agent 取得，失敗再查 ARP。
- macOS 範本如果有列 `vnc` 端點，Gateway 會在每台新 VM 裡開啟螢幕共享。
- VM 一律用 `--no-graphics --vnc-experimental` 啟動，Mac 上不會跳出視窗，`screenshot_vm` 透過 SSH tunnel 連 Tart 的 VNC 取得畫面。
- macOS 範本請事先完成設定輔助程式並建立好帳號（`static` 模式）。Apple 限制同一台 Mac 最多同時跑 2 台 macOS VM，`max_macos_vms` 預設就是 2。

#### Hyper-V（Windows 主機，例如 Windows on ARM）

- 開啟 OpenSSH Server、安裝 PowerShell 7，Gateway 用的帳號要在 *Hyper-V Administrators* 和 *Administrators* 群組，並授權 Gateway 的 SSH key。
- 範本是一個 generalize 過（開機會進 OOBE）的 Windows VHDX，建議設成唯讀，每台 VM 都是疊在上面的 differencing disk。
- 網路用 NAT（預設 `Default Switch`），Gateway 用 `netsh interface portproxy` 把主機 `public_ip` 上 `port_range` 範圍內的埠轉給 VM，並建立對應的防火牆規則。
- Gateway 的開機流程：
  1. 把 `unattend.xml` 寫進新磁碟（跳過 OOBE、建立這次租約的管理員帳號、開啟 RDP、關閉螢幕休眠、設定 `LocalAccountTokenFilterPolicy=1`）。
  2. **拔掉網路**開機。否則 OOBE 可能先裝 zero-day 更新、重開機，最後停在「誰會使用這台裝置」的互動畫面。
  3. 透過 PowerShell Direct 等到 `ImageState` 變成 `IMAGE_STATE_COMPLETE`（OOBE 結束）。
  4. 接上網路，設定防火牆、RDP、需要的話安裝 OpenSSH、擴充分割區。
- 範本有列 `ssh` 端點但 VHDX 沒預裝 OpenSSH 時，會在第一次開機從 Windows Update 下載安裝，大約多花 5 到 6 分鐘。追求速度的話，建議準備一個只開 RDP 的範本，或預裝 OpenSSH 的 VHDX。
- `windows_locale` 決定 unattend 的語系（例如 `zh-TW`），要跟 VHDX 的語言一致。

### 設定檔說明

完整範例見 [`config.example.yaml`](../config.example.yaml)。原則：**設定檔裡不放任何密碼**，所有 `*_env` 欄位填的是環境變數名稱，值放在 `.env`。

#### `server`

| 欄位 | 預設 | 說明 |
|---|---|---|
| `listen` / `port` | `0.0.0.0` / `8000` | 監聽位址 |
| `public_url` | `http://127.0.0.1:8000` | Agent 連 Gateway 用的網址，也會出現在 MCP 認證資訊和發 key 時的安裝指令裡 |
| `allowed_hosts` | `127.0.0.1:*`, `localhost:*` | MCP 接受的 Host header（防 DNS rebinding），`host:*` 代表任何埠。**用 IP 或網域連線時一定要加進來**，否則 MCP 會被拒絕 |
| `db_path` | `data/vmgw.db` | SQLite 路徑 |

#### `policy`

| 欄位 | 預設 | 說明 |
|---|---|---|
| `ttl_min_minutes` | 10 | 租約最短期限 |
| `ttl_default_minutes` | 120 | 沒指定時的期限 |
| `ttl_max_minutes` | 4320 | 最長期限（72 小時） |
| `default_max_leases` | 5 | 每人同時租約上限（個別使用者可另外設定） |
| `reaper_interval_s` | 60 | 多久檢查一次到期 |
| `reconcile_interval_s` | 600 | 多久對帳一次（清孤兒 VM） |

#### `hosts`

每台虛擬化主機一筆，`type` 決定種類。所有種類都有 `name` 和 `max_vms`（每節點上限，不填就只看記憶體）。

| `type` | 欄位 |
|---|---|
| `proxmox` | `url`、`token_id`、`token_secret_env`、`verify_ssl`（預設 false）、`pool`（預設 `vmgw`）、`storage`（full clone 的目標儲存區） |
| `tart` | `ssh: {host, user, port, key_path, known_hosts}`、`tart_bin`、`bridged_interface`（預設 `en0`）、`max_macos_vms`（預設 2） |
| `hyperv` | `ssh: {...}`、`vm_root`（VM 檔案放哪）、`switch`、`public_ip`（agent 連轉發埠用的主機 IP）、`port_range`（預設 40000–40999） |

`ssh.known_hosts` 不填代表不驗證主機金鑰（信任區網）；填了就會驗證。

#### `templates`

Agent 能選的範本清單。

| 欄位 | 說明 |
|---|---|
| `id` | 範本代號，agent 呼叫 `create_vm` 時用 |
| `description` | 說明，會顯示給 agent 看 |
| `os` / `arch` | `linux` / `windows` / `macos`，`amd64` / `arm64` |
| `host_type` | `proxmox` / `tart` / `hyperv` |
| `sources` | 可以從哪裡 clone：`{host, node, ref}`。`ref` 在 PVE 是 VMID、Tart 是 VM 名稱或 OCI 映像、Hyper-V 是 VHDX 路徑。PVE 每個節點各列一筆 |
| `defaults` | 預設規格 `{cpu, memory_gb, disk_gb}` |
| `credentials` | `{mode, username, password_env}`，見下表 |
| `endpoints` | 要提供哪些連線方式：`ssh`、`rdp`、`vnc`。Gateway 會等這些埠都開了才算 ready |
| `full_clone` | 預設 false（linked clone，快很多） |
| `windows_locale` | Hyper-V unattend 語系，預設 `en-US` |
| `ready_timeout_s` | 等多久還沒 ready 就判定失敗，預設 600 |

帳密模式：

| `mode` | 適用 | 帳密 |
|---|---|---|
| `cloudinit` | PVE Linux | 每次租約產生隨機密碼 + ed25519 金鑰（或使用者指定） |
| `unattend` | Hyper-V Windows | 每次租約產生隨機管理員密碼（或使用者指定） |
| `static` | 範本內建固定帳號 | 範本帳號，密碼從 `password_env` 讀；使用者指定帳密時開機後另外建立 |

改完設定要重啟 Gateway（`docker compose restart`）。設定有錯時 `vmgw serve` 會直接印出原因並結束。

### 管理 API key

**最簡單：直接叫 agent。** 管理員在自己的 Claude Code 裡說：

- 「幫我建一把 VMGateway 的 key 給筆電用」
- 「建一把給同事 bob 的 key，最多同時開 3 台 VM」
- 「列出所有 VMGateway 的 key」
- 「撤銷筆電那把 key」

`create_api_key` 會回傳 key 本身，以及 Claude Code、VS Code、Cursor、Claude Desktop 的安裝設定，拿到另一台電腦貼上就好。

**REST：** `POST /v1/users`、`GET /v1/users`、`DELETE /v1/users/{name}`（限管理員）。

**CLI**（在 Gateway 上，或 `docker compose exec vmgateway ...`）：

```bash
vmgw user add bob --max-leases 3   # 印出 key
vmgw user add carol --admin
vmgw user list
vmgw user revoke bob
```

規則：

- 只有管理員能發、列、撤銷 key。第一個管理員只能用 CLI 建立。
- 名稱只能用英數字、`.`、`_`、`-`（開頭須為英數字），最長 64 字元，不能重複。
- 不能撤銷最後一個有效的管理員，避免把自己鎖在外面。
- 撤銷 key 不會關掉那個人已經開著的 VM，它們會照原本的期限到期，管理員也可以手動刪除。

### 維運

- **日誌：** `docker compose logs -f vmgateway`。每次建立、ready、失敗、回收、清孤兒都會記錄。
- **健康檢查：** `GET /healthz`，Docker 映像檔已內建 HEALTHCHECK。
- **孤兒 VM：** Gateway 啟動時和每 10 分鐘會對帳一次。名稱是 `vmgw-` 開頭、但資料庫裡沒有有效租約的 VM，要連續兩次都被看到才會刪除，避免誤刪正在建立中的 VM。
- **只能跑一個 Gateway：** 同一組主機如果有兩個 Gateway（或你手動建了 `vmgw-*` 的 VM），彼此會把對方的 VM 當孤兒刪掉。跑長時間的 live test 前請先停掉正式的 Gateway。
- **備份：** 只需要備份 `data/vmgw.db`、`config.yaml` 和 `.env`。

## 錯誤碼

| 代碼 | HTTP | 意思 |
|---|---|---|
| `unauthorized` | 401 | 沒帶 key、key 錯誤或已撤銷 |
| `forbidden` | 403 | 這個操作需要管理員 |
| `not_found` | 404 | 找不到租約或使用者（或那不是你的租約） |
| `unknown_template` | 404 | 沒有這個範本 |
| `invalid_ttl` | 400 | 期限超出允許範圍 |
| `invalid_credentials` | 400 | 自訂帳密不符合規則 |
| `invalid_name` / `invalid_quota` | 400 | 建立使用者時名稱或上限（1–1000）不合法 |
| `exists` | 409 | 使用者名稱已存在 |
| `last_admin` | 409 | 不能撤銷最後一個管理員 |
| `not_active` | 409 | 租約已結束，不能續租 |
| `not_ready` | 409 | VM 還沒 ready（例如截圖時） |
| `quota_exceeded` | 429 | 你同時持有的租約已達上限 |
| `no_capacity` | 503 | 沒有主機有足夠的空間 |
| `unsupported` | 400 | 這個平台不支援該操作 |
| `screenshot_failed` | 502 | 跟虛擬化主機要畫面失敗 |

## 疑難排解

| 症狀 | 可能原因與解法 |
|---|---|
| Claude Code 的 `/mcp` 顯示 failed | 網址要以 `/mcp` 結尾；Gateway 的位址（IP 或網域加埠）要列在 `allowed_hosts`；確認 `curl <gateway>/healthz` 有回 `ok` |
| 回 `unauthorized` | key 打錯、少了 `Bearer `，或已被撤銷 |
| `no_capacity` | 主機記憶體不夠或已達 `max_vms`；macOS 已有 2 台在跑（包含不是 Gateway 建的）。用 `list_hosts` 看剩餘容量 |
| 一直停在 `provisioning` | Windows ARM 第一次開機要跑 OOBE，正常要 3 分鐘，有 SSH 的要 9 分鐘左右；用 `get_vm(wait_s=600)` 等，或拿 `screenshot_vm` 看畫面 |
| `failed`，`error` 寫 no IP / no IPv4 within ... | PVE 範本沒裝 qemu-guest-agent；Tart 沒用 bridged 網路；DHCP 沒發 IP |
| 拿到 IP 但連不上 | Agent 那台電腦要能連到 VM 的網段；Hyper-V 要連的是 `public_ip` 加轉發埠，不是 VM 內部 IP |
| 截圖是黑的或鎖定畫面 | 鎖定畫面是正常的（還沒人登入）；Gateway 已關閉螢幕休眠，黑畫面通常是 VM 還在開機 |
| PVE clone 失敗 | 範本不在 `vmgw-templates` pool；`sources` 的節點不是範本所在的節點 |
| Hyper-V 停在「誰會使用這台裝置」 | 範本 VHDX 的語系和 `windows_locale` 不一致，或 VHDX 不是 generalize 過的 |

## 開發與測試

```bash
uv sync
uv run pytest                 # 單元測試：fake backend + 錄好的虛擬化平台呼叫，不需要真的主機
uv run ruff check . && uv run ruff format --check .
```

對真的主機跑 live test（會實際建立並刪除 VM，請先停掉正式的 Gateway，見[維運](#維運)）：

```bash
VMGW_LIVE=1 VMGW_LIVE_TEMPLATES=ubuntu-24.04,macos uv run pytest -m live -s
VMGW_LIVE_USER='me:Secret-Pass1' ...   # 同時測自訂帳密
VMGW_LIVE_SHOTS=shots/ ...             # 順便存截圖
```

程式結構：

```
src/vmgateway/
  app.py          FastAPI app + MCP 掛載、背景工作（reaper / reconciler）
  api.py          REST /v1
  mcp_server.py   MCP 工具
  leases.py       租約狀態機、建立 / 續租 / 回收 / 對帳
  scheduler.py    挑主機
  auth.py         API key、使用者管理、安裝設定產生
  creds.py        帳密產生與檢查
  store.py        SQLite
  config.py       設定檔模型
  backends/       proxmox.py、tart.py、hyperv.py、vnc.py（截圖用的 RFB client）、fake.py
scripts/pve/      PVE 的 token、範本、Gateway VM 建立腳本
```

## 安全注意事項

- 同一組主機只能跑一個 Gateway，對帳程式會刪掉它沒有租約紀錄的 `vmgw-*` VM。
- 租約的帳密存在 Gateway 的 SQLite 裡，請保護好 `data/` 目錄。
- API key 只存 SHA-256 雜湊值。
- PVE token 只能管理 `vmgw` pool 裡的 VM，以及 clone `vmgw-templates` 裡的範本。
- Gateway 預設信任區網：沒設定 `known_hosts` 時不驗證 SSH 主機金鑰。
- Gateway 是 HTTP，key 在區網內是明文傳輸。要跨網路使用，請在前面加一層 HTTPS 反向代理，或走 VPN。
- Hyper-V 的 VM 會開啟 `LocalAccountTokenFilterPolicy`（允許本機帳號遠端取得完整管理員權限），每次租約都有自己的隨機密碼。

