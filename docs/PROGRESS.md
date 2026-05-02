# 项目当前进展（持续维护）

> 目的：让任何新窗口/新同学**只看这一份**就能知道：已经做了什么、现在做到哪、下一步做什么、怎么验收。

## 0. 一句话架构结论（TL;DR）

- **Neo4j 是设定集真相源（source of truth）**；Postgres 仅为读优化投影（materialize）。
- 任何设定集实体写入：**写 Neo4j → materialize → PG**；禁止 PG 直写以避免 drift。
- 例外：`foreshadows` 当前仍 PG 直写（**三处**：api 层 2 处 + service 层 1 处），路线决策见下方 P2。
- ⚠ **README 描述的 `/neo4j-settings/*` 与 `/admin/entities/materialize` 端点不在 main 实现**；当前 main 上唯一可用的设定集写入口是 `POST /api/projects/{pid}/outlines/{oid}/extract-settings`。详见 RUNBOOK §1。

## 1. 最近一次更新

- 日期：2026-05-01（晚 + 二轮）
- 更新人：自动执行代理（Notion + GitHub MCP + AWS MCP shell @ `/root/ai-write`）
- 关联 PR：#6（`chore/runbook-and-handoff-sync`）
- 本地状态：`main` HEAD = `17fb371` (origin/main fast-forwarded)，工作区 clean，stash 已清空

## 2. 已完成（按时间倒序）

- [x] 2026-05-01 — **P3 part 2：`scripts/verify_entity_writeback_v19.sh` 入仓**（本 PR）
  - 脚本 151 行，`bash -n` SYNTAX_OK
  - 4 段验收：legacy 410 烟测 → extract-settings 路由探测 → PG 行数 → Neo4j 对账
  - 依赖：`curl` + `psql`；可选 `cypher-shell`
- [x] 2026-05-01 — **发现：README 描述的 `/neo4j-settings/*` + `/admin/entities/materialize` 不在 main 实现**
  - `git ls-files backend/app/api/` 不含 `neo4j_settings.py` 与 `admin_entities.py`
  - 仅在 `feature/v1.0-big-bang` 历史里有过 `dc98363 feat(v1.9): add neo4j settings write API + materialize projection`
  - main 上的实际入口：`backend/app/api/outlines.py:152 POST /{outline_id}/extract-settings`，内部调用 `backend/app/tasks/entity_tasks.py:47 _materialize_entities_to_postgres`
  - 已在 RUNBOOK §1 / §5 / §4.4 修正
