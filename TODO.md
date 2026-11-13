# TODO（Backend）

2026-11-13 安全／效能／維運審查（#32）合併並部署後，留下來還沒做的事。依優先順序排列；每項都附上相關檔案，可以直接在本地 session 接手。

前端的待辦在 Saturn-MCP-Frontend 的 `TODO.md`。

---

## 一、需要正式環境權限（這次的 session 做不到）

- [ ] **把舊的明文工具結果補上密封。** #32 之後新寫入的結果都已密封，但部署前就存在的 `actions.result` 仍是明文，而且 `GET /runs` 仍會回傳。用正式環境的 image 跑一次：
  ```bash
  python -m app.cli.seal_results            # 先 dry run，只印筆數
  python -m app.cli.seal_results --apply
  ```
  可以仿照 `saturn-migrate` 用 Cloud Run Job 執行（同一個 image、同一組 `DATABASE_URL` 與 KMS 權限）。程式在 `app/cli/seal_results.py`。

- [ ] **確認沒有組織被誤標為 `suspended`。** 停權現在真的會擋主控台、OAuth 與 `/mcp`。
  ```sql
  SELECT id, name, status FROM organizations WHERE status = 'suspended';
  ```

- [ ] **從外部驗證線上 API。** 這次的 session 連不到 `api.saturn-core.com`，上線驗證只依據部署流程的啟動檢查。請確認：
  - `GET /healthz` 回 200，回應帶 `X-Request-Id`。
  - 對 `/oauth/register` 送 70 KB 的 body 回 413。
  - `Origin: https://evil.example` 的 `POST /oauth/login` 回 403。
  - Cloud Logging 裡每個請求一行 JSON，有 `severity`、`request_id`、`logging.googleapis.com/trace`，而且沒有 query string。

- [ ] **把 Cloud Run 的入口收到 Cloudflare 後面。** 正式環境的 `CLOUD_RUN_INGRESS` 仍未設定，`*.run.app` 可以從網際網路直接連，只靠 origin secret 擋住。步驟見 `infra/gcp/README.md` 的「Restrict ingress to Cloudflare」。完成後把 repository variable `REQUIRE_HARDENED_DEPLOY` 設為 `true`，部署工作流程就會在設定退回不安全預設值時失敗。

- [ ] **在非正式環境實際跑一次 `infra/gcp/rotate-db-password.sh`。** 重寫後的流程（先改密碼、等 Cloud SQL 完成、寫 secret、失敗時還原舊密碼）只用替身指令測過，沒有在 Google Cloud 上執行過。

- [ ] **收斂部署身分的 IAM 權限。** `infra/gcp/split-deploy-identities.sh` 給每個 repo 的 deployer 專案層級的 `roles/run.developer`，所以前端的 pipeline 也能刪除或重跑後端的服務與 `saturn-migrate` Job。改成只在各自的 Cloud Run 服務上授權。

- [ ] **可用性與災難復原。**
  - Redis 是 BASIC tier，沒有備援。Redis 掛掉時限流會一律拒絕、`/mcp` 不能用。
  - Cloud SQL 是 zonal。
  - 沒有告警、uptime check，也沒有還原演練的 runbook。

---

## 二、程式缺陷（已確認，未修）

- [ ] **`app/cli/reseal.py` 會讓進行中的核准在 KMS 金鑰輪替後失效。** `_plain_arguments` 只認得 `{sealed, encryption}`，但 gateway 存的是 `{digest, sealed, encryption}`，所以 `approvals.parameter_digest` 只會被記成「digest left as is」。`actions.arguments.digest` 則完全沒有重算。結果是輪替之後，待核准的請求被判為參數不符而失效，已核准的則回 mismatch。行為是失敗即拒絕，但會打斷使用者。另外，`SATURN_SECRET_PROJECT` 設定時存放在 Secret Manager 的金庫值不會被重新密封。修好後補一個帶 `{digest, sealed, encryption}` 的測試。

- [ ] **平台管理員在租戶裡建立成員，租戶看不到紀錄。** `admin_service.py` 寫的是 `member_created`、`org_updated` 事件，`admin.py` 寫的是 `member_updated` 事件。三者都不在 `app/features/execution/activity/controllers/activity.py` 的 `CHANGE_EVENTS` 裡，所以 `GET /activity/changes` 不會列出。再加上平台管理員能建立有 admin 角色的成員，而 admin 的第一把金鑰會自動核准，等於平台方可以讓自己收到之後的內容封裝。至少要把這些事件加進 `CHANGE_EVENTS`；要不要限制平台管理員建立 admin 是產品決定。

- [ ] **Webhook 遞送時在 HTTP 請求期間持有列鎖。** `deliver_due` 在送出期間一直持有列鎖和交易，應該先 claim、commit，再送。檔案在 `app/features/webhooks/services/webhook_service.py`。

- [ ] **上游 OAuth 的 refresh token 有競態。** `mcp_oauth.py` 的 `_refresh` 沒有加鎖，輪替後的 token 要等請求 commit 才寫入。同時兩個 refresh，或 refresh 之後交易 rollback，都可能讓連線留著一個已被撤銷的 token。

