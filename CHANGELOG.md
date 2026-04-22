# Changelog

本项目遵循语义化版本号（SemVer）。

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
