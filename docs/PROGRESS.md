# 项目当前进展（持续维护）

> 目的：让任何新窗口/新同学**只看这一份**就能知道：已经做了什么、现在做到哪、下一步做什么、怎么验收。

## 0. 一句话架构结论（TL;DR）

- **Neo4j 是设定集真相源（source of truth）**；Postgres 仅为读优化投影（materialize）。
- 任何设定集实体写入：**写 Neo4j → materialize → PG**；禁止 PG 直写以避免 drift。
- ✅ **`foreshadows` 已纳入 Neo4j 真相源链路**（F1=A 落地，PR #18 合入），foreshadows API 与 `foreshadow_manager.create` 改走 `POST /api/projects/{pid}/neo4j-settings/foreshadows`。
- ✅ **`/neo4j-settings/*` 与 `/admin/entities/materialize` 端点已在 main 实现**（PR #18 合入）。
- 当前可用入口：`POST /api/projects/{pid}/outlines/{oid}/extract-settings`（提取链路）+ `POST /api/projects/{pid}/neo4j-settings/{characters|world_rules|relationships|locations|character_states|organizations|foreshadows}` + `POST /api/admin/entities/materialize`。详见 RUNBOOK §1。

## 1. 最近一次更新

- 日期：2026-05-02（晚）
- 更新人：自动执行代理（GitHub MCP + AWS MCP shell @ `/root/ai-write`）
- 关联 PR：#8 - #18（v1.0 → v1.9 共 11 个 release PR 全部合入）
- 本地状态：`main` HEAD = `0a4f9a1`（PR #18 合并 commit），working tree clean
- 远端状态：`origin/main` = `0a4f9a1`；`origin/feature/v1.0-big-bang` 与 main 内容一致（已无独立价值，可关闭）

## 2. 已完成（按时间倒序）

### 2026-05-02 — `feature/v1.0-big-bang` 237 commit 全量回收（11 个 release PR）

按版本号干净拆分，每个 PR 独立 cherry-pick + 解决冲突 + compileall + push + open + merge：

| PR | branch | range | commits | 关键内容 |
|---|---|---|---:|---|
| #8 | `release/v1.0.0` | `036d9a0..99170ed` | 20 | Docker / Sentry / Prometheus / GH Actions CI / BVSR / LangGraph / usage_quotas / EPUB-PDF-DOCX / i18n / mobile |
| #9 | `release/v1.1.0` | `99170ed..e35cd90` | 5 | en i18n / mobile landing / design tokens / sidebar memory |
| #10 | `release/v1.2.0` | `e35cd90..37e5379` | 5 | JSON logging / X-Request-ID / Prometheus 扩展 / Sentry redaction / CI smoke |
| #11 | `release/v1.3.0` | `37e5379..f1f4730` | 6 | target_word_count + budget allocator + cascade |
| #12 | `release/v1.4.0` | `f1f4730..cdc6d2b` | 20 | LLM tier routing |
| #13 | `release/v1.4.1` | `cdc6d2b..2aedd2a` | 7 | probe surface + max_tokens + staged outline |
| #14 | `release/v1.5.0` | `2aedd2a..581f957` | 61 | chunker fix + tier-aware fallback + scene mode + cascade tasks |
| #15 | `release/v1.6.0` | `581f957..1d8d53b` | 5 | prompt cache + scene_mode metrics |
| #16 | `release/v1.7.x` | `1d8d53b..1b455b9` | 19 | cascade panel + time_llm_call + outline injection + post-summarizer + anti-AI prompts |
| #17 | `release/v1.8.x` | `1b455b9..7209960` | 8 | dosage anti-AI + Bug L 自动保存 |
| #18 | `release/v1.9.0` | `7209960..73e7897` | 81 | **Entity writeback Neo4j↔PG + F1=A foreshadows-via-Neo4j** |

冲突解决记录：`.gitignore`（PR #8）+ `README.md`（PR #12）+ `docs/RUNBOOK.md`（PR #16/#18）+ `outline_to_facts.py`（PR #16）+ `entity_tasks.py`（PR #14）。PR #18 用 `cherry-pick -X theirs` 自动取 big-bang 版本。

### 2026-05-02 — F1=A foreshadows-via-Neo4j 落地（PR #18 包含）

关键 commit：
- `02a5f19` neo4j foreshadows write + materialize to postgres
- `cd8ed7a` make foreshadows api write neo4j and materialize pg
- `e16c492` make foreshadows resolve/delete write neo4j
- `a86738c` materialize foreshadows deletion to postgres
- `49776e0` route foreshadow writes to neo4j source-of-truth

现状：
- `backend/app/api/foreshadows.py`、`backend/app/services/foreshadow_manager.py` 不再 PG 直写，全部走 `/neo4j-settings/foreshadows`。
- 外部 API URL 兼容（前端无需改）。
- materialize 函数：`backend/app/tasks/entity_tasks.py` 的 `_materialize_foreshadows_to_postgres()`。

### 2026-05-02 — F2 v1.10 路由族提前落地（PR #18 包含）

- ✅ `backend/app/api/neo4j_settings.py` 已在 main（characters / world_rules / relationships / locations / character_states / at_location / organizations / membership / foreshadows）。
- ✅ `backend/app/api/admin_entities.py` 已在 main（`POST /api/admin/entities/materialize`，env `ADMIN_USERNAMES` JWT-sub gate）。
- ✅ `backend/app/api/admin_usage.py` 同步入仓（`/api/admin/usage`）。

### 2026-05-02 — alembic head 推进至 `a1001908`

