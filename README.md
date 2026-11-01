# iCloud HME — 多账号聚合管理平台

基于 iCloud Hide My Email 协议，批量创建 `@icloud.com` 隐私邮箱的商用聚合平台。

- 👥 **多账号管理** — 同时管理多组 iCloud 账号，每个独立存储、独立会话
- 🔗 **账号-别名映射** — 自动提取真实 Apple ID，每个隐私邮箱标注归属账号
- ⏱ **定时调度** — 整点自动触发，多账号轮询创建，触达上限自动切下一个
- 🌐 **Web UI** — 暖色面板，仪表盘 + 账号列表 + 别名管理 + 跨账号批量创建

## 前提条件

- **iCloud+ 订阅**（Hide My Email 需要 iCloud+）
- Python 3.10+
- Windows / macOS / Linux

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 创建管理员（只在本机终端初始化，不开放网页注册）
python manage_admin.py init --username admin

# 3. 启动 Web UI（仅本地 HTTP 开发时允许非 Secure Cookie）
AUTH_COOKIE_SECURE=0 python web_ui.py --host 127.0.0.1
python web_ui.py

# 4. 打开 http://127.0.0.1:2224 并登录管理员
#    点击左下角「导入 Cookie」添加第一个账号
#    支持粘贴 Cookie Editor 的 Header String 或 JSON
```

### Windows 启动与重启

`start.sh` 是 macOS/Linux 的 shell 脚本，Windows 的 CMD/PowerShell 请使用 `start.cmd` / `restart.cmd`。仓库没有名为 `restart.sh` 的脚本。首次在 Windows 部署时，在项目目录执行：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\start.cmd
# 重启本项目服务
.\restart.cmd
```

默认监听 `http://127.0.0.1:2224`，参数可附在脚本后，例如 `.\start.cmd --port 8080`。窗口保持打开，关闭窗口会停止服务。重启脚本只停止本项目 Windows 虚拟环境中以绝对路径启动的 `web_ui.py`，不会按端口或 Python 进程名批量结束其他项目；旧的手动相对路径启动进程请先在其窗口按 Ctrl+C。重启时正在执行的任务可能中断，已确认成功记录及配置保留在 SQLite，自动任务按已保存设置恢复。macOS 的 `.venv` 不能复制到 Windows 使用，需重新创建 Windows 虚拟环境。

## 使用方式

### Web UI（推荐）

```bash
python web_ui.py                    # 启动 Web 界面
python web_ui.py --port 8080        # 指定端口
python web_ui.py --scheduler        # 启动时自动开启调度器
```

界面功能：

| 模块 | 功能 |
|------|------|
| **账号管理** | 添加/切换/删除账号，每个账号独立 Cookie + 会话 |
| **仪表盘** | 账号总数、总别名数、今日创建数，每账号一张状态卡片 |
| **别名列表** | 实时拉取所有别名，标注所属账号 + 真实邮箱 |
| **批量创建** | 勾选目标账号 → 输入数量 → 跨账号轮询创建 |
| **调度器** | 一键启停，每整点遍历所有活跃账号创建到上限 |

### 命令行调度器

```bash
# 多账号定时调度（需要先通过 Web UI 添加账号）
python scheduler.py

# 指定账号间间隔
python scheduler.py --interval 5

# 后台守护进程
python scheduler.py -d
```

### CLI 手动操作

```bash
# 列出所有别名
python icloud_hme.py list --cookies cookies.json

# 创建别名
python icloud_hme.py create -n 5 --cookies cookies.json

# 删除别名
python icloud_hme.py delete --email xxx@icloud.com --cookies cookies.json
```

## 加号地址分裂与收信验证

从侧栏打开「分裂地址验证」（`/plus`），选择一个发件 iCloud 账号、1–3 个已配置收件的源地址及 1–3 个后缀。例如 `words-here.4w@icloud.com` 可生成候选 `words-here.4w+test01@icloud.com`。后缀只是原地址的变体，不是新的 Apple 账号，也不会增加云端隐藏地址计数。

