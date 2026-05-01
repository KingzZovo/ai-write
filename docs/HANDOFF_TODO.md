# Handoff TODO（可直接打勾执行）

## P0 本地状态（仅本机执行）
- [x] `git fetch origin main` ✅ 2026-05-01（AWS MCP shell）
- [x] 丢弃本地未提交的 `backend/app/api/settings.py`（`git diff HEAD origin/main` 与本地 worktree diff 完全一致 = origin/main PR #3 内容 → 安全 `git checkout --`）
- [x] `git stash drop stash@{0}`（stash 内容 = origin/main PR #2 README + ITERATION_PLAN 子集 → 安全 drop）
- [x] `git pull --ff-only origin main` → HEAD `17fb371` (PR #5)
- [x] `git status` = working tree clean，stash 列表为空

## P1 文档（必须）
- [x] `docs/RUNBOOK.md` 写清：Neo4j truth + PG projection（本 PR）
- [x] 写清正确写入口：`/neo4j-settings/*`、`/outlines/{id}/extract-settings`（RUNBOOK §1）
- [x] 写清手动 materialize：`/api/admin/entities/materialize`（RUNBOOK §1.3）
- [x] 写清 legacy 410：`/world-rules`、`/relationships` 写接口（RUNBOOK §3）
- [x] 标注 foreshadows 仍 PG 直写（待 follow-up）（RUNBOOK §3 + PROGRESS §3）
- [x] 把 service 层残留 `foreshadow_manager.py:84` 也写进 RUNBOOK / PROGRESS（不止 api 层）

## P2 防回归（清残留 PG 直写）
- [x] 远端 search_code 扫描 INSERT/UPDATE/DELETE（world_rules/relationships/locations/foreshadows）= 0 命中
- [x] 远端 search_code 扫描 `WorldRule(` / `db.add WorldRule` / `db.add Relationship` = 0 命中
- [x] 本地 `grep -RnE` 同上模式（`backend/app`）= 除 `models/project.py` 类定义（误匹配，正常）外 0 行
- [x] 本地 grep `Foreshadow(` 残留：3 处（`api/foreshadows.py:111,179`、`services/foreshadow_manager.py:84`），列入 follow-up
- [ ] foreshadows 路线决策 + follow-up PR（选项 A：纳入 Neo4j；选项 B：显式排除 + 回归测试）

## P3 验收（仅本机）
- [x] `python3 -m compileall -q backend/app` ✅ COMPILEALL_OK（2026-05-01，AWS MCP shell）
- [ ] `scripts/verify_entity_writeback_v19.sh` 当前仓库**不存在** → 需先把脚本提交到 `scripts/`，再跑 `PROJECT_ID=... CHAPTER_IDX=... bash scripts/verify_entity_writeback_v19.sh`
- [x] 结果回填到本 PR 描述 Verification 段（详见 PR 描述）
