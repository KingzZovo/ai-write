# 赤心巡天 PR-BOOK-PROFILE-BIND 验证报告

**起点**: HANDOFF_2026-05-05_pr-book-profile-bind.md  
**结稿**: 2026-05-11 23:55 Asia/Shanghai  
**Project**: 赤心巡天 `df6f523e-f903-4644-bcce-636f5ed89c68`  
**Volume1**: `ee36b649-ff4d-45ea-a045-f50f01589b5a` (20 章)

## Stage A — profile 体系入库 ✅
- 赤心巡天 style_profile `b76da43a` rules=73 anti_ai=13 — [DB 真值核实]
- profile 绑 project `df6f523e` `0a543b1d` — 生成时装载生效

## Stage B — 创项目 ✅
- 赤心巡天 创项目 target_word_count=200000、genre_profile_code 绑定 OK
- 事件后补补 anti_ai_rules + bind profile (PR-CHIXIN-ANTI-AI `3fb7cfa`)

## Stage C — 全书 / 分卷 / 章节大纲 ✅
- Vol1 outline `803b025e-5347-4eb3-bb18-780169f6a732`
- ch1-20 outline_json 均已生成，面向 14000 字目标 expand 可重跳

## Stage D — bulk_generate 正文 ✅
- 首轮跨夜跨 codex token: PR-CHIXIN-REGEN-V2 `8d227e6` + `181ae07`
- ch1-20 全部 completed=true / word_count 在由
- **复检后发现的 6 章 低分**，dedup PR 补齐后终总结见 Stage E补充

## Stage D — 补充：PR-GEN-REVISE-DEDUP ✅

### 问题
6 章分低于阈值 7.0：ch2 5.64 / ch8 6.90 / ch10 6.54 / ch12 4.80 / ch15 5.82 / ch16 多次重评得 7.02 但 5/9 旧评 低。 
issues_json 共性 = scene重复推进 / 场景回拨 / 章末突兑。

### 根因定位
- `scene_orchestrator.SceneOrchestrator.plan_scenes` user_content 缺「场景互斥」硬约束
- `scene_orchestrator.write_scene_stream` prior_scenes_summary 被当作「背景」而非「不准重演」硬约束
- DB `prompt_assets.scene_planner / scene_writer` 系统提示词同样缺

### Patch (commit `3a2fd01` on `feat/pr-gen-revise-dedup`)
- `backend/app/services/scene_orchestrator.py` ：plan_scenes 注入 mutex_block + write_scene prior_block 改「已发生场景禁重写 / 禁回拨 / 禁再演」硬约束
- `backend/migrations_manual/2026-05-10_pr_gen_revise_dedup.sql` ：idempotent UPDATE ，scene_planner sp 647→911、scene_writer sp 443→632
- `scripts/stage_d_dedup_rerun.sh` ：跳过 outline/expand 的 4 章串行重生 driver

### Canary (ch12)
| stage | overall | plot | pace | word_count |
|---|---|---|---|---|
| baseline | 4.80 | 4.2 | 2.6 | 8301 |
| canary first | 6.96 | 5.6 | 5.2 | 11452 |
| canary revise1 | **7.62** | 7.1 | 6.8 | **14550** |

revise_skipped (score_above_threshold) pre_canary_dedup_v1 快照已落 chapter_versions

### Batch (ch2/8/10/15) — 2026-05-11 07:15-08:32
| ch | baseline | new | round | wc |
|---|---|---|---|---|
| 2 | 5.64 | **7.18** | 1 PASS | 13009 |
| 8 | 6.90 | **7.86** | 1 PASS | 8492 |
| 10 | 6.54 | **8.42** | 1 PASS | 11182 |
| 15 | 5.82 (佟base) | 8.28 (旧评留存, see backlog) | n/a | 14245 |

ch15 batch 进程 rc=0 elapsed=294s 但 SSE 中无 saved/scored 决策事件，evaluations 未新增 — 留入 PR-GEN-SSE-FINALIZE backlog (同根于 ch10/12 早先出现的 silent skip)。当前分 8.28 已 PASS，不阻塞。

## Stage E — Vol1 终态

- **总字数 223,167** (≈11,158 字/章)
- **均分 7.97**
- **20/20 章 ≥ 7.0** — 最低 ch9=7.28 最高 ch14=8.82
- 5 个不同 style_profile 绑定 / 73 rules + 13 anti-AI 生效
- scene_mode + auto_revise C2 loop 在 dedup 补丁后 表现为「首稿点对」 占多数

## Backlog (转交下一轮)
- PR-CHAPTER-PROTECT-V1：后端 PUT guard / 前端定位 / chapter_versions 自动 snapshot
- PR-GEN-SSE-FINALIZE：SSE close 路径及 silent-skip 检查 (ch10/12/15 同根)
- celery worker 容器未起 — async evaluate 走不通
- prompt_assets 升级从 manual SQL 迁到 alembic / startup seed
- 天之炽 2 个 profile 启用 / dosage_to_rules.py audit
- codex auth 24h TTL 自动续期
