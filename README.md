# Job tracker from email

Keeps a Notion job-application tracker up to date from your inbox, automatically.

Every day it reads new emails in an Outlook folder (application confirmations, online assessments,
interview invites, rejections...), uses Claude to work out the company, role and new status, and
creates or updates the matching row in a Notion database, including a dated timeline and the job
description. Processed emails get an Outlook category so they are never handled twice; anything
uncertain is flagged for review instead of guessed.

**Stack:** Python · Microsoft Graph (Outlook) · Claude API · Notion API · GitHub Actions (daily cron)

---

每次运行：读取 Outlook「收件箱/Application」中还没打 `JobTracker` 分类的邮件 → Claude 解析 → 新建或更新 Notion 条目 → 给邮件打上 `JobTracker` 分类（在 Outlook 里能看到，删掉分类就会被重新处理）。

## 一次性设置

### 1. Python 环境（系统自带 3.9 太旧）
```sh
curl -LsSf https://astral.sh/uv/install.sh | sh     # 装 uv
uv venv --python 3.12 .venv
uv pip install -r requirements.txt
```
之后用 `.venv/bin/python tracker.py ...` 运行。

### 2. Outlook（Microsoft Graph）
1. 打开 https://portal.azure.com 用你的 Outlook 账号登录 → 搜索 **App registrations** → New registration
   - Name: `job-tracker`
   - Supported account types: 个人邮箱选 **Personal Microsoft accounts only**；学校/公司邮箱选 single tenant
   - Redirect URI 留空
2. 创建后 → **Authentication** → 最下面 *Allow public client flows* 设为 **Yes** → Save
3. **API permissions** → Add → Microsoft Graph → Delegated → 勾 **Mail.ReadWrite** → Add
4. Overview 页复制 **Application (client) ID** 填进 `.env` 的 `MS_CLIENT_ID`
5. 运行 `.venv/bin/python tracker.py --login`，按提示在浏览器输入代码并同意授权（只需一次，token 缓存在 `token_cache.json`）

### 3. Notion
1. https://www.notion.so/profile/integrations → New integration（Internal）→ 复制 secret 到 `NOTION_TOKEN`
2. **先复制一份数据库做测试**：在测试库页面右上角 `…` → **Connections** → 搜索刚建的 integration 并添加
3. 把测试库 URL 中的 32 位 ID 填到 `NOTION_DATABASE_ID`
4. 在 Status 属性中手动添加选项：`OA done`、`Interview`、`Withdrawn`、`Ghosted`（API 不能加 Status 选项）。
   其余新属性（Deadline / Job ID / Email link / Timeline / Needs review）首次运行会自动创建。

### 4. Anthropic
https://console.anthropic.com → Billing 充值（API 按量付费，与 Claude Pro 订阅分开）→ API Keys → Create key → 填到 `ANTHROPIC_API_KEY`。

## 运行
```sh
cp .env.example .env   # 填好
.venv/bin/python tracker.py --dry-run --limit 5   # 只打印，不写 Notion、不打分类
.venv/bin/python tracker.py --dry-run             # 全量预览
.venv/bin/python tracker.py                       # 真正写入
.venv/bin/python tracker.py --no-mark             # 写 Notion 但不给邮件打分类（测试库用）
```

## 每日自动运行（GitHub Actions）
运行日志会包含邮件标题和申请进度，所以定时任务放在一个**私有仓库**里，只含一个 workflow，运行时从本仓库拉代码：
1. 新建私有仓库，放入 `.github/workflows/daily.yml`，核心步骤：checkout 本仓库 → `pip install -r requirements.txt` → `python tracker.py`
2. 在私有仓库 Settings → Secrets 添加 `ANTHROPIC_API_KEY`、`MS_CLIENT_ID`、`NOTION_TOKEN`、`NOTION_DATABASE_ID`，
   以及 `MS_TOKEN_CACHE`（本地 `--login` 后 `token_cache.json` 的全部内容）
3. Microsoft 每次使用都会换新的 refresh token，workflow 用 `actions/cache` 在两次运行之间保存最新的 `token_cache.json`

不要把 token cache 放进公开仓库的 Actions 缓存：公开仓库的 fork PR 能读到它。

## 已处理标记
处理完的邮件会被加上 Outlook 分类 `JobTracker`。在 Outlook 网页版里搜索 `category:JobTracker` 可以查看；
去掉某封邮件的分类，下次运行时就会重新处理它。

## Needs review
需要人工确认的原因会追加写在 `Review note` 列里。
