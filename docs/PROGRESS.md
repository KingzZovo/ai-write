# 项目当前进展（持续维护）

> **2026-05-04 00:40 交接**：上一窗口的 phase2-fix B1 批次交付 9 commits + B1 剩余 5 项未做，**严格看** `docs/HANDOFF_2026-05-04_phase2-fix.md`。

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

> 分支 `feat/outline-batch2`，HEAD = `f3e9e55`，自 main `1b52952` 起新增 9 个 commit（2 baseline + 7 PR）。
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


---

## 2026-05-03 V2 + V3B 实证报告 + 修订 Plan（**唯一权威 plan**）

> 取代 `FOLLOW_UP_PLAN.md` / `HANDOFF_TODO.md` / `HANDOFF_2026-05-03_outline-batch2.md` 的 PR 排序。后续 plan 演进只在本节追加，不开新文件。

### 1. V2 跑（200 万字、5 卷、staged_stream + 并发卷大纲，PID `20d164ab-232f-4863-8265-452186638d83`）

| 阶段 | 结果 | 备注 |
|---|---|---|
| A 建项目 | ✅ 3 s | `target_word_count=2000000` |
| B 全书大纲 | ✅ 7 m 04 s | book_oid `bf1b3cf1…`，**PR-OL10 仍失效**：volume_plan 输出 5 卷而非 3-5 |
| C1+C2 建 5 卷壳 | ✅ 4 s | 每卷 `est_chapters=150`、共 750 章 / 2 M 字 ≈ 2 667 字/章 |
| D 5 卷大纲并发 | ⚠️ **3/5** | vol 2/3/4 OK；vol 1（外滩怀表）/ vol 5（红玉无声）SSE 中断 fail |
| E `outline_to_facts.run_full_etl` | ❌ | inline `python -c` 内 `async def` 跟在 `;` 后 → SyntaxError |
| F 自动建章 | ❌ | `volumes.py` SSE handler `json.loads` 失败兜底 `{"raw_text": full}`，下游 `parsed.get("chapter_summaries") = None` → 0 章 |

**关键 bug PR-VOL2-PARSE**：`backend/app/api/volumes.py:217` 的 `try: parsed=json.loads(cleaned) except: parsed={"raw_text":full}` 兜底过宽。dry-run 直接 `python3 json.loads(raw_text)` 三卷全合法、各 150 章。SSE handler 拼 chunk 时附加了某种 trailing/control 字符导致 parse 失败，但兜底吞掉报错让链路静默断。

### 2. V3B 续跑（绕过 PR-VOL2-PARSE，直接从 raw_text 建章）

- **R1 建章** (backend 容器内 SQLAlchemy 直写) — vol 2/3/4 各 INSERT 150 章 = **450 章** ✅
- **R2 ETL** `run_full_etl(db, PID)` — `world_rules=29` ✅、`characters/foreshadows=0`（这一步只读章纲，没正文）
- **R3 章细化大纲** vol 2 ch 1-10 顺序 — **10/10 OK**，各 10-31 KB SSE 流，平均 ≈ 20 s/章
- **R4 章正文** vol 2 ch 1-10 并发 ×4 — **10/10 OK**，平均 ≈ 8 919 字符 / 章（target 3 000 字 ≈ 6 000-9 000 chars，符合），平均 290 s / 章

### 3. 14 项探针实证（V3B 跑完后取）

| # | 维度 | 数值 | 判定 |
|---|---|---|---|
| 1 | chapters total/with_outline/with_text | **450 / 450 / 10** | ✅ |
| 2 | characters | **47** | ✅ chapter 生成时触发 entity extraction |
| 3 | locations | **40** | ✅ |
| 4 | world_rules | **29** | ✅ R2 ETL 生效 |
| 5 | relationships | **51** | ✅ |
| 6 | character_states | **51** | ✅ |
| 7 | foreshadows | **0** | ❌ **伏笔提取链路完全断**（核心业务！） |
| 8 | organizations | **0** | ❌ 组织提取没触发（汇丰、军统、76 号都没建） |
| 9 | items / item_events | **0 / 0** | ❌ 道具提取没触发（核心道具「外滩怀表」漏掉） |
| 10 | faction_events / faction_oppositions | **0 / 0** | ❌ 派系事件链路缺失 |
| 11 | chapter_versions | **0** | ❌ SSE 直写 `chapters.content_text`，没建版本节点 → git-native 主张落空 |
| 12 | chapter_evaluations | **0** | ⚠️ `auto_revise=False` 业务设计，预期 |
| 13 | Neo4j (Character 47 / Location 40 / WorldRule 29 / ExtractionMarker 10) | ✅ | 同步写回 |
| 14 | 三线关键字（主线 / 感情 / 伏笔）grep on vol2 ch1-10 content | **全 0** | ❌ 三线注入到 prompt 但生成的正文没有显式标识，无法量化验证 |

