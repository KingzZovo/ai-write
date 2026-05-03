# 项目当前进展（持续维护）

> **2026-05-03 12:25 交接**：上一窗口的 outline-batch2 全部交付 + 未完任务清单看 `docs/HANDOFF_2026-05-03_outline-batch2.md`（新窗口接手优先看那份）。

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
- 远端状态：`origin/main` HEAD = `960a38b`（PR #19 合并）；P6 仓库清理已完成（仅剩 `main` + `archive/feature-v1.0-big-bang` tag）

## 2. 已完成（按时间倒序）

### 2026-05-02 晚 (E2E 全业务验证 + chapter generate-stream 签名修复)

**场景**：全链路 E2E 跑一遇 — 拆分参考小说 → 提取/蒸馏 → 向量化/入库 → 创建小说 → 书级大纲 → 分卷大纲×3 → 章节大纲×30 → 生成 30 章正文。

**项目**：`0eaeff87-2f91-452c-812c-b4bcf2924fe2` (《城下听潮》×仿《龙族》中二都市风)

**阶段结果**
| 阶段 | 状态 | 产出 |
|---|---|---|
| P1 参考书预处理 | ✅ | 3 本 status=ready (《龙族》×2 / 《三体》×1)、qdrant 多维 collection 全点亮 |
| P2 创建项目 | ✅ | PID 如上 |
| P3 书级大纲 | ✅ | OID `f9af7582...` 6766 chars、is_confirmed=1 |
| P4 分卷大纲 ×3 | ✅ | TID 3个、并发 60-70s、章节摘要 JSON 30×{title,summary,key_events} |
| P5 物化 PG | ✅ | 3 volumes + 30 chapters、每章携 outline_json |
| P6 生成 30 章正文 | ✅ | **需修复** chapter generate_stream 签名 bug (见下) 后 30/30 全过 |

**Chapter 产出**：30/30 done，277,882 字，avg 9263，min 7377，max 13155；单章耗时 178~280s（4-worker 并发 ≈27 min 跑完 30 章）。抽样 v1.c1 / v2.c5 / v3.c10 剧情连贯、人物（林渡、乔野、闻栖枝）跨卷一致、语言风格合「中二热血×现代都市」。