- [ ] **暫時性錯誤被當成永久錯誤。**
  - `pq_crypto.py` 把任何例外（包括 KMS 暫時不可用）都轉成 `SecretNeedsReconnect`，webhook 因此直接標成 dead。
  - `pq_crypto._keys` 的淘汰邏輯在 `to_thread` 下不是執行緒安全的。

- [ ] **小項：**
  - `mcp_endpoint.address_blocked` 沒有擋 `fec0::/10` 和 `168.63.129.16`。
  - 名單外的憑證 query 參數（例如 `x-api-key`）會留在存起來的 endpoint 裡，`serialize_mcp_server` 也會原樣回傳。

---

## 三、效能與擴展

- [ ] **限流器改用 `redis.asyncio`。** `app/core/rate_limit.py` 與 `app/features/identity/auth/services/totp.py` 仍用同步 client，Redis 變慢時每次最多卡事件迴圈 0.5 秒。
- [ ] **活動列表改成 keyset 分頁。** `GET /activity` 仍用 OFFSET，而且以 `payload->>'source'` 這個 JSON 欄位篩選。把 `source` 拆成獨立欄位，再用 `(organization_id, created_at, id)` 做 keyset。
- [ ] **資料保留期限。** `audit_logs`、`webhook_deliveries` 都沒有保留期限或分區。上游結果現在存兩份，每份最多 4 MB。
- [ ] **每個請求的固定開銷。**
  - `deps.require_access` 每次都會載入整張成員對應圖。
  - `/auth/me` 每次都會執行 `bind_workspace`。
  - 每次工具呼叫都會重新載入整個組織的政策（可以用 version 欄位當快取鍵；不能用 TTL，過期的 allow 是安全問題）。
  - 每筆稽核紀錄會為每位成員各做一次 ML-KEM 封裝。
- [ ] **上游 MCP 改用共用的 httpx client。** 每次工具呼叫都新建 client、重做 TLS 握手，還會多送一次 `initialize`。檔案在 `mcp_remote.py`。
- [ ] **同步呼叫仍在事件迴圈上執行。** KMS 的同步 gRPC 呼叫（`content_key`、`seal`、`open_secret`）、Secret Manager（`secret_vault.py`）每次呼叫都新建 client。
- [ ] **冷啟動開銷。** 啟動時 `log_agents_without_policy` 會載入所有租戶的 agent 與政策。
- [ ] **SSE 串流的時限。** Cloud Run `--timeout 300` 每 5 分鐘就會切斷 `GET /mcp` 串流，用戶端得重新 initialize。

---

## 四、遷移與 CI

- [ ] **遷移流程的規範。** 所有待跑的遷移在同一個交易裡執行，`CREATE INDEX CONCURRENTLY` 因此不能用。需要為大表訂一套 expand/contract 的做法。另外 `009` 的 downgrade 是空的。
- [ ] **CI 從沒建過映像。** `test.yml` 不會建 Docker 映像，所以 Dependabot 的 base image PR 雖然 CI 是綠的，映像本身其實沒驗證過。考慮加一個 `docker build`，不用 push。

---

## 五、Dependabot 與套件

- [ ] #30 pytest-asyncio 1.4.1.post1：2026-11-15 之後合併。
- [ ] #26 SQLAlchemy 2.1.8：2026-11-18 之後合併。
- [ ] #29 pydantic 2.14.3：2026-11-19 之後合併。
- [ ] **aiosqlite 0.22。** 升級後測試全過，但程式要再等約五分鐘才退出（#27 已關閉）。要先改 `tests/conftest.py`，讓測試用的 engine 明確關閉 aiosqlite 連線，再升級。
- [ ] **Python 3.15。** #25 已關閉，因為 `httptools` 沒有 3.15 的 wheel，而 Dockerfile 只接受 wheel。等鎖檔裡每個套件都有 3.15 的 wheel，再一起改三處：Dockerfile 的 base image、`test.yml` 的 `python-version`、`requirements.in` 裡 compile 指令的 `--python-version`。

---

## 六、低風險／設計決定

- [ ] 已設定 TOTP 的帳號，重新驗證（step-up）時只輸入密碼也能通過，而且之後能新增 passkey（`auth.py` 的 reauth）。
- [ ] 帳號列舉：
  - 註冊時，已存在的 email 會跳過 argon2，回應時間比較短。
  - `/auth/login/migration` 只會對舊雜湊的帳號回傳 `migration`。
- [ ] 任何人都能用錯誤密碼鎖住別人的帳號（每 10 分鐘 10 次），也能用掉對方的重設碼額度（每天 10 個）。
- [ ] Refresh token 會一直滑動續期 30 天，沒有絕對到期時間；重複使用的授權碼也不會撤銷已發出的 token。
- [ ] 編輯一個啟用中的政策會把它切回 draft，限制在重新啟用前不生效（`policy_service.py`）。
- [ ] `GET /org` 沒有檢查 `agents:read` 就回傳所有 agent，包含 system prompt。
- [ ] `/approvals` 和 `/activity` 沒有套用 `/runs` 那套 agent 對應過濾。
- [ ] 字串的 `eq` deny 規則區分大小寫與空白。
- [ ] 核准策略名稱、payload-key 請求、`/approvals/{id}/approve` 的原始 body 沒有長度上限（現在有全域 4 MiB 的上限）。
