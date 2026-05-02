# Changelog

本项目遵循语义化版本号（SemVer）。

## [1.2.0] - 2026-04-23

v1.2.0 B 系列可观测性 + CI 自动化。给 v1.0/1.1 的骨架+血肉补上神经系统：结构化日志 -> 指标 -> 错误追踪 -> 流水线。所有 v1.2 变更均为非破坏性，schema 未动，向前兼容。

### 新增 / 改进

- **Chunk 24 — 后端结构化 JSON 日志**：新增 `backend/app/observability/logging.py`（loguru JSON sink + stdlib `logging` 拦截 + `_SENSITIVE_KEYS` 脱敏 + Bearer token 正则脱敏），`backend/app/middlewares/request_logging.py`（`RequestLoggingMiddleware` 生成/回显 `X-Request-ID` 头并为每条请求发一行 JSON，包含 method/path/status/latency_ms/user_id/request_id），`main.py` 在启动时 `setup_logging()` 并注册中间件。smoke 新增 [12/12] 五条断言（静态 + 运行时 + 响应头）。
- **Chunk 25 — Prometheus 指标**：扩展 `backend/app/observability/metrics.py`，HTTP counter 按 Prometheus 命名规范从 `http_request_total` 改名为 `http_requests_total`；新增 `CELERY_TASK_TOTAL` / `CELERY_TASK_DURATION` 两族 celery 指标 + `DB_POOL_SIZE` / `DB_POOL_CHECKED_OUT` / `DB_POOL_OVERFLOW` 三 Gauge；`backend/app/tasks/__init__.py` 接入 celery `task_prerun/postrun/failure/retry/revoked` 信号，为每个任务实例用 `time.monotonic()` 计时；`observability/grafana/dashboards/ai-write-overview.json` 指向新命名；`docker compose up -d prometheus grafana` 起 prometheus(9091)+grafana(3001)。smoke 新增 [13/13] 九条断言覆盖源码 + `/metrics` 响应 + 容器状态 + prom 实际 scrape。
- **Chunk 26 — Sentry 接入（可选 DSN）**：`backend/app/observability/sentry_init.py` 补 `_scrub_event(event, hint)` 作为 `before_send` 和 `before_send_transaction`，复用 `logging.redact` 对 `event.request.headers/cookies/query_string/data` 与 `extra/contexts/tags` 脱敏；未设 `SENTRY_DSN` 时静默返回 `False`。前端新增 `frontend/src/sentry.client.config.ts` 浏览器端 shim：读 `NEXT_PUBLIC_SENTRY_DSN`，动态 `import('@sentry/browser')`（包未装则 no-op），`beforeSend` 剥离 URL query string 与敏感请求头，自挂 `window.error` / `unhandledrejection` 监听。smoke 新增 [14/14] 四条断言。
- **Chunk 27 — GitHub Actions CI + smoke 静态子集 + 自签 JWT fixture**：`.github/workflows/ci.yml` 扩为 ruff + mypy（非阻塞基线）+ pytest + next build + compose-validate + 新的 `smoke-static` job（`SMOKE_STATIC_ONLY=1 bash scripts/smoke_v1.sh`）；`backend/tests/fixtures/self_sign_jwt.py` 暴露 `sign_smoke_jwt(subject, ttl_seconds, secret, algorithm)` + pytest `admin_jwt` fixture + `__main__` CLI，smoke 与 CI 共用同一签名路径。`scripts/smoke_v1.sh` 引入 `SMOKE_STATIC_ONLY=1` 网关，把所有 `docker compose exec` / `curl $BASE/api/*` / `/metrics` / prom 查询块改为 SKIP 而非 FAIL，CI 无需起服务容器即可 grep 所有源码级断言。smoke 新增 [15/15] 六条断言（workflow 存在 + pull_request 触发 + 工具链 + smoke 子集调用 + fixture 就位）。

### 版本号

- backend `APP_VERSION` 从 `1.1.0` 提升至 `1.2.0`。
- `frontend/package.json` `version` 从 `1.1.0` 提升至 `1.2.0`。
- 镜像按 `GIT_TAG=v1.2.0` 重建 `backend` 与 `celery-worker`。

### smoke 矩阵

- 完整 runtime（本地 docker compose stack）：44 passed / 0 failed。
- `SMOKE_STATIC_ONLY=1`（CI 模式）：27 passed / 0 failed / 7 skipped。

## [1.1.0] - 2026-04-23

v1.1.0 A 系列骨架->血肉。在 v1.0 骨架之上填充真正可用的国际化、移动端体验、设计令牌一致性与可记忆的工作区布局。

### 新增 / 改进

