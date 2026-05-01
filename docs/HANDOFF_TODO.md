# Handoff TODO（可直接打勾执行）

## P0 本地状态（仅本机执行）
- [ ] `git fetch origin main`
- [ ] `git reset --hard origin/main`
- [ ] `git status` = clean
- [ ] `git stash list` 检查是否有历史 stash
- [ ] 如需恢复，只恢复明确的 md 文件；不要恢复 `.audit-* / backups / .coverage`

## P1 文档（必须）
- [x] `docs/RUNBOOK.md` 写清：Neo4j truth + PG projection（本 PR）
- [x] 写清正确写入口：`/neo4j-settings/*`、`/outlines/{id}/extract-settings`（RUNBOOK §1）
- [x] 写清手动 materialize：`/api/admin/entities/materialize`（RUNBOOK §1.3）
- [x] 写清 legacy 410：`/world-rules`、`/relationships` 写接口（RUNBOOK §3）
- [x] 标注 foreshadows 仍 PG 直写（待 follow-up）（RUNBOOK §3 + PROGRESS §3）

## P2 防回归（清残留 PG 直写）
- [x] 远端 search_code 扫描 INSERT/UPDATE/DELETE（world_rules/relationships/locations/foreshadows）= 0 命中
- [x] 远端 search_code 扫描 `WorldRule(` / `db.add WorldRule` / `db.add Relationship` = 0 命中
- [ ] 本地复核 `grep -RnE` 同上模式（`backend/app`）= 0
- [ ] foreshadows 路线决策 + follow-up PR

## P3 验收（仅本机）
- [ ] `python -m compileall -q backend/app`
- [ ] `PROJECT_ID=... CHAPTER_IDX=... bash scripts/verify_entity_writeback_v19.sh`（如脚本就位）
- [ ] 结果回填到本 PR 描述 Verification 段或 docs/PROGRESS.md §1