附带 schema 偏差（影响 plan 文档准确性）：
- `characters` 表只有 `name + profile_json`（之前文档假设的 `role` 列不存在）
- `chapter_time_anchors.chapter_idx` 而非 `chapter_id`（外键设计偏弱，重命名章节不会更新锚点）

### 4. 修订 Plan — 17 PR 按优先级排序

#### Phase I 「修正不生效」（**最高优先级**，正在跑的项目能直接吃到）

| PR | 内容 | 触点 | 立判 |
|---|---|---|---|
| **PR-VOL2-PARSE** ⛔ | `volumes.py:217` 改为 `json.loads` 失败时**显式 raise + log 完整 chunk**；二级 fallback 用 `_extract_largest_json_object()` 分块扫描 | `backend/app/api/volumes.py` | 修后 V2 链路自然恢复，450 章自动建 |
| **PR-FACTS-FORE** ⛔ | `outline_to_facts.run_full_etl` 增加 `extract_foreshadows_from_volume_outline()`；prompt 把每章 outline_json 的 `foreshadow_state` 字段抽出 | `backend/app/services/outline_to_facts.py` + prompt | 跑完应有 ≥30 伏笔行 |
| **PR-FACTS-ORG** ⛔ | ETL 同步抽 `organizations`（军统/中统/76号/汇丰/同盟会等）+ `character_organizations` 关联 | 同上 | ≥8 组织 |
| **PR-FACTS-ITEM** ⛔ | ETL 同步抽 `items` 与 `item_events`（怀表/灰灯/封签等）+ Neo4j `Item` 节点 | 同上 + Neo4j writeback | ≥10 道具 |
| **PR-VER1** ⛔ | chapter SSE 流写完后**强制建** `chapter_versions` 行 `source="ai_generation" is_active=1`，启用 git-native 版本树 | `backend/app/api/generate.py` chapter post-save | ver count = chapter count |
| PR-EVAL1 | chapter SSE 完毕后异步触发 `ChapterEvaluator`（不阻塞返回），写 `chapter_evaluations`（即使 `auto_revise=False`） | 同上 + service | eval count ≥ chapter count |
| PR-SSE-FIX | volume outline SSE 端 600 s+ 心跳 keepalive；客户端断开时不丢已生成内容（vol 1/5 fail 的成因） | `volumes.py` + uvicorn | vol 1/5 重跑 OK |
| PR-OL10-fix | 修复全书大纲「下输出 5 卷而非 3-5」（再说一次：约束词被忽略，需提到 system prompt 顶部 + 后置校验回退） | `backend/app/services/book_outline.py` | volume_plan ≤ 5 卷 |
| PR-WIRE1 | 三线（主线/感情线/世界观）注入 prompt 后，**在生成的章节末尾追加 `<!-- strand: main=… love=… world=… -->` 注释**，让正文级量化可验证 | chapter generator template | grep 命中率 ≥ 90 % |

#### Phase II Mem-Forever 借鉴（4 PR，msg 6 决策）

| PR | 内容 | 来源 |
|---|---|---|
| PR-MEM1 | 四属性记忆元（who/when/where/what + decay/contradiction/mutation）→ 给 `character_states` 加 `decay_score / mutated_at / contradicts_state_id` 列 | Mem-Forever |
| PR-MEM3 | git-native 时间线：每个 character_state 变更走 `chapter_versions`-style 版本树 | Mem-Forever |
| PR-MEM2 | soul-memory：把 `style_profile` 切到「记忆体」结构，章正文写完后增量更新 | Mem-Forever |
| PR-MEM4 | 审稿/偏好录入 → 强依赖 PR-UI1/2/3 落地后 | Mem-Forever |

#### Phase III UI（3 PR，msg 7 决策，PR-MEM4 阻塞项）

