# Nextcloud LDAP 設定與診斷檢查清單（occ）

**適用環境：** Nextcloud 32.x + `user_ldap` + K8s / Docker（本專案 UDrive）  
**關聯 log：** [`log-analyze.md`](./log-analyze.md) §3.3  
**最後更新：** 2026-06-12

---

## 0. 執行 occ 的前置條件

將下列變數換成實際值後，後續指令一律套用。

```bash
# Docker 範例
NC="docker exec -u www-data <容器名稱> php occ"

# Kubernetes 範例（與 tools/ 腳本一致）
NC="kubectl exec -n <namespace> <pod-name> -u www-data -- php occ"
```

- [ ] 確認以 **`www-data`** 執行（非 root）
- [ ] 確認 `user_ldap` app 已啟用：`$NC app:list | grep user_ldap`
- [ ] 記下 LDAP **config ID**（多為 `s01`）：見步驟 1

---

## 1. 快速健康檢查（每次事件發生時先做）

| # | 檢查項 | 指令 | 預期結果 |
|---|--------|------|----------|
| 1.1 | 列出 LDAP 設定 | `$NC ldap:show-config` | 至少一筆 active config（如 `s01`） |
| 1.2 | 測試連線與 bind | `$NC ldap:test-config s01` | `The configuration is valid and the connection could be established!` |
| 1.3 | 抽查 LDAP 使用者 | `$NC ldap:check-user <uid>` | `The user is still available on LDAP` |
| 1.4 | 抽查 OCS 搜尋 | 見 [`ref/api-nextcloud.http`](../api-nextcloud.http) `GET /ocs/v1.php/cloud/users?search=` | 回傳符合的使用者 JSON |
| 1.5 | 對照 log 時間 | grep `Lost connection to LDAP` in `nextcloud.log` | 與 AD 維護窗口、網路事件比對 |

**`ldap:test-config` 失敗對照：**

| 輸出訊息 | 可能原因 |
|----------|----------|
| `configuration is invalid` | `ldapHost` / `ldapBase` / filter 設定錯誤 |
| `bind failed` | `ldapAgentName` / `ldapAgentPassword` 錯誤或 AD 帳號鎖定 |
| `simple search on the base fails` | `ldapBase` 或 user filter 過嚴 |

---

## 2. 從 Nextcloud Pod 驗證網路（排除「Can't contact LDAP server」）

log 底層錯誤為 **LDAP error code `-1`**，訊息 **`Can't contact LDAP server`**（見 `apps/user_ldap/lib/LDAP.php`）。

在 **Nextcloud Pod 內**執行（非本機）：

```bash
# 替換為 ldap:show-config 輸出的 ldapHost / Port
LDAP_HOST="ldap.example.com"
LDAP_PORT="389"    # 或 636（LDAPS）

# TCP 連通
kubectl exec -n <ns> <pod> -- sh -c "nc -zv $LDAP_HOST $LDAP_PORT"

# 若有 ldapsearch（部分映像檔需自行安裝）
kubectl exec -n <ns> <pod> -- ldapsearch -x -H "ldap://${LDAP_HOST}:${LDAP_PORT}" \
  -D "<bind-dn>" -w '<password>' -b "<ldapBase>" "(objectClass=user)" dn -LLL | head
```

| # | 檢查項 | 負責 | 通過標準 |
|---|--------|------|----------|
| 2.1 | Pod → LDAP 389/636 可連 | 網路 / K8s | `nc` 成功 |
| 2.2 | DNS 解析正確 | 網路 | `getent hosts $LDAP_HOST` 回傳預期 IP |
| 2.3 | 防火牆 / NetworkPolicy 允許 | 資安 | 無 intermittent drop |
| 2.4 | AD 連線數未滿 | AD 管理員 | Perf Monitor / LDAP 服務 log 無 limit |
| 2.5 | 備援 DC 可連 | AD 管理員 | 對 `ldapBackupHost` 重複 2.1 |

---