新增迁移：
- `a1001200_v10_usage_quotas`
- `a1001400` LLM tier routing（v1.4）
- `a1001401_v141_prompt_max_tokens`
- `a1001900_v190_*` 系列：characters_unique / relationships_unique / world_rules_unique
- `a1001904_v190_locations_table`
- `a1001905_v190_character_locations_table`
- `a1001906_v190_character_states_table`
- `a1001907_v190_organizations_table`
- `a1001908_v190_character_organizations_table`（current head）

部署前必须 `alembic upgrade head`。

### 2026-05-01 ~ 2026-05-02 早 — 此前已合入（保留摘要）

- PR #1 ~ #7：PR #1 v1.9 主要收敛 / PR #2 文档 / PR #3 legacy 410 / PR #4-#6 HANDOFF + RUNBOOK + verify 脚本 / PR #7 FOLLOW_UP_PLAN 决策辅助文档
- 本地状态清理（P0）：丢弃未提交 settings.py，drop stash，pull --ff-only
- 防回归扫描（P2）：远端 search_code + 本地 grep 两路扫描 PG 直写 = 0 命中（除已知 foreshadow 3 处，现已修复）

## 3. 当前未决事项 / Follow-up

### F3 P3 part 3：真实环境对账（仅本机环境可执行）

- 脚本：`scripts/verify_entity_writeback_v19.sh`（PR #6 入仓）+ `scripts/verify_entity_writeback.sh`（PR #18 入仓）
- 命令：`PROJECT_ID=... CHAPTER_IDX=... bash scripts/verify_entity_writeback_v19.sh`
- 前置：后端启动 + PG/Neo4j 连接通 + 真实 PROJECT_ID
- 输出：legacy 410 烟测 / extract-settings 路由探测 / PG 行数 / Neo4j 对账

### 部署 checklist

- [ ] `alembic upgrade head`（target = `a1001908`）
- [ ] 配置 env `ADMIN_USERNAMES`（JSON 数组，例 `["admin","king"]`），否则 `/api/admin/*` 路由族对所有 JWT-sub 返回 403
- [ ] 验证 v1.9 entity writeback 全链：写 `POST /api/projects/{pid}/neo4j-settings/foreshadows` → Neo4j `(:Foreshadow)` 节点出现 → PG `foreshadows` 表 materialize 出同一行
- [ ] 关闭 / 删除 `feature/v1.0-big-bang` 远端分支（其内容已 100% 在 main 上）
- [ ] 关闭 / 删除 11 个 `release/v1.*` 远端分支（已 merge）

### 后续大版本（参考 ITERATION_PLAN.md）

- v0.6 ~ v1.0 之间的大版本规划见 `ITERATION_PLAN.md` v0.6/v0.7/v0.8/v0.9/v1.0 章节（设计文档 `docs/V06_DESIGN.md` ~ `docs/V10_DESIGN.md`）。
- 现在 `feature/v1.0-big-bang` 上的内容（v1.0 - v1.9）已实质落地到 main，主要从 v1.0 → v1.9 横跨多个 design 文档主题。后续若要继续推进 v2.0+，从 `ITERATION_PLAN.md` 的 Iteration 系列继续。

## 4. 验证命令清单（可复制粘贴）

```bash
cd /root/ai-write

# 仓库静态健康
git log -1 --oneline                                    # expect: 0a4f9a1 Merge PR #18
python3 -m compileall -q backend/app && echo OK         # expect: OK
ls backend/alembic/versions/ | grep -c '^a100'          # expect: 12+

# v1.9 关键文件 blob hash 与 big-bang HEAD 73e7897 一致
for f in backend/app/api/foreshadows.py \
         backend/app/api/neo4j_settings.py \
         backend/app/api/admin_entities.py \
         backend/app/api/admin_usage.py \
         backend/app/services/foreshadow_manager.py \
         backend/app/services/usage_service.py; do
  if [ "$(git rev-parse main:$f)" = "$(git rev-parse 73e7897:$f)" ]; then
    echo "OK $f"
  else
    echo "DIFF $f"
  fi
done

# 真实环境对账（需要 PROJECT_ID + 后端运行）
PROJECT_ID=... CHAPTER_IDX=... bash scripts/verify_entity_writeback_v19.sh
```

## 5. 历史关键 PR 一览

| PR | 合并 SHA | 主题 |
|---|---|---|
| #1 | `cfbdbf4` | v1.9 主要收敛（outlines extract / world_rules ETL / relationships deletion sync） |
| #2 | `3195927` | README + ITERATION_PLAN 文档持续维护 |
| #3 | `3c9f4b0` | legacy PG 直写接口禁用（→ 410） |
| #4 | `ca96d2c` | HANDOFF_EXECUTION 模板 |
| #5 | `17fb371` | PROGRESS 模板 |
| #6 | `8e34c0b` | RUNBOOK + verify_v19 + .gitignore + README/410 修正 |
| #7 | `ea624e2` | FOLLOW_UP_PLAN.md 决策辅助 |
| #8 | `144e84e` | release/v1.0.0（20 commit） |
| #9 | `444d6bf` | release/v1.1.0（5 commit） |
| #10 | `8ea639a` | release/v1.2.0（5 commit） |
| #11 | `3a7eca1` | release/v1.3.0（6 commit） |
| #12 | `0d0d0b7` | release/v1.4.0（20 commit） |
| #13 | `4c6bbf4` | release/v1.4.1（7 commit） |
| #14 | `bb364a5` | release/v1.5.0（61 commit） |
| #15 | `31b4875` | release/v1.6.0（5 commit） |
| #16 | `0f79975` | release/v1.7.x（19 commit） |
| #17 | `b54159d` | release/v1.8.x（8 commit） |
| #18 | `0a4f9a1` | release/v1.9.0（81 commit, F1=A） |