- [x] 2026-05-01 — **P0 本地状态清理（已实际执行）**：
  - 丢弃本地未提交 `backend/app/api/settings.py`（与 origin/main PR #3 等价 → 安全 `git checkout --`）
  - `git stash drop stash@{0}`（PR #2 子集 → 安全 drop）
  - `git pull --ff-only origin main` → HEAD `17fb371` (PR #5)，working tree clean
- [x] 2026-05-01 — **P3 compileall 已实际执行**：`python3 -m compileall -q backend/app` → `COMPILEALL_OK`
- [x] 2026-05-01 — 补齐 `docs/RUNBOOK.md`（写入入口 / Anti-pattern / 410 表 / 验收命令 / 灾难恢复 / PR 模板）（本 PR）
- [x] 2026-05-01 — `.gitignore` 补产物目录排除：`.audit-*/`、`backups/`、`backend/.coverage`、`.coverage*`、`*.sse`（本 PR）
- [x] 2026-05-01 — **P2 双重扫描完成**：
  - 远端 `search_code`：11 条 PG 直写模式 = **0 命中**
  - 本地 grep 复核：除 `models/project.py:156 class WorldRule(Base)` / `:425 class Relationship(Base)` 类定义（误匹配，正常）外，全部 0 命中
  - 但发现 `Foreshadow` PG 直写 **3 处**：`backend/app/api/foreshadows.py:111`、`:179`、`backend/app/services/foreshadow_manager.py:84` → 列入 P2 follow-up
- [x] PR #3 — 禁用 legacy `/world-rules`、`/relationships` 写接口（→ 410）；410 message 在本 PR 重写为引导 `extract-settings`（v1.10 计划后推 `/neo4j-settings/*`）
- [x] PR #2 — README + ITERATION_PLAN 文档持续维护
- [x] PR #1 — v1.9 主要收敛（outlines extract / world_rules ETL / relationships deletion sync 等）

## 3. 进行中（正在做 / 卡点）

- [ ] **P2 残留：foreshadows PG 直写迁移决策**
  - 当前状态：仓库内有 3 处 PG 直写 Foreshadow：
    - `backend/app/api/foreshadows.py:111`（POST 创建）
    - `backend/app/api/foreshadows.py:179`（DELETE）
    - `backend/app/services/foreshadow_manager.py:84`（service `create`，被章节生成 / 大纲提取链路调用）
  - 阻塞点：需要决策 Foreshadow 是否纳入 Neo4j 真相源链路
  - 下一步动作：开 follow-up PR
    - **选项 A**：（依赖 v1.10 先引入 `/neo4j-settings/*` 路由族）新增 `/neo4j-settings/foreshadows` 写入口 + materialize 投影回 `foreshadows` 表，把现有 `/foreshadows` 写接口改 410；同步把 `foreshadow_manager.create` 改走 Neo4j 写入口
    - **选项 B**：在 RUNBOOK §3 显式声明 foreshadows 不入 Neo4j 真相源链路，并加回归测试避免别处误用

- [ ] **架构 vs main 一致性：决策是否把 `neo4j_settings.py` + `admin_entities.py` 从 `feature/v1.0-big-bang` 合并到 main**
  - 当前状态：README 文档化的两个路由族不在 main
  - 影响：所有依赖这两个端点的脚本 / 文档（包括本对账脚本的部分逻辑）都要等到合并后才能完整跑
  - 候选方案：
    1. cherry-pick `dc98363` + 后续 v1.9 链 commits 到 main（带回归测试）
    2. 修订 README，把这两个路由族标注为 "v1.10 计划"，main 只承诺 `extract-settings`
  - 建议：先做 2，再独立 PR 做 1

## 4. 下一步（可执行清单，按优先级）

### P0（必须，仅本机执行）
- [x] `git fetch origin main && git pull --ff-only origin main && git status` 干净（已完成）
- [x] `git stash list` 检查 + 清理（已完成）

### P1（应该）
- [x] 补齐 `docs/RUNBOOK.md`（本 PR）
- [x] `python3 -m compileall -q backend/app` 通过（本 PR Verification）

### P2（应该）
- [ ] foreshadows 路线决策（见 §3）并落地 follow-up PR
- [x] 本地复核 `grep -RnE` 残留模式（已完成；除已知 `Foreshadow(` 3 处与 `models/project.py` 类定义外全部 0 行）

### P3（可选 / follow-up）
- [x] 把 `scripts/verify_entity_writeback_v19.sh` 提交到仓库（本 PR）
- [ ] 在有真实 PROJECT_ID 的环境跑 `PROJECT_ID=... CHAPTER_IDX=... bash scripts/verify_entity_writeback_v19.sh` 验收
- [ ] 决策是否合并 `neo4j_settings` + `admin_entities` 进 main（见 §3）

## 5. 验收 / 回归验证（复制即用）

详见 [docs/RUNBOOK.md §4](RUNBOOK.md)。

## 6. 文档更新规则（必须遵守）

每个可合并的 PR（一个"步子"）都必须同步更新文档，确保可交接：

- 影响**怎么做/怎么验收/怎么运维**：更新 `docs/RUNBOOK.md`
- 影响**架构结论/推荐入口/对外行为**：更新 `README.md`
- 影响**里程碑与计划**：更新 `ITERATION_PLAN.md`
- 跨窗口交接：更新 `docs/HANDOFF_EXECUTION.md` + `docs/HANDOFF_TODO.md`
- 当前进展快照：更新 `docs/PROGRESS.md`（本文件）

PR 描述必须包含：Context / Change / Verification / Docs updated。