## 3. LDAP 設定檢查（`ldap:show-config s01`）

執行：

```bash
$NC ldap:show-config s01 --output=json_pretty > /tmp/ldap-s01.json
```

對照下列 **關鍵鍵值**（`ldap:set-config` 使用的 camelCase 名稱）：

### 3.1 連線與高可用

| 設定鍵 | 建議 | 檢查要點 |
|--------|------|----------|
| `ldapHost` | 必填 | 格式 `ldap://` 或 `ldaps://`；避免過期單一 DC 主機名 |
| `ldapPort` | 389 / 636 | 與 TLS 設定一致 |
| `ldapBackupHost` | **建議設定** | 主 DC 故障時 failover |
| `ldapBackupPort` | 與 backup 一致 | |
| `ldapTLS` | 依環境 | `ldaps://` 或 StartTLS |
| `turnOffCertCheck` | 生產環境 **0** | 僅除錯時暫設 1 |
| `ldapConnectionTimeout` | 預設 **15** 秒 | 過短易在 AD 忙時誤判斷線；過長會拖慢請求 |

```bash
# 設定備援（範例）
$NC ldap:set-config s01 ldapBackupHost "ldap://dc2.example.com"
$NC ldap:set-config s01 ldapBackupPort "389"

# 調整連線逾時（範例：30 秒）
$NC ldap:set-config s01 ldapConnectionTimeout "30"
```

### 3.2 認證與搜尋

| 設定鍵 | 建議 | 檢查要點 |
|--------|------|----------|
| `ldapAgentName` / `ldapAgentPassword` | 服務帳號 | 密碼未過期、具讀取權限 |
| `ldapBase` / `ldapBaseUsers` | 正確 OU | `ldap:test-config` search 通過 |
| `ldapLoginFilter` | 可登入 | 與 UDrive 登入帳號格式一致（工號 / email） |
| `ldapAttributesForUserSearch` | **建議** `displayName;employeeID` | 讓 OCS `search=00059094` 可命中；見 [`LDAP-搜尋-00059094-設定說明.md`](../LDAP-搜尋-00059094-設定說明.md) |
| `ldapUserDisplayName` | 對應 AD 屬性 | 通常 `displayName` |

```bash
# 工號搜尋（範例）
$NC ldap:set-config s01 ldapAttributesForUserSearch "displayName;employeeID"
```

### 3.3 快取與負載

| 設定鍵 | 預設參考 | 檢查要點 |
|--------|----------|----------|
| `ldapCacheTTL` | 常見 600 秒 | 過低 → LDAP 查詢暴增；變更設定後可暫設 0 再改回 |
| `ldapPagingSize` | 依 AD | 大量使用者環境建議啟用 paging |

```bash
# 變更設定後強制刷新快取
$NC ldap:set-config s01 ldapCacheTTL "0"
# 確認生效後改回，例如 600
$NC ldap:set-config s01 ldapCacheTTL "600"
```

### 3.4 設定變更後必做

- [ ] `$NC ldap:test-config s01`
- [ ] `$NC ldap:check-user <測試帳號>`
- [ ] 管理介面 → LDAP → **「測試連線」** 或 **「清空快取並重新載入」**

---

## 4. 與 log 事件交叉比對（本環境實際紀錄）

### 4.1 `Lost connection to LDAP server` 全期間摘要

| 日期（UTC） | 筆數 | 主要觸發來源 | 典型 URL |
|-------------|------|--------------|----------|
| 2026-03-18 | 1 | 瀏覽器 Chrome | `/ocs/v2.php/apps/notifications/...` |
| 2026-04-22 | 2 | **FileSpace API** `python-httpx` | `/ocs/v1.php/cloud/users?search=00032254` |
| **2026-06-10** | **15** | 多位使用者瀏覽器 heartbeat | notifications / heartbeat / csrftoken |

底層 LDAP 訊息一致：**`error code -1` / `Can't contact LDAP server`**。

### 4.2 2026-06-10 事件窗口