| PR | 内容 |
|---|---|
| PR-UI1 | author_profile 录入页（口味、禁词、偏好节奏）|
| PR-UI2 | 审稿面板（章节级 issue 列表 + 修改建议）|
| PR-UI3 | 三线进度可视化（每卷一条三色线 = 主线 / 感情 / 世界观，节点 = 章）|

#### Phase IV Neo4j 状态机增强（2 PR，去重之前 NEO1-4 中已实现的 Location）

| PR | 内容 |
|---|---|
| PR-NEO5 | Time/Era/TimeEvent + `OCCURS_AT` 关系；用 `chapter_time_anchors` 实证派生 |
| PR-NEO6 | FactionEvent（结盟/破盟/开战/休战）从 `faction_events` 表派生写回 |

#### Phase V 三线增强（3 PR，msg 6 决策）

| PR | 内容 |
|---|---|
| PR-STRAND1 | 章生成 prompt 显式列出当前章应推进的三线节点（来自卷大纲 chapter_summary 的 main_progress / side_progress） |
| PR-STRAND2 | 章生成完后 strand_tracker 量化每条线推进 + 写 `strand_progress` 表 |
| PR-STRAND3 | UI 三线 dashboard（接 PR-UI3） |

#### Phase VI 朱雀 V1 CH2 第二轮 reduction（沿用前序 plan）

### 5. 跑通 Phase I 后的复跑验收脚本

复用 `/tmp/build_and_etl.py` + `/tmp/orchestrate_v3b.py`，跑完后预期：
- chapters 750 / 750 / 750（5 卷 × 150 章全建 + 全有 outline + 全有 text）
- foreshadows ≥ 200、organizations ≥ 8、items ≥ 30
- chapter_versions = 750、chapter_evaluations = 750
- 三线注释 grep 命中率 ≥ 90 %

### 6. 当前未决 / Follow-up

- vol 1（外滩怀表）/ vol 5（红玉无声）卷大纲未落库 → PR-SSE-FIX 修后重跑
- characters.role 列实际不存在（在 `profile_json.role`）→ 文档校正条目
- chapter_time_anchors.chapter_idx 而非 chapter_id → 设计偏弱，章节重排会丢锚

### 7. 临时 artefact（不入 git）

- `/tmp/orchestrate_v2.py` `/tmp/orchestrate_v3b.py` `/tmp/build_and_etl.py` `/tmp/probes_v3b.sh`
- `/tmp/orchestrate_v2_status.json` `/tmp/orchestrate_v3b_status.json` `/tmp/probes_v3b.out`
- `/tmp/sse_v3b_outline_v2c{1..10}.log` `/tmp/sse_v3b_text_v2c{1..10}.log`

---

## B2 — Phase II 修复（2026-05-03，feat/phase2-fix）

### 触发问题（用户实测，user msg 12 + 13）
1. 三线平衡显示 ⚠Quest/Fire/Constellation 已 150 章未推进。
2. 伏笔追踪都是 50%。
3. 设定集 → 人物 profile 全为空。
4. 设定集 → 世界规则全无。
5. 右侧「查看全书/分卷/章节大纲」点击无效。
6. 第一卷为空，从第二卷开始生成内容。
7. TOKEN 用量始终为 0。
8. 全书大纲文本中泄露 `<volume-plan>...</volume-plan>` LLM 控制标签。

### 修复（6 个 commit on feat/phase2-fix，已 push）

| # | Commit | PR | 文件 | 解决症状 |
|---|---|---|---|---|
| 1 | `5ab7782` | PR-OL15 + PARSE-VOL | outline_generator.py / generate.py / OutlineTree.tsx / MobileWorkspace.tsx | #6 #8 + 解锁下游结构化数据流 |
| 2 | `e49a05f` | PR-FACTS-CHAR-PROFILE | outline_to_facts.py | #3 |
| 3 | `7dbad7c` | PR-USAGE-SYNC | llm_call_logger.py | #7 |
| 4 | `64743cc` | PR-WORLDRULES-FE | SettingsPanel.tsx | #4 |
| 5 | `ef06e24` | PR-OUTLINE-BUTTONS | chapters.py | #5（章节大纲按钮）|
| 6 | `260cbb8` | PR-STRAND-OUTLINE | strand_tracker.py | #1 |

#9 伏笔 50%（user msg 12 第二项）= 「unresolved/total = 当前所有有 setup 但暂未 resolve 的伏笔比例」 → 50% 是符合预期的 mid-stream 比率，不修。

