# Handoff TODO（可直接打勾执行）

## P0 本地状态
- [ ] `git fetch origin main`
- [ ] `git reset --hard origin/main`
- [ ] `git status` = clean
- [ ] `git stash list` 检查是否有历史 stash
- [ ] 如需恢复，只恢复明确的 md 文件；不要恢复 `.audit-* / backups / .coverage`

## P1 文档（必须）
- [ ] `docs/RUNBOOK.md` 写清：Neo4j truth + PG projection
- [ ] 写清正确写入口：`/neo4j-settings/*`、`/outlines/{id}/extract-settings`
- [ ] 写清手动 materialize：`/api/admin/entities/materialize`
- [ ] 写清 legacy 410：`/world-rules`、`/relationships` 写接口

## P2 防回归（清残留 PG 直写）
- [ ] grep 扫 INSERT/UPDATE/DELETE（world_rules/relationships/locations/foreshadows）
- [ ] grep 扫 `WorldRule(` / `Relationship(` / `db.add(...WorldRule...)`
- [ ] 对发现点：迁移到 Neo4j 写入 + materialize 或禁用并写文档

## P3 验收
- [ ] `python -m compileall -q backend/app`
- [ ] `PROJECT_ID=... CHAPTER_IDX=... bash scripts/verify_entity_writeback_v19.sh`
- [ ] 结果记录到 PR 描述中（或 RUNBOOK 的验收段落）