| 階段 | 時間（UTC） | 關聯 |
|------|-------------|------|
| 登入風暴 | 05:36～05:37 | 13 次 `Login failed`（LDAP bind 49 與本地驗證失敗） |
| LDAP 完全不可達 | **06:55～07:12** | 15 次 `Lost connection`（間隔 20 秒～數分鐘） |
| 恢復後殘留 | 08:02 | `Bind failed: 49`（LDAP 已恢復但帳密錯誤） |

- [ ] 向 AD 團隊確認 **06:55 UTC（台灣 14:55）** 是否有維護、重啟、網路切換
- [ ] 檢查該時段 Nextcloud Pod 是否重啟、節點遷移（導致舊 LDAP socket 失效）

### 4.3 與其他 LDAP 錯誤的區分

| log 訊息 | 意義 | 處理方向 |
|----------|------|----------|
| `Lost connection to LDAP server` | TCP/LDAP 層斷線（code **-1**） | 網路、DC、備援 host、connection timeout |
| `Bind failed: 49: Invalid credentials` | LDAP 有回應，帳密錯 | 使用者密碼、帳號格式、login filter |
| `Login failed: 'xxx'` | Nextcloud 驗證失敗（可能 LDAP 或 local） | 對照上兩者 |

---

## 5. 進階診斷指令

```bash
# 搜尋 LDAP 上是否存在某帳號（需 config ID）
$NC ldap:search s01 "(sAMAccountName=00059094)"

# 檢查群組
$NC ldap:check-group <group-cn>

# 列出 LDAP 相關 occ 指令
$NC list ldap

# 提高 LDAP debug（短期，會增加 log 量）
$NC config:system:set loglevel --value=0
# 復原
$NC config:system:set loglevel --value=2
```

**資料庫（選用）：** LDAP mapping 表 `ldap_user_mapping` 可確認 Nextcloud UID 與 LDAP DN 對應是否正常（需 DB 權限）。

---

## 6. 建議修復方案（依優先順序）

### P1 — 立即（事件進行中）

- [ ] `$NC ldap:test-config s01` — 確認當下是否仍斷線
- [ ] Pod 內 `nc -zv` 測 LDAP host
- [ ] 聯繫 AD 確認 DC 狀態
- [ ] 若僅單一 DC：設定 `ldapBackupHost` 後重測

### P2 — 短期（一週內）

- [ ] 設定 **備援 LDAP host**（`ldapBackupHost` / `ldapBackupPort`）
- [ ] 檢視 `ldapConnectionTimeout`（建議 20～30 秒，視 AD 延遲調整）
- [ ] 確認 `ldapAttributesForUserSearch` 含 `employeeID`（減少 FileSpace 反覆搜尋失敗）
- [ ] FileSpace API：LDAP/OCS 失敗時 **不要** 短時間大量重試（參考 429 事件）

### P3 — 中期（架構）

- [ ] Session 後端改 **Redis**（`config.php`），降低每次 heartbeat 對 LDAP 的依賴（視 auth 鏈路而定）
- [ ] 監控告警：`Lost connection to LDAP` > 5 次 / 5 分鐘 → 通知 AD + 維運
- [ ] 建立定期探測：cron 每 5 分 `$NC ldap:test-config s01`

---

## 7. 一頁式巡檢表（可列印 / 值班用）

```
日期：__________  值班：__________  Pod：__________

[ ] ldap:show-config s01          → config ID 正確
[ ] ldap:test-config s01          → established
[ ] ldap:check-user <樣本帳號>    → available on LDAP
[ ] Pod nc -zv LDAP:389/636       → 通
[ ] ldapBackupHost 已設定         → 是 / 否
[ ] ldapAttributesForUserSearch   → 含 employeeID：是 / 否
[ ] 近 24h log 無 Lost connection → 是 / 否（若有，記時間：________）
[ ] AD 維護窗口已對照             → 是 / 否

備註：
_________________________________________________________________
```