### 关键根因发现（第二轮深探）

- **B2 #1 + #3 共享根因 — 写盘缺陷**：`generate.py:943` 的 `_content_json = {"raw_text": full_text}` 仅书级提取 `volume_plan`，卷/章级别**从未把 LLM JSON 解析出来**，导致 vol2 outline content_json 实测 `keys=['raw_text']`、`new_characters count=0`，下游所有 ETL 即使流程正确也拿不到结构化数据。**PR-OL15 + PARSE-VOL 同时解决了 B2 #6 + #8 + 解锁 #3 + #7 数据链路**。
- **B2 #5 章节大纲按钮**：根因不是 onClick 失效，是 `lightweight=true` 后端省略 `outline_json` 字段，前端条件 `Boolean(chapter.outline_json)` 永远 false。
- **B2 #4 世界规则**：根因不是后端缺路由（路由 `/api/projects/{pid}/world-rules` 存在），是前端 `RuleResp { rules?: ... }` 字段名不匹配（后端返回 `world_rules`）。

### 实测验证（V2 PID）

```
=== before B2 ===
characters=47        with_profile_json=0  ← #3
world_rules=83       (后端有数据)        ← #4 字段名错
foreshadows=450
organizations=36
items=49
outlines vol2 keys=['raw_text']         ← B2 写盘缺陷证据
outlines tag occurrences <volume-plan>=1

=== after B2 (代码 patch + SQL 数据回填) ===
characters=66        with_profile_json=14 ← +14 非空 ✅
world_rules=111      (前端可正确渲染 ✅)
foreshadows=463      ← +13
outlines vol2 keys 补足 chapter_summaries / new_characters / world_rules / volume_idx ✅
outlines tag occurrences <volume-plan>=0  ✅
```

### 数据补救

- **SQL/Python 一次性脚本**（`/tmp/p2_clean_outlines.py`）：14 行 outline 中 1 行 strip volume-plan tag、13 行从 raw_text 反向解析回填 `chapter_summaries / new_characters / world_rules / volume_idx`。
- **ETL 重跑**：etl_characters / world_rules / foreshadows / organizations / items 依次跑通，profile_json 14 行非空。
- **vol1 + vol5 SSE 重跑**：后台 nohup 进行中（pid 4065651 / 4065756，约 4-6 min/卷）。完成后 `chapters` 表 v1/v5 各 150 章。

### Follow-up

- 前端代码改动需要重启 Next.js dev server（无 docker 容器，`cd frontend && npm run dev`）或重新 build。
- 后端已重启，5 个后端 patch 全部生效。
- 待 vol1/vol5 SSE 跑完，再次跑 ETL，characters 有望追加新角色，foreshadows 也会增长。

## 2026-05-03 22:25 — 502 修复 + frontend 镜像 rebuild

### 真根因
- 用户访问 8080 → ai-write-nginx 反代
- ai-write-nginx 配置 set $frontend_upstream http://frontend:3000 + resolver 127.0.0.11（docker 内部 DNS）
- nginx error log: frontend could not be resolved (2: Server failure)
- docker compose ps: frontend 服务 missing（其他 9 容器 Up）
- 宿主机有个 next-server 进程跑在 *:3000 但不在 docker network 里 → ai-write-nginx 不可达 → 502

### 修复
1. docker compose up -d frontend → 容器起来，烟测 8080 返回 200（root/login/api/health）
2. 杀宿主机 stale next-server PID 4088553（释放 *:3000，避免后续混淆）
3. 发现 ai-write-frontend:latest 镜像是 2026-05-02T22:00Z build，早于 PR-WORLDRULES-FE / PR-OL14 / PR-OL15（2026-05-03 02:54~09:26）
4. docker compose build frontend → 新镜像 2026-05-03T14:24Z，自动 recreate 容器
5. 烟测 8080 全部 200，nginx access log 干净

### 认知更正
- 127.0.0.1:3001 是 grafana docker-proxy（不是 next-server）
- next-server 之前一直在宿主机 *:3000，但用户看到的是 8080→nginx→docker frontend:3000；所以前端 commit 是否生效取决于 docker 镜像 build 时间，不是宿主机进程
- user msg 12/15 报的 5 症状之所以"修了还在"，部分原因是新前端代码从未真正进入 ai-write-frontend 镜像