**Bug fix (PR #21)**：`backend/app/tasks/knowledge_tasks.py` 调用 `ChapterGenerator.generate_stream` 仍按 v0.4 签名传 `project_settings/world_rules/...`，与 v0.5+ 新签名（`*, project_id, volume_id, chapter_idx, db, chapter_id, user_instruction`）不匹配，导致所有 `task_type=chapter` 的 celery 任务 0.06s 立刻 TypeError。patch +9/-3，合并后 E2E 过。

**后续（未起）**
- 章节生成未自动触发 entity 抽取/cascade/evaluation（`characters=0, foreshadows=0, cascade_tasks=0`），如需可手动调用 `entities.extract_chapter` / `evaluations.evaluate_chapter` celery task 或接入生成后钩子。
- 建议加 `tests/tasks/test_run_async_generation_chapter.py` mock ChapterGenerator 锁定签名。

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
- [x] **仓库清理完成**（2026-05-02 晚）：origin 上仅剩 `main` + `archive/feature-v1.0-big-bang` tag（锁 `73e7897` 237 commit 历史）。删除：feature/v1.0-big-bang + 11 个 release/v1.* + 4 个 doc/fix 分支 + 3 个 chore 分支

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

## feat/outline-batch2 — 7-PR 批次 ✅ 已 push（2026-05-03）

> 分支 `feat/outline-batch2`，HEAD = `38c4413`，自 main `1b52952` 起新增 11 个 commit（2 baseline + 7 PR + 2 docs）。
> 背景：上一轮 E2E PID `310c1f9a` 《狩人账》30 章跡走后朱雀 AI 检测 12.04% 人工 / 42.21% 疑似 / 45.75% AI，六个架构问题取得共识后拆出本批 7 PR 修正。

### Commit 链

| commit | PR | 主题 |
|---|---|---|
| `bf1ae1e` | baseline backend | 抢救 PR-OL1~9 backend 工作区改动（12 文件 +942/-16）|
| `de6d623` | baseline frontend | 抢救 fallback 卡片 / cascade UI / i18n（8 文件 +989/-599）|
| `70706c9` | **PR-OL10** | 字数→章数→卷数自动推算（默认 4000 字/章、100-200 章/卷）+ prompt 硬约束注入 |
| `e838cd6` | **PR-OL11** | 分卷大纲 chapter_summaries 强化（60-100 字 + 主线/支线/伏笔/关键场景）+ `extract_chapter_breakdown()` helper |
| `4b515ba` | **PR-OL12** | 章节大纲调用层补 `previous_chapter_summary` + 本章预规划注入 |
| `3d07194` | **PR-OL13** | 章节大纲生成后解析 `title` 回写 `Chapter.title`（清除「第N章」占位）|
| `f6fa9e5` | **PR-OL14** | OutlineTree 三层查看入口（全书/分卷/章节大纲）|
| `919abab` | **PR-AI1** | 命名与词汇硬约束：`FORBIDDEN_HALLUCINATION_TERMS` + `NAMING_DIRECTIVE` + context_pack 注入 |
| `f3e9e55` | **PR-STY1** | style v9 5 条节奏/留白/信息密度/句式/在场 directive + context_pack 注入 |

### Verification

- 所有 backend 改动过 `python3 -m py_compile`。
- 所有 frontend 改动过 `cd frontend && npx tsc --noEmit -p tsconfig.json`。0 错误。0 警告。
- 行为级 E2E 验证 + 朱雀复测 **延后** 到本批全部落定后一次跑完成，避免每个 PR 都付 SSE 长任务成本。

### 重跑测试预计步骤

1. 则使用 PID `310c1f9a` 清 30 个 Chapter / 30 个 chapter outline / 9 个已生成正文（保留 book outline + 20 个 volume outline 可复用）或新建项目。
2. POST `/api/projects` `target_word_count: 2000000`，走完全书 outline + 各卷 volume outline + 全 30 章 chapter outline + 30 章正文，验证：
   - 全书大纲 「七、分卷规划」 应说 「下输出 3-5 卷」（PR-OL10）。
   - 各 volume_outline.chapter_summaries 每项含 main_progress / side_progress / foreshadow_state / key_scene（PR-OL11）。
   - 各 chapter outline 调用 应额外携 previous_chapter_summary（PR-OL12，看 backend log）。
   - Chapter.title 不再是 「第 N 章」（PR-OL13，查 DB）。
   - 前端侧栏 OutlineTree 顶部能展开 「全书大纲」，每章能展开 「大纲」（PR-OL14）。
   - 生成的正文 grep 不到 「怎表」/「屃门」/「黄铜怎表」类含词（PR-AI1）。
   - 生成的正文节奏合理，段落长短交错，周期出现 1-2 句短段（PR-STY1）。
3. 取 V1 CH2 等价隐藏位置贴朱雀 AI 检测，取人工/疑似/AI 三段比例与 baseline 12.04% / 42.21% / 45.75% 对比。

### Neo4j 状态机扩展状态（不在本批，居后动工）

| 维度 | 现状 | 缺口 |
|---|---|---|
| 地点 | ✅ 已实现 `Location` 节点 + `AT_LOCATION` 关系（chapter_start 时序）| 无 |
| 阵营 | ⚠️ 半：有 `Organization` 节点 + `MEMBER_OF`，没有 「阵营事件」 | 缺 `FactionEvent`（结盟/破盟/开战/休战）|
| 道具 | ❌ 未实现 | 缺 `Item` 节点、`HAS_ITEM`/`USES_ITEM` 关系、prompt 不抽 `items` |
| 时间 | ❌ 未实现（仅 chapter_start 隐式时序）| 缺 `Time`/`Era`/`TimeEvent` 节点、`OCCURS_AT` 关系 |

列为下一批 PR-NEO1~NEO4 开新分支 `feat/neo4j-batch1`。

## 2026-05-03 — feat/neo4j-batch1 PR-NEO1~NEO4 v2.0 批次（branched off `38c4413`）

分支：`feat/neo4j-batch1`，HEAD = `fe01405`（4 个 feat commit + 1 个 docs commit）。

```
fe01405 feat(neo4j): PR-NEO4 wire NEO1-3 PG projections into ContextPack + critic
6d21591 feat(neo4j): PR-NEO3 add Time anchors + chapter linkage
932831a feat(neo4j): PR-NEO2 add FactionEvent + opposition windows
fa82700 feat(neo4j): PR-NEO1 add Item entity + ownership/transfer events
1834cdb docs(handoff): align HEAD pointer to 38c4413 in outline-batch2 docs
```

### PR-NEO1 道具

- 新建 alembic `a1001909_v200_items_tables.py`：`items(name/kind/first_owner)` + `item_events(kind ∈ has/use/transfer/break/recover, chapter_idx, actor_name)`，`uq_items_project_name` + `uq_item_events_key`。
- `models/project.py` append `Item` + `ItemEvent`。
- `entity_timeline.py`：文档 schema、`ENTITY_EXTRACTION_PROMPT` 加 `items[]` + `item_transfers[]`、`initialize_graph` 加 (:Item) PK + HAS_ITEM uniqueness + Item index、helpers `_ensure_item` / `_set_item_owner` / `_record_item_transfer`、`extract_and_update` 处理 items + transfers。
- `entity_tasks.py`：imports + Neo4j 读 :Item/HAS_ITEM/TRANSFER_ITEM 块 + 计数器 `created_items` / `created_item_events` + PG upsert（items on_conflict_do_update uq_items_project_name 更新 kind/first_owner，item_events on_conflict_do_nothing uq_item_events_key）+ 返回字典加新字段。
- diff stat: 3 files +368/-1 + 1 新 alembic 文件，COMPILE_OK。

### PR-NEO2 阵营事件 + 对立窗口

- 新建 alembic `a1001910_v200_faction_events_tables.py`：`faction_events(kind ∈ alliance/break/war_open/war_close/...)` + `faction_event_orgs` 多对多 + `faction_oppositions(source_org_id, target_org_id, chapter_start, chapter_end)`，`uq_faction_event_orgs_key` + `uq_faction_oppositions_key`。
- `models/project.py` append `FactionEvent` / `FactionEventOrg` / `FactionOpposition`。
- `entity_timeline.py`：文档 schema 加 (:FactionEvent) + INVOLVED_IN + OPPOSED_BY、ENTITY_EXTRACTION_PROMPT 加 `faction_events[]` + `faction_oppositions[]`、`initialize_graph` 加 (:FactionEvent) PK + OPPOSED_BY uniqueness + FactionEvent index、helpers `_ensure_faction_event` / `_link_org_to_event` / `_set_faction_opposition`（写 chapter_start，下一次 close 时回填 chapter_end）、`extract_and_update` 处理新字段。
- `entity_tasks.py`：imports + Neo4j 读 :FactionEvent/INVOLVED_IN/OPPOSED_BY 块 + 计数器 `created_faction_events` / `created_faction_event_orgs` / `created_faction_oppositions` + PG upsert + 返回字典加新字段。
- diff stat: 3 files +384/-0 + 2 新 alembic 文件（`a1001910` + `a1001911`，后者为 NEO3 alembic 文件，提前落入此 commit）。

### PR-NEO3 时间锚点

- alembic `a1001911_v200_time_events_tables.py`（实际在 NEO2 commit 中已建）：`time_anchors(label, kind, abs_value)` + `chapter_time_anchors(chapter_idx, time_anchor_id, precision, offset_value, anchor_label)`，`uq_time_anchors_key(project_id, label, kind)` + `uq_chapter_time_anchors_key(project_id, chapter_idx, time_anchor_id)`。
- `models/project.py` append `TimeAnchor` + `ChapterTimeAnchor`，imports 加 `BigInteger`。
- `entity_timeline.py`：文档 schema 加 (:Time) + (:Chapter)-[:OCCURS_AT]->(:Time)、ENTITY_EXTRACTION_PROMPT 加 `time_anchors[]`（label/kind/precision/offset_value/anchor_label）、`initialize_graph` 加 (:Time) PK (project_id,label,kind) + (:Chapter) PK (project_id,chapter_idx) + ()-[r:OCCURS_AT]-() PK + Time index、helpers `_ensure_time_anchor` / `_link_chapter_to_time`、`extract_and_update` 处理 `time_anchors`。
- `entity_tasks.py`：imports + Neo4j 读 (:Time) 节点 + (:Chapter)-[:OCCURS_AT]->(:Time) 关系 + 计数器 `created_time_anchors` / `created_chapter_time` + PG upsert（time_anchors on_conflict_do_update 更新 abs_value，chapter_time_anchors on_conflict_do_update 更新 precision/offset_value/anchor_label）+ 返回字典加新字段。
- diff stat: 3 files +302/-0，COMPILE_OK。

### PR-NEO4 消费层（ContextPack + Critic）

- `services/context_pack.py`：
  - imports 加 `Item / ItemEvent / FactionEvent / FactionEventOrg / FactionOpposition / Organization / TimeAnchor as TimeAnchorRow / ChapterTimeAnchor`。
  - ContextPack 加三段字段：`current_items: list[dict]` / `faction_state: dict` / `timeline_anchors_v2: list[dict]`。
  - `to_system_prompt` 在 L2 「时间线锚点」 后渲染三新块：「当前道具持有」 / 「阵营动态」 / 「时间锚点 v2」。
  - 新私方 `_build_pr_neo4_facts`：
    - current_items：`items` LEFT JOIN 「最近一次 has/transfer item_event」 取 holder（chapter_idx ≤ 当前章）。
    - faction_state：oppositions（chapter_start ≤ 当前 AND (chapter_end IS NULL OR chapter_end ≥ 当前)） + 最近 10 条 faction_events 含 org 名。
    - timeline_anchors_v2：chapter_time_anchors JOIN time_anchors，取 ≤ 当前章前 30 条。
    - 三块各自 try/except，DB schema 缺失不阻断。
- `services/generation_runner.py::_phase_critic`：
  - 加 PG 读 `Item.name` 列表 → `item_names`。
  - 加 PG 读 chapter `chapter_idx` → `chapter_idx_for_critic`。
  - 把两值传给 `run_critic`，让 `scan_item_missing` / `scan_time_reversal` / `scan_geo_jump` 拿到真实输入。
- diff stat: 2 files +217/-0，COMPILE_OK。

### Verification

- 4 个 commit 全部 `python3 -m compileall -q backend/app` → COMPILE_OK。
- alembic 链：`a1001908` → `a1001909` (NEO1) → `a1001910` (NEO2) → `a1001911` (NEO3)。
- 行为级 E2E（真实 PG round-trip + LLM 抽 items / faction_events / time_anchors） **延后** 到 task A 30 章 E2E 重跑时一并验证；那里也会贴朱雀 AI 检测对比 baseline 12.04% / 42.21% / 45.75%。

### 留给下一批的 follow-up

- 当 LLM 真正吐出 items / faction_events / time_anchors 后，确认 PG 三对表 row count 不为 0，并且 ContextPack `to_system_prompt` 输出确实包含三新块（直接 print 一次即可）。
- 三个 checker（`time_reversal` / `geo_jump` / `item_missing`）目前已能拿到 PG / Neo4j 输入；尚未加 critic 端「issue.severity」 校准 + 单测，列入下批 PR-NEO5。
- alembic head 最高已到 `a1001911`，没消费更高号；如未来需更多 v2.0 表，从 `a1001912` 起编号。
