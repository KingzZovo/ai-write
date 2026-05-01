# 项目当前进展（持续维护）

> 目的：让任何新窗口/新同学**只看这一份**就能知道：已经做了什么、现在做到哪、下一步做什么、怎么验收。

## 0. 一句话架构结论（TL;DR）

- **Neo4j 是设定集真相源（source of truth）**；Postgres 仅为读优化投影（materialize）。
- 任何设定集实体写入：**写 Neo4j → materialize → PG**；禁止 PG 直写以避免 drift。

## 1. 最近一次更新

- 日期：YYYY-MM-DD
- 更新人：@<name>
- 关联 PR：#<n>（链接）

## 2. 已完成（按时间倒序）

- [ ] YYYY-MM-DD — <一句话成果>（PR #<n>）
- [ ] YYYY-MM-DD — <一句话成果>（PR #<n>）

## 3. 进行中（正在做 / 卡点）

- [ ] <正在推进的任务>（owner / 分支 / PR 链接）
  - 当前状态：
  - 阻塞点：
  - 下一步动作：

## 4. 下一步（可执行清单，按优先级）

### P0（必须）
- [ ] <任务>

### P1（应该）
- [ ] <任务>

### P2（可选）
- [ ] <任务>

## 5. 验收 / 回归验证（复制即用）

### 5.1 本地状态必须干净

```bash
cd /root/ai-write

git fetch origin main
git reset --hard origin/main

git status
```

### 5.2 Python 基础校验

```bash
cd /root/ai-write
python -m compileall -q backend/app
```

### 5.3 设定集一致性对账（如脚本存在）

```bash
cd /root/ai-write
PROJECT_ID=<your_project_id> CHAPTER_IDX=0 bash scripts/verify_entity_writeback_v19.sh
```

预期：脚本输出 OK；Neo4j 与 PG 投影一致。

## 6. 文档更新规则（必须遵守）

每个可合并的 PR（一个“步子”）都必须同步更新文档，确保可交接：

- 影响**怎么做/怎么验收/怎么运维**：更新 `docs/RUNBOOK.md`
- 影响**架构结论/推荐入口/对外行为**：更新 `README.md`
- 影响**里程碑与计划**：更新 `ITERATION_PLAN.md`
- 跨窗口交接：更新 `docs/HANDOFF_EXECUTION.md` + `docs/HANDOFF_TODO.md`
- 当前进展快照：更新 `docs/PROGRESS.md`（本文件）

PR 描述必须包含：Context / Change / Verification / Docs updated。