- **Chunk 20 — 英文 i18n 真翻译 + 设置页语言切换器**：`lib/i18n/messages.ts` 从 stub 扩充到 39 个键位的中/英完整对照，覆盖 app / nav / locale / settings / workspace 五个命名空间；`settings/page.tsx` 与 `settings/layout.tsx` 全量改用 `useT`，并在设置页顶部提供 `LanguageSwitcher` 组件，切换后写入 `ai-write-locale` cookie 并同步 `<html lang>`。smoke 新增 [9/9] 断言语言开关、cookie 名与英文词条存在。
- **Chunk 21 — 移动端核心页落地**：`ProjectListPage` 默认单列栅格、头部 `flex-wrap`、移动内边距 `px-3 md:px-6`；`MobileWorkspace` 新增大纲抽屉（`mobile-outline-drawer` / `mobile-outline-toggle`），在 list / editor / tools / create 之间切换；`Navbar` 在窄视口下显示汉堡菜单并开合抽屉。iPhone SE（380px）视口下无横向滚动。smoke 新增 [10/10]。
- **Chunk 22 — 业务 UI 全面迁入设计令牌**：新建 `lib/graph-palette.ts` 统一关系图调色板（`SENTIMENT_*` / `NODE_*` / `GRAPH_*` / `NODE_COLOR_PALETTE`）；`relationship-graph/page.tsx`、`RelationshipGraph.tsx`、`EditorView.tsx`、`WritingGuidePanel.tsx` 中所有散落的 `#xxxxxx` 与 `rgba(...)` 字面量迁移到 `var(--text)` / `var(--color-info-500)` / `shadow-card` 等令牌或已导出的调色板常量。smoke 新增 [11/11]，通过 grep 保证白名单外无 hex/rgba 残留。
- **Chunk 23 — 工作区布局 per-project 折叠记忆 + 快捷键 + 移动端自动折叠**：`WorkspaceLayout` 新增可选 `projectId` 属性，`localStorage` 键从平键升级为 `ai-write.workspace.{sidebar,panel}-collapsed:<projectId>`，不同项目记忆各自的侧栏/面板折叠状态，缺省时回落到平键以保持向前兼容；新增 `[` / `]` 键盘快捷键切换侧栏与面板（在输入框 / contentEditable 中自动让行）；`matchMedia('(max-width: 767px)')` 在窄视口自动折叠两侧，不会在视口变宽时自动展开以尊重用户意图。smoke [8/8] 扩充三条断言。

### 版本号

- backend `APP_VERSION` 从 `1.0.0` 提升至 `1.1.0`。
- `frontend/package.json` `version` 从 `1.0.0` 提升至 `1.1.0`。
- 镜像按 `GIT_TAG=v1.1.0` 重建 `backend` 与 `celery-worker`。

## [1.0.0] - 2026-04-23

v1.0.0 big-bang 首个正式版。聚焦于用户体验、可观测性与可导出性。

### 新增 / 改进

- **Chunk 12 — 使用配额 & 管理面板**：引入用户额度、402 拦截器与 `/api/admin/usage` 管理接口，管理员可查看每用户的消耗情况；附带 alembic 迁移 `a1001200`。
- **Chunk 13 — 项目一键导出**：支持将项目导出为 EPUB / PDF / DOCX 三种格式，路径形如 `/api/export/projects/<id>.{epub,pdf,docx}`。
- **Chunk 14 — Tailwind v4 设计令牌**：在 `globals.css` 中落地 `@theme` 变量（品牌色 `--color-brand-*`、圆角 `--radius-card`、阴影与排版），为全站视觉统一奠基。
- **Chunk 15 — 国际化脚手架（中 / 英）**：新增 `lib/i18n/messages.ts` 与 `I18nProvider`，通过 `useT` / `useLocale` 切换语言，语言偏好写入 `ai-write-locale` cookie。
- **Chunk 16 — 移动端响应式基建**：`layout.tsx` 导出 `viewport`，`globals.css` 提供 `safe-area-x/top/bottom` 工具类，Navbar 支持汉堡菜单，移动端首屏可用。
- **Chunk 17 — 工作区侧栏可折叠**：`WorkspaceLayout` 左侧主侧栏与右侧面板均可折叠，通过 `usePersistedFlag` 将状态写入 `ai-write.workspace.sidebar-collapsed` 与 `ai-write.workspace.panel-collapsed`。
- **Chunk 18 — 八项核心能力冒烟脚本**：新增 `scripts/smoke_v1.sh`，在 backend 容器内自签 JWT 后跑 10 项断言，覆盖版本、鉴权、额度、导出、设计令牌、i18n、移动端、侧栏等 v1.0 所有关键能力。

### 版本号

- backend `/api/version` 新增 `version` 字段，固定为 `1.0.0`（`APP_VERSION`）。
- `frontend/package.json` `version` 从 `0.1.0` 提升至 `1.0.0`。
