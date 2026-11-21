# Handoff 2026-05-12 — PR-BOOK-PROFILE-BIND 结稿 / PR-GEN-REVISE-DEDUP 并入主

**上手者启动顺序**：先读本文 → `docs/CHIXIN_VALIDATION_REPORT_2026-05-05.md` → `docs/PROGRESS.md` 末段

## TL;DR
赤心巡天 Vol1 20 章均过阈 7.0，均分 7.97，总 22.3 万字。PR-BOOK-PROFILE-BIND 全链路 Stage A-E 完工。PR-GEN-REVISE-DEDUP 已合入 main。

## 新 main HEAD
feat(scene): scene-mutex + no-redo — dedup patch + DB prompt + driver + Stage E validation report

## 代码 变动面
- `backend/app/services/scene_orchestrator.py` — plan_scenes 场景互斥硬约束 / write_scene prior 硬约束
- `backend/migrations_manual/2026-05-10_pr_gen_revise_dedup.sql` — idempotent prompt_assets upgrade
- `scripts/stage_d_dedup_rerun.sh` — 跳 expand 的串行重生 driver
- `docs/CHIXIN_VALIDATION_REPORT_2026-05-05.md` — Stage E 报告

## 验证路径
```
# 1. 验代码
sed -n '210,260p' backend/app/services/scene_orchestrator.py | grep PR-GEN-REVISE-DEDUP
# 2. 验 DB prompt
PGPASSWORD=$PW docker exec -i ai-write-postgres-1 psql -U postgres -d aiwrite -c \
  "SELECT task_type, length(system_prompt) FROM prompt_assets WHERE task_type IN ('scene_planner','scene_writer');"
# 3. 验Vol1 终态
PGPASSWORD=$PW docker exec -i ai-write-postgres-1 psql -U postgres -d aiwrite -c "
  SELECT c.chapter_idx, c.word_count, e.overall
  FROM chapters c JOIN LATERAL (SELECT overall FROM chapter_evaluations
    WHERE chapter_id=c.id ORDER BY created_at DESC LIMIT 1) e ON true
  WHERE c.volume_id='ee36b649-ff4d-45ea-a045-f50f01589b5a' ORDER BY c.chapter_idx;"
```

## Backlog
见 `CHIXIN_VALIDATION_REPORT_2026-05-05.md` 末段。首要 PR-CHAPTER-PROTECT-V1。

## 备份
- ch12 pre-canary: `chapter_versions.branch_name='pre_canary_dedup'` source=`pre_canary_dedup_v1` 8301 字
- ch2/8/10/15 pre-batch: 同上 4 行 + fs `/tmp/canary_ch{2,8,10,15}_pre.txt`
- chixin 全卷 sql: `/tmp/chixin_vol1_ch1-20_backup_v1.2_20260509_132143.sql` 22 MB