点击「配置并发送对照邮件」后，后台通过发件账号现有 Cookie 调用 iCloud 网页邮件接口，给原地址和加号地址各发送带唯一标记的测试邮件。无需为网页发信配置 SMTP 密码，但 Cookie 必须有效且账号已经开通 iCloud 邮件。会话返回 401、403 或 421 时停止，请在账号管理中重新导入 Cookie。网页发信协议参考 [IC-VeilMail](https://github.com/Redmig110/ic-veilmail/blob/main/src/hidemyemail_generator/icloud_mail.py) 的 `/wm/message` 调用；它不提供苹果对加号收信的支持保证。

加号地址复用原地址已有的收件箱和转发通道，不创建新 Apple 别名，不添加或修改 Apple 转发规则。每个变体会生成返回该变体地址的独立公开链接，以兼容现有导入器的地址校验；**链接之间共用全部底层邮件，不提供后缀之间的收件隔离**。持有任一链接的人可以看到该原地址收件箱的邮件。

在手动收信验证中，只有本次实际收到分裂地址对应的测试标记，后台才把它记为已验证、写入邮箱清单，并提供 `邮箱----收件链接` 导出。发送接口成功不代表已收信；等待结束未观察到邮件也不直接判定为“不支持”。发信请求超时保留“结果未知”，不会自动重发。停止、失败或超时保留已配置的链接供重试；后缀相同会复用已有链接。服务重启将未结束任务标记为中断，不会重发。验证任务及结果使用现有 SQLite `documents` 表保存，类型为 `plus_job`。

接口：

- `GET /api/plus-addresses`：列出可用于验证的本地原地址和发件账号，不返回 Cookie。
- `POST /api/plus-jobs`：`{"sender_id":"账号ID","bases":["原地址@icloud.com"],"tags":["test01"],"timeout":120}`，异步启动，返回 `job_id`。等待时间为 30–300 秒，同一进程同时只运行一个加号测试。
- `GET /api/plus-jobs/{job_id}`：查看每封邮件的发信和收件结果，轮询不会再次发信。
- `POST /api/plus-jobs/{job_id}/cancel`：停止后续发信和等待，已经发出的邮件不会撤回。
- `GET /api/plus-jobs/{job_id}/export`：仅导出本次验证成功的加号地址。

导入的 SQL、Cookie 和数据库含敏感信息，不应提交；`webui_with_cookies_*.sql` 已加入 Git 忽略。

## 自动分裂、定时任务与地址类型

侧栏的「自动分裂」（`/auto-plus`）复用自动注册的配置、倒计时、任务历史、日志、结果弹窗和下载状态。选择 1–20 个已配置收件的**源地址**，设置每源每轮数量、任务标签、地址间隔和固定或随机调度间隔。初次安装默认关闭，默认每 60 分钟、每源地址 10 个；开启后先等待一个配置间隔，也可以立即执行一轮。

每轮按源地址串行生成随机 4 位小写字母或数字后缀（例如 `potted_smogs05+a7k2@icloud.com`），不拼接任务标签或额外连字符；遇到已有地址会重新随机，最多尝试 64 次，无法得到新地址则记录本项失败并继续后续任务，不覆盖旧记录。生成结果校验 `@` 前部分不超过 64 字节，源地址不得以点开头、结尾或包含连续点。已有地址及失败续配地址保持原值。随后配置收件链接并写入同一「邮箱列表」。同源地址间使用现有工作流锁，暂停后完成当前地址即停止；配置失败的地址会保留到列表，后续轮次优先续配，不重复写入。自动分裂复用源地址的收件通道，无需新建 Apple 隐藏地址或转发规则，不增加 Apple 云端别名计数，也不会自动发送测试邮件。链接配置成功显示「收件已配置 · 待实测」，只有实际投递验证成功才显示「已实收」。

邮箱列表统一使用两种类型：**源地址**和**分裂地址**。分裂地址显示所属源地址，支持类型筛选；复制、CSV 和 TXT 导出与当前类型筛选一致。分裂地址的独立链接仍然与源地址共用全部底层邮件，不提供同源地址之间的收件隔离。导出的链接可以继续用于现有中转邮箱导入器。

任务开关、配置、历史、倒计时和下载状态保存在现有 SQLite，自动分裂使用 `documents.kind=auto_plus`，创建记录使用 `plus_creation_job`，与自动注册分别保存。服务启动会恢复调度；重启中断的轮次标记为中断，不会重新生成同一轮。单轮最大 200 个是本工具的批次限制，不是苹果每源地址的总量限制，后续轮次可继续生成。

接口：

- `GET /api/auto-plus/sources`：列出可分裂的源地址。
- `GET/POST /api/auto-plus`：查看或保存配置，字段为 `enabled`、`base_emails`、`count`、`interval_mode`、`interval_minutes`、`min_minutes`、`max_minutes`、`item_interval_seconds`、`label`（ASCII 任务标签，1–10 位，不拼入邮箱地址）。
- `POST /api/auto-plus/run`：按当前或提交的配置异步执行一轮，返回 `job_id`。
- `GET /api/auto-plus/tasks/{job_id}`、`GET /api/auto-plus/tasks/{job_id}/export`：查看或导出该轮已配置成功的结果。
- `GET /api/emails`：每条增加 `address_type`（`source` / `split`）、`address_type_label`、`source_email`、`shared_inbox` 和独立的 `inbox_verified` 状态。
- `GET /api/inbox-links/export?address_type=source|split|all`：按类型导出，仍可与 `account_id` 组合。

### 分裂数量的已知边界

加号后缀不是向苹果注册新的隐藏地址，因此隐藏地址创建额度不能直接当作分裂数量上限。苹果没有在已查阅的官方资料中给出每源地址加号后缀的数量承诺；[iCloud 条款](https://www.apple.com/legal/internet-services/icloud/)仍保留服务限制权。邮箱本地部分（`@` 前）有 [RFC 5321 的 64 字节长度边界](https://www.rfc-editor.org/rfc/rfc5321.html#section-4.5.3.1.1)，本工具只生成 ASCII 后缀并校验长度。

2026-10-25 的本地实测：同一源地址成功生成并发布 100 个不同分裂地址的链接；实际发送的 1 封源地址对照与 11 封不同分裂地址邮件全部实收，下一次发送被微软发件服务 HTTP 429 限流后停止。因此可以确认这批配置和已投递样本有效，不能宣称苹果的确切数量上限或“无限分裂”。测试原始记录包含真实邮箱和收件凭据，只保存在 Git 忽略的本地 `results/` 中。

## Cookie 获取

| 方式 | 说明 |
|------|------|
| Web UI 导入 | 点击左下角按钮，粘贴 Cookie Editor 的 Header String |
| Chrome 自动提取 | Windows 下 `python icloud_hme.py export-cookies` |
| 命令行 `--cookies` | 指定 JSON 文件路径 |

支持两种输入格式：
- **Header String**：`name1=value1; name2=value2; ...`
- **JSON**：`{"name1":"value1", "name2":"value2"}`

导入后自动持久化到项目目录 `icloud_hme.sqlite3`，重启无需重新粘贴。

## 管理员登录与公开 API 授权

后台页面、账号/Cookie 管理、任务、日志、邮箱及配置接口均需要管理员会话。未登录的页面跳转 `/login`，管理 API 返回 HTTP 401。没有管理员时也不会匿名开放；首次通过本机或服务器终端执行 `python manage_admin.py init --username admin`，交互输入至少 12 字符的密码。忘记密码可执行 `python manage_admin.py reset-password`。

管理员密码使用 Werkzeug scrypt 哈希；随机会话令牌与 API Key 仅保存 SHA-256 摘要。认证数据单独保存在被 Git 忽略的 `admin_auth.sqlite3`，邮箱备份导入不应覆盖此文件。备份生产环境时也应备份此认证数据库。

会话默认自登录起固定 **30 天**，访问不会自动延期，重启服务仍然有效。可设置环境变量 `ADMIN_SESSION_DAYS`（1–365）调整新会话期限。退出登录立即撤销当前会话；修改或重置密码立即撤销全部管理员会话。后台写请求还需 `X-CSRF-Token`，从 `GET /api/auth/session` 获取，内置页面自动携带。实时事件流在会话失效后停止输出。日志接口 `/api/log-stream` 每次立即返回最多 100 条 SSE 事件，页面每 5 秒重连；空队列也立即返回，避免多个页面或已断开的连接占满 Waitress 工作线程。

登录 Cookie 使用 HttpOnly、SameSite=Lax，默认要求 HTTPS（`AUTH_COOKIE_SECURE=1`）。生产通过 Caddy HTTPS 访问；本地 `start.sh` 和 Windows 启动器为回环 HTTP 设置 `AUTH_COOKIE_SECURE=0`。直接运行 Python 并使用本地 HTTP 时也需显式设置该变量，生产不得沿用此开发设置。

登录后从侧栏打开「管理账号与 API Key」（`/admin/security`），可修改密码、退出、创建和吊销密钥。完整 API Key 只显示一次；丢失后创建新密钥并吊销旧密钥。密钥有效期持续到吊销，独立于管理员会话。

| 公开接口 | 必需的 API Key 权限 | 用途 |
| --- | --- | --- |
| `GET /api/public/emails` | `emails:read` | 本地邮箱列表及已就绪的收件链接 |
| `GET /api/public/inbox-links/export` | `inbox-links:read` | `邮箱----收件链接` 导出，沿用账号/地址类型筛选 |
| `GET /api/public/mail?email=...&alias=...` | `mail:read` | 按主邮箱或指定别名读取邮件，沿用 limit/days 参数 |

公开接口必须携带 `Authorization: Bearer <API Key>` 或 `X-API-Key: <API Key>`。不接受 URL 参数中的密钥，管理员 Cookie 也不能代替公开接口的 API Key；错误或已吊销的密钥返回 401，权限不足返回 403。密钥不能调用账号管理、Cookie、任务或密钥管理接口。上述权限覆盖当前全部邮箱及其邮件，请只授予调用方实际需要的权限。

```bash
curl https://icloud.tumeiapi.com/api/public/emails \
  -H 'Authorization: Bearer <API Key>'
curl https://icloud.tumeiapi.com/api/public/inbox-links/export \
  -H 'Authorization: Bearer <API Key>'
```

原 `/api/emails`、`/api/inbox-links/export`、`/api/mail` 保留为需要管理员会话的后台接口。外部程序应改用 `/api/public/` 对应路径。独立的 `webmail.fastaiapi.cloud/m/*` 收件服务继续使用原有随机收件令牌，不受本项目管理员登录影响。`GET /healthz` 仅公开返回存活状态，不暴露账号或数据。

### 防暴力破解与调用限流

限流状态存储在认证 SQLite 中，多线程/进程共享，重启不清空。超限返回 HTTP 429，并附 `Retry-After` 秒数：

| 对象 | 默认限制 |
| --- | --- |
| 登录尝试 | 每来源 IP 每分钟最多 10 次（包含用户名变化） |
| 登录失败 | 同账号与 IP 15 分钟最多失败 8 次；同账号跨 IP 最多失败 12 次 |
| 管理页面/API | 每来源 IP 每分钟 300 次；每管理员会话每分钟 60 次写操作 |
| 修改密码 | 每来源 IP 15 分钟最多 8 次 |
| 公开 API | 每来源 IP 每分钟 120 次（包括无效 Key）；每有效 Key 每分钟 60 次 |
| 公开读取邮件 | 在上述限制内，每 Key 每分钟最多 10 次 |

API Key 和会话均采用 256 位随机令牌，不使用可猜测的账号或递增 ID。后台不会接受 API Key 代替登录会话，公开接口也不会接受 URL 参数中的 Key。

只有 `AUTH_TRUSTED_PROXIES` 中的直接代理可提供 `X-Forwarded-For`，默认仅信任回环地址。当前生产 Caddy 在 `45.78.71.86`，生产服务设置 `AUTH_TRUSTED_PROXIES=127.0.0.1,::1,45.78.71.86`。直接访问业务端口时，攻击者自行发送的转发头不能改变限流来源。更换代理地址后同步调整该配置，避免将所有用户误识别为同一个代理 IP。

## 一键创建与收件链接

使用 `./start.sh` 启动后，打开 `http://127.0.0.1:2224`：

1. 在「批量创建」中选择账号和数量（每账号 1–20 个）。
2. 点击「一键创建并配置收件」。后台自动创建 iCloud 隐藏地址、独立域名收件箱和仅匹配该别名的转发规则，并读回校验规则与收件凭据。
3. 结果文本框直接显示每行 `邮箱----收件链接`。可「复制本批文本」或「导出本批 TXT」。TXT 为 UTF-8 纯文本，每行一条，无标题、无 JSON 引号。

```text
别名1@icloud.com----https://webmail.fastaiapi.cloud/m/mbx_123456789.该邮箱的随机令牌
别名2@icloud.com----https://webmail.fastaiapi.cloud/m/mbx_987654321.另一个邮箱的随机令牌
```

当前复用账号已有的 `api.fastaiapi.cloud` 收件服务，收件域为 `fastaiapi.cloud`。`cloud_inbox/` 提供部署到 `webmail.fastaiapi.cloud/m/*` 的专属收件页：直接显示原 iCloud 别名、邮件列表及提取的验证码，每 10 秒自动刷新。每个别名分配独立域名收件箱和独立随机链接；公开页面不显示转发地址，不携带上游 JWT，也不能切换到其他邮箱。

本机 `gpt-auto-register` 的「iCloud 隐藏邮箱（中转）」已支持上述原始短链接：直接导入这里导出的 TXT 即可，程序自动读取链接下的 `/api/messages`，无需修改导出文本。同一链接可以在浏览器打开查看邮件。接收端会核对返回邮箱与导入地址一致，并按邮件 ID、时间过滤重复邮件和旧验证码。

开始前会检查 iCloud 会话和规则权限，并确认隐藏地址转发到本账号的 iCloud 邮箱。如果尚未设置且只有一个可用的 iCloud 邮箱，会自动将其设为默认转发目标（影响此账号隐藏地址的默认转发，不修改主邮箱的全局转发）。全新账号需先开通 iCloud 主邮箱；第一个隐藏地址保留后，再设置并读回确认默认转发目标，以兼容 Apple 的首次初始化要求。随后只添加 `发送至指定别名 → 转发至该别名独立域名邮箱` 的规则，不覆盖冲突规则。

部分失败会保留已创建的别名、域名收件箱和执行进度，停止本账号本轮继续创建，避免留下大量未配置邮箱。在「邮箱列表」点击「补全未完成配置」即可重试，复用原别名与收件箱。若请求超时，先读回规则再决定是否补建，避免重复规则。刷新浏览器后会继续查看同一后台任务；服务重启后任务查看状态失效，但仍可从已保存记录恢复和导出。

「邮箱列表」支持按账号筛选后复制或导出全部已配置记录。未完成配置的条目不混入最终文本。「收件已配置」表示已验证转发规则存在且收件凭据有效，不表示每个新地址都已实际收到测试邮件；Apple 提示规则生效可能需几分钟。遇到 Cookie 过期、服务验证要求或创建频率上限时会显示错误并停止，不绕过限制。

接口：

- `POST /api/create-jobs`：`{"account_ids":["账号ID"],"count_per_account":2,"label":"可选标签"}`，返回 `job_id`。补全已有记录可加 `"mode":"repair"`。
- `GET /api/create-jobs/{job_id}`：查询进度、每项结果及 `lines` / `output`，查询不会重复执行创建。
- `GET /api/create-jobs/{job_id}/export`：导出本批已配置成功的记录。
- `GET /api/inbox-links/export?account_id=账号ID`：按账号导出；省略参数导出全部已配置记录。

Cloudflare 页面只保存独立域名收件箱的读取凭据，不接收 Apple 登录 Cookie。`results/cloud_inbox_config.json` 保存本机注册链接所需的管理令牌，需完成首次 Cloudflare 部署后配置。首次部署使用 `cloud_inbox/wrangler.jsonc`，新 Worker 仅接管 `/m/*` 路由，保留已有临时邮箱站点；`schema.sql` 仅新增 `hme_public_inboxes` 表。

映射与恢复进度保存在被 Git 忽略的 `icloud_hme.sqlite3` 的 `inbox_links` 表，数据库文件权限为 600。导出内容含收件凭据，持链接者可以访问对应的独立收件箱。服务应保持仅本机访问。Web 界面、AccountManager 和调度器走完整流程；`icloud_hme.py create` 为底层仅创建别名的 CLI。

## 自动注册页面

打开 `http://127.0.0.1:2224/auto`，或点击管理面板的「自动注册」。该页面创建的是 iCloud 隐藏邮箱，并自动配置转发和专属收件链接。

账号元数据和 Cookie 分别存放在 SQLite 的 `accounts`、`account_sessions` 表，登录态按账号 ID 独立存储，并通过外键关联；旧版 JSON 和独立会话文件只导入一次，之后不再写入。每个客户端有独立的 Cookie 副本和 HTTP 会话，Apple 成功响应中的 Cookie 更新只保存回所属账号，已替换会话不会被旧客户端覆盖。调度器直接读取各账号会话，无需浏览器切号；某账号失败后仍继续处理其他账号。Apple 已失效或撤销的会话无法靠本地保存恢复，需对该账号重新登录一次。多个账号建议使用独立浏览器个人资料保持登录。

- 可选择执行账号、每账号每轮数量（1–20）、每个邮箱间隔（0–300 秒）、标签前缀。
- 每轮间隔可设固定分钟数，或最小 / 最大分钟范围；上一轮结束后选定本轮等待时间。保存配置立即生效，不重置已排定的下一轮时间；新数量用于随后启动的任务，新间隔用于下一次重新计时。默认每轮 5 个、间隔 60–90 分钟，首次开关默认关闭。
- 开启后先等待设定间隔；「立即执行一轮」异步提交任务，在开关关闭时也可只跑一轮。立即执行至少成功创建并配置 1 个邮箱才重置下一轮倒计时；全部失败时保留原计划（包括执行期间继续倒计时），自动定时轮次失败则正常安排下一轮，避免连续重试。任务与手动批量创建共用互斥队列，不会并行重复创建。
- 关闭开关会取消下一轮，当前正在处理的邮箱完成后停止，不再开始下一个。Apple 限流或配置失败时保留成功结果，结束本账号本轮，按配置等待下一轮。
- 页面显示北京时间、下次启动倒计时、每轮成功 / 失败数、最近 40 轮任务及 300 条日志，每轮提供「查看结果」弹窗、一键复制全部以及 TXT 下载；下载状态和最近下载时间会保存，刷新或重启后仍可查看。结果格式为 `邮箱----收件链接`，无成功结果时禁用查看和下载。
- 配置、开关、下次时间、任务摘要和日志保存在忽略的 `icloud_hme.sqlite3`。任务历史和下载状态持久化到 `tasks` 表，旧任务超出页面最近 40 轮后仍可按任务 ID 查询和导出。关闭网页不影响执行；重启服务恢复开关和等待时间。重启中断的轮次不会自动重复提交，已创建但未配完的邮箱可在邮箱列表补全。

API：`GET/POST /api/auto-registration` 读取或更新配置；`POST /api/auto-registration/run` 提交单轮任务；`GET /api/auto-registration/export` 导出最近一轮成功结果；`GET /api/auto-registration/tasks/{job_id}` 查询指定轮次；`GET /api/auto-registration/tasks/{job_id}/export` 下载并记录该轮下载状态（仅允许已结束且有成功结果的任务）。原 `/api/scheduler/start`、`stop` 也使用同一调度器。

## 文件结构

```
├── icloud_hme.py        # 核心库：Cookie 提取 / HME API / 账号身份提取
├── account_manager.py   # 多账号管理器：CRUD / 批量创建 / 别名索引
├── web_ui.py            # Flask Web 面板 + 内置调度器
├── scheduler.py         # 独立命令行调度器
└── requirements.txt     # pip 依赖
```

运行时生成：

```
icloud_hme.sqlite3      # 账号、独立登录态、邮箱链接、任务和邮件缓存
scheduler_state.json   # 调度器历史状态
logs/                  # 运行日志
results/               # 创建的邮箱列表
```

## 依赖

```
requests>=2.25          # HTTP
pycryptodome>=3.15     # Chrome cookie 解密 (Windows)
pywin32>=305           # Windows DPAPI (仅 Windows)
flask>=3.0             # Web UI
```

## License

MIT

## Linux 服务器部署（导入本地数据，自动任务默认关闭）

工作目录为 `/opt/fastaiapi-groups/cloud-email-register`，仓库为 `git@github.com:fastaiapi-groups/cloud-email-register.git`。在服务器上克隆后创建独立虚拟环境并安装 `requirements.txt`。不能直接上传 macOS 的 `.venv`。

`deploy/cloud-email-register.service` 提供 systemd 模板，安装前将 `User`、`Group` 改为实际运行账号，并确认该账号能读写项目数据库及 `results/`、`logs/`。默认监听服务器 `127.0.0.1:2224`，通过 SSH 隧道访问管理面板。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
# 安装前确认服务模板的账号、目录与实际环境一致
sudo cp deploy/cloud-email-register.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cloud-email-register
systemctl status cloud-email-register --no-pager
```

数据通过 SSH 单独传输，不提交 Git。运行中的本地 SQLite 使用下文在线备份方法导出一致快照；不要只复制主文件而遗漏 WAL。目标已有部署时先停止目标服务并备份目标数据库，再导入本地快照。`results/` 内的运行配置和历史导出按需同步，不能覆盖目标已有数据而不留备份。

服务模板使用 `--disable-automation`，每次进程启动均会先持久化关闭自动注册、自动分裂和 FastAI 自动同步，并取消下次执行时间，再启动调度线程；同时不启动周期账号检查。复制来的数据即使保存了开启状态，也不会自动执行。历史记录、账号与收件链接保留，手动操作仍可使用；用户显式开启的自动任务仅在当次进程中运行，重启后再次关闭。该参数不能与 `--scheduler` 同时使用。

```bash
# 不使用 systemd 时的等价启动命令
.venv/bin/python web_ui.py --host 127.0.0.1 --port 2224 --no-sync --disable-automation
```

## SQLite 存储与迁移

数据库位置固定为项目根目录 `icloud_hme.sqlite3`，使用 Python 标准库 sqlite3，无需单独安装数据库服务。写入使用事务、外键和 WAL；账号会话更新使用条件写入，旧客户端不能覆盖已经替换的新会话。数据库及其 WAL/SHM 文件已加入 Git 忽略规则。

主要表：`accounts`（账号元数据）、`account_sessions`（按账号独立的 Cookie）、`email_records`（创建记录）、`inbox_links`（完整收件映射和恢复进度）、`tasks`（每轮结果和下载状态）、`task_logs`（日志）、`documents`（调度配置、原始异步任务、缓存和服务配置）、`legacy_sources`（原始文件的逐字节归档）。

迁移入口为 `python migrate_sqlite.py`。部署迁移前应停止服务并备份；脚本按模块事务导入、记录已完成标记，并核对账号、会话、邮箱顺序、完整收件凭据、任务配置、日志和历史是否一致。重复导入不会覆盖数据库中新数据。旧 JSON/TXT 文件保留作迁移前恢复材料，修改旧文件不会更新运行数据。TXT/CSV 仍是独立导出文件。

运行中的数据库请用 SQLite 在线备份，不能只复制主文件而漏掉 WAL。例如：

```python
from storage import Storage
Storage().backup("backup/icloud_hme_snapshot.sqlite3")  # 目标文件需尚不存在
```

恢复时先停止服务，保留当前数据库及 WAL/SHM 作为回退材料，再用备份替换数据库。原始迁移备份还包含旧版代码，可完整回退到迁移前版本。

## FastAI 同步

左侧「发送账号」菜单（`/fastai-sync`）提供独立配置页面：开关、导入接口、每轮提交数量、执行间隔、倒计时、同步日志及任务历史。每条任务可打开结果弹窗查看邮箱状态及失败原因。立即执行一轮同样遵守每轮数量上限，邮箱列表的「一键同步所有未同步」不受该上限限制。

邮箱列表提供「同步 FastAI」状态列、「一键同步所有未同步」「同步配置」「查看同步结果」。同步只处理已完成收件配置的邮箱，调用 gpt-auto-register 的 `POST /api/import`，请求为 `{"kind":"icloud_relay","text":"邮箱----收件链接"}`。逐条确认导入数量后才标记「已同步」，目标接口确认的重复记录也视为成功；验证失败或超时不会误标。关闭弹窗后后台继续执行，可重新打开查看进度和逐条结果。

发送账号页面和邮箱列表的同步配置均支持「接口 Token」，后端调用导入接口时添加 `Authorization: Bearer <Token>`。可直接粘贴 Token 或带 Bearer 前缀的值；留空保存保留原 Token，勾选「清除已保存的 Token」后保存可移除授权。Token 保存在被 Git 忽略的本地 SQLite 配置中，状态接口只返回是否已配置，不返回 Token；任务启动时固定本轮凭据，不写入任务记录或日志，接口重定向不自动跟随。

接口地址、Token、自动同步开关、执行间隔、每轮提交数量、最近 300 条日志及下次时间存放在 SQLite `documents` 的 `fastai_sync_state`；每轮进度及结果存放在 `fastai_sync_job`；每个邮箱的同步状态存放在 `fastai_sync` 表。默认接口是 `http://127.0.0.1:8766/api/import`，自动同步默认关闭，默认间隔 60 分钟，每轮最多同步 50 个。「包含已同步」默认关闭，只发送未同步账号；开启后允许再次发送已同步账号。邮箱列表的同步全部未同步按钮始终只处理未同步记录。只填服务根地址会自动补上 `/api/import`。同步状态同时绑定目标接口和收件链接；换目标或链接后会重新视为未同步。配置保存不改变已经排定的下次时间，新间隔用于下一次重新计时；当前任务继续使用启动时的目标地址。

自动同步与手动同步共用一个任务队列，重复点击会查看正在执行的任务。服务重启保留成功标记，将未完成的任务标记中断，可安全重试；不会为了重试而创建新邮箱。导入只传邮箱与收件链接，不发送 Apple Cookie。此操作是导入邮箱号池，未调用目标项目的注册接口。

API：`GET/POST /api/fastai-sync` 读取状态或保存 `{api_url,enabled,interval_minutes,batch_size,include_synced}`；`POST /api/fastai-sync/run` 按配置数量执行一轮（传 `{all:true}` 同步全部，或传 `{emails:[...]}` 指定子集）；`GET /api/fastai-sync/jobs/{id}` 查看任务进度。

## iCloud 会话保活与 Cookie 更新

首页校验、Cookie 更新与后台保活均检查隐藏邮箱列表和 iCloud 邮件转发规则权限，和自动注册的预检范围一致。隐藏邮箱能列出但邮件规则返回 401/403/421/450 时，会显示邮件规则认证失败，不再只凭首页主会话有效判为正常。应在所属账号的原浏览器中确认 iCloud 邮件可打开，再更新完整 Cookie；其他账号的会话和任务独立保留。

每主账号的项目源地址上限为 750，默认轮换阈值为 495。未达到阈值时只补到阈值，达到后先完整清理旧源及其分裂地址，再按成功释放数补建；即使关闭轮换，单轮和手动创建也受 750 上限约束。邮件规则容量保护同步为 750，Apple 提前返回容量或限流错误时仍停止并保留已完成进度。

生产启动可使用 `--disable-automation --session-keepalive`：注册、分裂、同步仍默认关闭，会话维护独立运行。保活启动后立即检查，之后每轮随机间隔 10–20 分钟调用 Apple 会话校验，保存 Apple 返回的新 Cookie，不创建邮箱、不发送邮件。网络错误以随机间隔为基础退避重试，最长 60 分钟；HTTP 401/403/421/450 标记需要重新登录并暂停自动尝试，避免反复使用已失效凭据。

这是会话保活和 Cookie 轮换保存，不能保证永久有效，也不能用已失效的 Cookie 绕过 Apple 登录和双重认证。浏览器登录时选择“保持登录”，导出后不要退出 iCloud。Apple 要求重新认证时，从账号卡片点击“更新 Cookie”，导入新登录的 Header String 或 JSON 即可；服务会验证账号身份，原位更新对应账号，不需要重启。邮箱、收件链接、其他账号和业务调度配置不会被该操作覆盖。

管理员接口 `POST /api/accounts/{id}/cookies` 接受 `{"cookie_input":"name=value; ..."}`，必须携带管理员会话及 CSRF Token；公开 API Key 无权更新 Cookie。返回结果不包含凭据，身份不匹配或新会话校验失败时保留原 Cookie。账号接口提供 `session_requires_login`、`session_checked_at` 及 `last_error` 供查看保活状态。

## 自动分裂后同步 FastAI

“自动分裂”的“分裂成功后自动同步 FastAI”开关默认关闭，保存后用于下一轮（包含“立即执行一轮”）。开启后仅交接该轮成功分裂且收件配置完成的地址，失败地址不会发送。使用“发送账号”保存的接口及 Bearer Token，独立于定时同步开关；无需开启全局定时同步。

成功结果先持久化排队，与手动和定时同步共用一个工作队列，忙时等待，不受全局每轮数量限制；重复交接及已确认同步的同一收件链接不会重复发送。接口地址在入队时固定，修改接口后旧队列暂停，切回原接口才能继续；Token 在实际同步任务启动时读取，仍不进入队列或任务记录。结果沿用邮箱列表中的待同步、同步中、已同步、失败状态，并在“发送账号”记录为“分裂后同步”。失败或重启中断的发送保留状态，需手动重试，不会无限自动重发。

此开关不修改自动分裂或定时同步的开关和下次时间。已提交轮次使用提交时的设置，关闭开关不撤回已经入队的同步。配置字段为 `sync_to_fastai`（布尔值），仍须管理员会话和 CSRF 校验。

自动分裂采用逐项失败隔离：普通网络、收件服务、链接发布或单个地址错误只记失败，继续同源及其他源的任务；本轮失败地址不重复尝试，下轮仍可续配。仅 Apple 客户端明确报告会话失效，或账号已标记 `session_requires_login` 时，才跳过该账号本轮剩余的全部源地址任务，其他账号继续。收件服务的 HTTP 401/403 不会被误当成 Apple Cookie 失效。任务分别记录成功、失败及跳过数量；失败和跳过项不交给 FastAI 自动同步。

Apple 邮件规则响应包含 `INTERNAL_SERVER_ERROR` 时，页面显示服务暂时异常并按保活策略退避重试，不标记主会话需要重新登录。新 Cookie 的身份及隐藏邮箱校验通过后可原位保存；规则服务异常时接口返回 HTTP 503、`ok=false` 和 `session_saved=true`，保留业务数据及另一账号。账号状态每 60 秒读取后台缓存，手动校验或更新 Cookie 后立即通知自动注册页刷新；这不等于每分钟请求 Apple。

当前自动注册规则：每主账号最近 60 分钟最多创建 5 个源邮箱，每源生成 1 个分裂邮箱。源邮箱总数严格大于 495 时，先清理本项目可确认归属和创建时间的最早 5 个源邮箱及其关联分裂地址，再按成功释放数补建；等于 495 时正常创建。额度按持久化创建记录与尝试计算，服务重启不清零。
