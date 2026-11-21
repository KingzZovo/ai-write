# AI Write v1.4 LLM 路由分档与配置 UI 设计规格

**日期：** 2026-04-23
**状态：** Draft（待 writing-plans）
**上一版本：** v1.3.0（2026-04-23, HEAD f1f4730）
**基线 Alembic head：** a1001300
**目标 Alembic head：** a1001400
**超级流程：** superpowers: brainstorming → writing-plans → executing-plans

## 1. 背景与目标

v0.5 已经把所有 LLM 路由下沉到 `PromptAsset`（每条 prompt 可绑 `endpoint_id + model_name + temperature + max_tokens`），v1.3.0 完成了字数预算、进度回填、volume 级 budget 等基础设施。现阶段整条管线已经形成的调用栈可粗分为三条线：

1. **参考小说切片线（高频）**：`reference_ingestor._process_slice()` 三路并发 → `style_abstractor` / `beat_extractor` / `entity_redactor`，三路各自调一次 `feature_extractor.generate_embedding()` 写 Qdrant。
2. **大纲 / 设定线（一次性）**：`outline_from_reference` → `OutlineGenerator`（卷/章大纲）→ `settings_extractor`（人物 / 世界设定 / 关系三合一 JSON）。
3. **逐章生成线（逐章）**：`generation_runner.execute_run` 的 LangGraph 拓扑 plan → recall → draft → critic → [rewrite ↔ critic] → finalize → compact，BVSR 打开时 draft/critic 分别 ×N。

这三条线目前都走同一个 `model_router.generate(task_type=...)` 的路由，但 task_type 只有 11 个粗粒度档（`style_abstraction / beat_extraction / redaction / extraction / outline_book / outline / generation / critic / rewrite / polishing / embedding`）。暴露出三个问题：

1. **差异化配置的颗粒度不够**：想给「人物抽取」用严格 JSON 小模型、「关系图」用便宜小模型、「一致性判官」用旗舰模型，这些在当前 task_type 里全被压扁成 `extraction` 和 `critic` 两档。
2. **配置界面缺口**：`PromptAsset` 管理页虽然能挂 endpoint，但没有「档位」标签，没有全局路由矩阵视图，新 task_type 上线后前端没有入口能分配不同模型。
3. **critic 职责混合**：`critic_service.run_critic` 里把硬伤检查（时间倒流、地理跳跃、物品丢失、一致性）和软指标（AI 味、节奏、阅读拉力）混在同一个 LLM 调用里，无法按职责换模型。

v1.4 目标：按职责拆 task_type + 在数据模型和前端都加「档位」概念 + 把必要的新 task_type 落到服务层，为 v1.5 的章型路由和 embedding 分档留口。

## 2. 非目标（留给 v1.5）

- **不拆 `generation`**：章型路由（opening / body / climax）依赖 `chapters.chapter_type` 字段，v1.5 做。
- **不拆 embedding**：不引入 `embedding_summary` 第二档 embedding，不建第二个 Qdrant 集合。召回空间统一由 v1.5 评估后再动。
- **不改 Qdrant schema**：现有 `plots / styles / chapter_summaries` 三个集合保持原样。
- **不引入新 LLM provider 类型**：保持 `anthropic / openai / openai_compatible` 三类，只在现有 endpoint 上加 `tier` 标签。
- **不做 ReAct agent / memory banks / reward loop**（与 v0.5 非目标延续一致）。
- **不改 `writing_guides.py:614` / `outline_from_reference.py` / `budget_allocator.py` / `chapters.target_word_count` 默认值**（v1.3.0 硬规则延续）。

## 3. 核心设计决策

| 决策 | 选择 | 备选被拒原因 |
|-----|------|-----------|
| 档位如何表达 | **`tier` 枚举字段**（`flagship` / `standard` / `small` / `distill` / `embedding`）挂在 `llm_endpoints` 上，`prompt_assets.model_tier` 作为期望标签 | 用 naming convention 藏在 `model_name` 里不可查询；再建一张 `tier_registry` 表过度设计 |
| 分档粒度 | **每个 endpoint 一个 tier + 每个 prompt 一个 model_tier 偏好** | 只给 endpoint 打 tier 不够——同一 endpoint 可能被多个 tier 复用；只给 prompt 打 tier 则无法快速过滤 endpoint 下拉框 |
| critic 拆分方式 | **新增 `critic_hard` + `critic_soft` 两个 task_type，原 `critic` 保留为兼容别名**（`prompt_registry` 在 resolve 时做 fallback） | 直接改 `critic` 为 `critic_hard` 会让老配置丢档；加 `critic_v2` 名字会越攒越乱 |
| 一致性校验是否独立 | **独立 task_type `consistency_llm_check`，由 `critic_hard` 管线按需触发** | 塞在 `critic_hard` 里会让 prompt 过长无法单独评估模型效果 |
| settings_extractor 拆分 | **三个独立 task_type `characters_extraction` / `world_rules_extraction` / `relationships_extraction`**，原 `settings_extractor` 服务改为编排三个子调用 | 合并成一个大 prompt 让 JSON schema 无法稳定约束；保留原 task_type 做兼容意味着老路径永远不会被替换 |
| rag_query_rewrite 触发时机 | **在 `ContextPackBuilder._build_rag()` 召回前插入，可配置开关 `rag_query_rewrite_enabled`** | 默认总是跑会让开发期 smoke 变慢；完全不默认开又没人用 |
| 前端改动范围 | **3 个页面**：Endpoint 管理页加 `tier` 下拉；PromptAsset 管理页加 `model_tier` 下拉 + 按 tier 过滤 endpoint；新增 `/llm-routing` 矩阵总览页（只读） | 把所有 tier 配置塞进 `/prompts` 一个页面会让行数爆炸，而且看不清全貌 |
| 兼容性 | **向后兼容**：`prompt_assets.model_tier` nullable，未设置时行为与 v1.3.0 完全一致；`llm_endpoints.tier` 默认 `standard` | 强制迁移会让用户升级时大量 prompt 突然失效 |
| 发布节奏 | **v1.4 单次发布**，~15-18 chunk | 拆两版会让 v1.4 的前端页面做一半 |

## 4. 架构总览

```
                 ┌──────────────── /llm-routing 矩阵总览（新）────────────────┐
                 │ task_type → model_tier → endpoint → model_name            │
                 │ 只读全景表                                                 │
                 └─────────┬─────────────────────────────────────────────────┘
                           │
               ┌───────────┼───────────┐
               ↓           ↓           ↓
        /endpoints (扩)  /prompts (扩)     prompts_registry
        endpoint.tier    prompt.model_tier (后端路由入口)
        旗舰/中档/小型/   偏好标签              │
        蒸馏/embedding                         ↓
                                        model_router.generate(task_type)
                                              │ 先查 tier 匹配，再落到 endpoint
                                              ↓
                         ┌────────────────────┼────────────────────────┐
                         ↓                    ↓                        ↓
               reference_ingestor         critic_service         settings_extractor
               (3 路并发)                 (3 路分流)              (3 路并发)
                 │  style_abstraction      │  critic_hard          │  characters_extraction
                 │  beat_extraction        │  critic_soft          │  world_rules_extraction
                 │  redaction              │  consistency_llm_check│  relationships_extraction
                 │                         │
                 ↓                         ↓
               generate_embedding  （单一 embedding provider，v1.4 不拆）
                 │
                 ↓
               QdrantStore（3 个集合不变）

   generation_runner._phase_planning → （新）rag_query_rewrite → ContextPackBuilder._build_rag()
   （可配置开关，默认关）
```

核心变更：

1. `llm_endpoints.tier`（新列）：`flagship` / `standard` / `small` / `distill` / `embedding`，默认 `standard`。
2. `prompt_assets.model_tier`（新列）：同值域，nullable，仅作「偏好」。
3. 新增 7 个 task_type（`BUILTIN_PROMPTS` 补齐）：`critic_hard`, `critic_soft`, `consistency_llm_check`, `rag_query_rewrite`, `characters_extraction`, `world_rules_extraction`, `relationships_extraction`。
4. `critic_service.run_critic`：按 `critic_hard` + `critic_soft` 两路并发，`consistency_llm_check` 由 hard 按需触发。原 `critic` 保留作为兜底别名。
5. `settings_extractor.extract_settings_from_outline`：由「一次大 JSON」改为三次并发结构化调用，再在 Python 合成最终 `SettingsBundle`。
6. `context_pack.ContextPackBuilder._build_rag`：在 Qdrant 召回前可选插入 `rag_query_rewrite`（HyDE），产出改写后的 query 再入召回；由 `settings.rag_query_rewrite_enabled` 控制，默认 `false`。
7. 前端：
   - `/model-config` Endpoint 管理页加 `tier` 下拉。
   - `/prompts` PromptAsset 管理页加 `model_tier` 下拉 + endpoint 下拉按 tier 过滤。
   - 新增 `/llm-routing` 矩阵总览页（只读）。
8. Alembic `a1001400_v14_llm_tier.py`：加两列 + 回填内建 prompt 默认 tier + 插入 7 个新 prompt 行（服务端路由 fallback）。

## 5. 组件清单

### 5.1 数据模型改动（Alembic `a1001400`）

#### `llm_endpoints`（扩字段）

```python
tier: str = "standard"   # VARCHAR(20) NOT NULL default 'standard'
                         # CHECK tier IN ('flagship','standard','small','distill','embedding')
```

- 索引：`ix_llm_endpoints_tier` on `(tier)`。
- 约束：CHECK 约束通过 `sa.text("tier IN (...)")`，Alembic `create_check_constraint`。

#### `prompt_assets`（扩字段）

```python
model_tier: str | None = None   # VARCHAR(20) NULL
                                # CHECK model_tier IS NULL OR model_tier IN ('flagship','standard','small','distill','embedding')
```

- 索引：`ix_prompt_assets_model_tier` on `(model_tier)`。
- 语义：未设置时 = 「任意 endpoint 可匹配」；设置后 = 「优先挑同 tier 的 endpoint，不存在时 fallback 到 endpoint_id 显式绑定」。

#### 新内建 prompt 行

在 `prompt_registry.BUILTIN_PROMPTS` 加 7 条，迁移 upgrade 里 INSERT ON CONFLICT DO NOTHING：

| task_type | name | model_tier 默认 | category |
|-----------|------|----------------|----------|
| `critic_hard` | 硬伤检查 | `standard` | Evaluation |
| `critic_soft` | 软指标检查 | `small` | Evaluation |
| `consistency_llm_check` | 一致性判官 | `standard` | Evaluation |
| `rag_query_rewrite` | RAG 检索改写 | `small` | RAG |
| `characters_extraction` | 人物抽取 | `standard` | Extraction |
| `world_rules_extraction` | 世界设定抽取 | `standard` | Extraction |
| `relationships_extraction` | 关系抽取 | `small` | Extraction |

### 5.2 后端服务改动

#### `backend/app/services/model_router.py`

- 新增 `def _pick_endpoint_by_tier(task_type: str, tier: str | None) -> Endpoint | None`：在现有 `_get_route` 之前先按 tier 匹配，匹配失败回落到现有逻辑。
- 新增 `def list_routes_matrix() -> list[RouteMatrixRow]`：供 `/llm-routing` 矩阵页使用。
- 签名兼容：`generate(task_type, messages, ...)` 不变，仅内部 resolve 分支增加。

#### `backend/app/services/prompt_registry.py`

- `BUILTIN_PROMPTS` 增加 7 条。
- `resolve_route(task_type)` 在找不到指定 task_type 时加 fallback 表：`critic_hard → critic`、`critic_soft → critic`、`consistency_llm_check → critic`、`characters_extraction | world_rules_extraction | relationships_extraction → extraction`、`rag_query_rewrite → extraction`。
- 新 helper `resolve_tier(task_type) -> str | None`：读 `prompt_assets.model_tier`。

#### `backend/app/services/critic_service.py`

- `run_critic` 重构：保留现有规则检查层（anti_ai_scanner + time_reversal + geo_jump + item_missing），LLM 层改为并发 `run_structured_prompt("critic_hard")` + `run_structured_prompt("critic_soft")`。
- `critic_hard` 命中「硬伤」标签时，按 flag 可再调 `run_structured_prompt("consistency_llm_check")` 做第二轮专项校验。
- 合并两路 JSON 结果为原 `CriticReport` 结构，外部调用方 `generation_runner._phase_critic` 无感知。

#### `backend/app/services/settings_extractor.py`

- `extract_settings_from_outline(outline)` 由单次 `router.generate(task_type="extraction", ...)` 改为 `asyncio.gather` 三路：`characters_extraction` / `world_rules_extraction` / `relationships_extraction`。
- 返回 `SettingsBundle(characters, world_rules, relationships)` 合成。
- 原 `extraction` task_type 保留为兜底（fallback 路径），任意子调用失败时降级走原始大 JSON prompt。

#### `backend/app/services/context_pack.py`

- `ContextPackBuilder._build_rag(query: str)` 前插 `_maybe_rewrite_query(query)`，当 `settings.rag_query_rewrite_enabled` 为 True 时调 `run_text_prompt("rag_query_rewrite", query)`，产出改写 query 再入 Qdrant 召回。
- 新增设置项 `RAG_QUERY_REWRITE_ENABLED: bool = False`（`config.py`）。

#### 新 API `GET /api/llm-routing/matrix`

返回 `list[{task_type, prompt_id, model_tier, endpoint_id, endpoint_name, endpoint_tier, model_name, temperature, max_tokens}]`，供前端 `/llm-routing` 矩阵页只读展示。

### 5.3 后端 schema 改动

- `backend/app/schemas/prompt.py`：`PromptAssetRead` / `PromptAssetUpdate` 加 `model_tier: Optional[str]`。
- `backend/app/schemas/model_config.py`：`LLMEndpointRead` / `LLMEndpointUpdate` 加 `tier: str`。
- 新 schema `backend/app/schemas/llm_routing.py`：`RouteMatrixRow`。

### 5.4 前端改动

- `frontend/src/pages/ModelConfig.tsx`：Endpoint 编辑/新建表单加 `tier` 下拉（5 选一），表格列加 Tier 列。
- `frontend/src/pages/Prompts.tsx`：PromptAsset 编辑表单加 `model_tier` 下拉（6 选一含「不限」）；endpoint 下拉框根据所选 `model_tier` 做过滤（「不限」时显示全部）。
- `frontend/src/pages/LlmRouting.tsx`（新）：只读矩阵表，列 = `task_type / model_tier / endpoint / model / temperature / max_tokens`，支持按 tier 过滤。顶部加跳转按钮到各 `/prompts?task_type=xxx` 快捷编辑。
- `frontend/src/App.tsx`（或 router 入口）：注册 `/llm-routing` 路由。
- `frontend/src/api/llmRouting.ts`（新）：`getRoutingMatrix()`。

## 6. 迁移与回滚

### 6.1 upgrade（`a1001400` revises `a1001300`）

1. `op.add_column("llm_endpoints", tier VARCHAR(20) NOT NULL DEFAULT 'standard')`。
2. `op.add_column("prompt_assets", model_tier VARCHAR(20) NULL)`。
3. `op.create_check_constraint` × 2（endpoints + prompts）。
4. `op.create_index` × 2。
5. 数据回填：
   - 对内建 `embedding` 相关 endpoint `UPDATE llm_endpoints SET tier = 'embedding' WHERE ...`（通过 name 或 model 关键词 `embedding|bge|e5` 匹配）。
   - INSERT 7 条新 `prompt_assets` 行（`ON CONFLICT (task_type, is_active) DO NOTHING`）。

### 6.2 downgrade

1. DELETE 7 条新 prompt（按 task_type 精确匹配 + `is_active = 1`）。
2. `op.drop_index` × 2。
3. `op.drop_constraint` × 2。
4. `op.drop_column` × 2。

### 6.3 非破坏性保证

- `model_tier IS NULL` 时路由逻辑与 v1.3.0 完全一致（代码走旧分支）。
- 7 个新 prompt 未挂 endpoint 时走 `prompt_registry.resolve_route` 的 fallback 表，不会 404。
- `rag_query_rewrite_enabled` 默认 False，开关控制。
- 前端新页面独立路由，不触碰现有页面逻辑。

## 7. Smoke 覆盖（扩 scripts/smoke_v1.sh）

新增 `[22/22] v1.4 llm-routing matrix & tier`：

1. `GET /api/health` = 200（确认 v1.4 迁移后 backend 仍起得来）。
2. `GET /api/llm-routing/matrix` = 200，返回 array 长度 ≥ 11（原有 task_type 数）。
3. `GET /api/prompts` 返回的每条 prompt 含 `model_tier` 字段（可为 null）。
4. `GET /api/llm-endpoints` 返回的每条 endpoint 含 `tier` 字段且 ∈ 5 枚举。
5. `POST /api/prompts` 创建一条 `task_type=test_tier_smoke, model_tier=small`，`GET` 再确认 `model_tier == "small"`，最后 `DELETE` 清理。
6. `GET /api/llm-routing/matrix?tier=flagship` 过滤后返回结果条数 ≥ 0 且所有 row 的 `model_tier` 或 `endpoint_tier` 至少一个是 `flagship`。

断言节奏沿用 v1.3：避开 `| head -1`，用 `awk '/pattern/ {print; exit}'`；host 层 pytest 用 `--noconftest -p no:cacheprovider + PYTHONPATH=backend`。

## 8. 验收标准（DoD）

- [ ] Alembic `a1001400` upgrade/downgrade 各跑一次不报错，head 回到 `a1001400` / `a1001300` 正确。
- [ ] `llm_endpoints.tier` / `prompt_assets.model_tier` 字段 NOT NULL / NULL 约束生效。
- [ ] 7 个新 task_type 的 prompt 默认 `model_tier` 已回填，且都在 `/api/prompts` 返回。
- [ ] `critic_service.run_critic` 默认仍以原 `CriticReport` 结构返回；开关 `CRITIC_SPLIT_ENABLED=true` 时走新两路。
- [ ] `settings_extractor.extract_settings_from_outline` 在三路都成功时合并 SettingsBundle；任一路失败时走原大 JSON 兜底。
- [ ] `/llm-routing` 矩阵页前端能打开、表格能渲染、按 tier 过滤正常。
- [ ] `/prompts` 编辑能保存 `model_tier`；endpoint 下拉按 tier 过滤正常。
- [ ] `scripts/smoke_v1.sh` `[22/22]` 全绿。
- [ ] `scripts/smoke_v1.sh` 老 21 步全部保持绿。
- [ ] 无前端 console 错误；无 backend 启动 warning 级别提升。

## 9. v1.5 预留

v1.4 不做但 v1.5 接手的项目：

- **章型字段**：`chapters.chapter_type VARCHAR(20) NULL`（`opening`/`body`/`climax`）。
- **generation 拆分**：`generation_opening` / `generation_body` / `generation_climax`。
- **前端章型路由表页**：按卷/章配不同 generation 档。
- **embedding 分档**：`embedding_summary` + 可选第二 Qdrant 集合 `chapter_summaries_small`。
- **章型自动检测**：规则优先 + LLM 标注兜底。

v1.4 完成后，v1.5 分支应从 `a1001400` 起 head。

## 10. 风险与缓解

| 风险 | 影响 | 缓解 |
|------|------|------|
| CHECK 约束在 Postgres 老版本上语法兼容性 | 迁移失败 | 使用 `sa.text` 显式写 `CHECK tier IN (...)`，PG 9.x+ 均支持 |
| 前端 `/llm-routing` 路由与已有路由冲突 | 404 或覆盖 | 检查 `App.tsx` 已有路由表，新路径用未占用串 |
| critic 分流后两路 token 费用上升 | 成本↑ | 由 `CRITIC_SPLIT_ENABLED` 开关控制，默认 True 但运维可 env 关 |
| settings_extractor 三路并发时外部 LLM rate limit | 抽取失败 | `asyncio.gather` 包 `return_exceptions=True`，任一失败走单 JSON 兜底 |
| 老 `model_tier IS NULL` 的 prompt 在矩阵页显示 "—" 可能让用户以为没配 | 运维误解 | 矩阵页顶部加说明文字「Tier 空 = 兜底到 endpoint_id 直绑」 |

## 11. Chunk 切分预估（供 writing-plans 参考）

- chunk-1: 本 spec 落盘（本 chunk）。
- chunk-2: implementation plan 落盘。
- chunk-3: Alembic `a1001400` + models 字段。
- chunk-4: 后端 schema + `/api/llm-endpoints` `/api/prompts` tier 读写。
- chunk-5: `prompt_registry` BUILTIN_PROMPTS + resolve fallback。
- chunk-6: `model_router` tier 路由 + matrix 接口。
- chunk-7: `critic_service` 两路拆分 + 开关。
- chunk-8: `consistency_llm_check` 触发点。
- chunk-9: `settings_extractor` 三拆 + 合成。
- chunk-10: `context_pack` rag_query_rewrite 钩子。
- chunk-11: 新 API `/api/llm-routing/matrix`。
- chunk-12: 前端 ModelConfig 页加 tier 列。
- chunk-13: 前端 Prompts 页加 model_tier + 过滤。
- chunk-14: 前端 LlmRouting 矩阵页。
- chunk-15: smoke `[22/22]` 步骤。
- chunk-16: 总收尾（README / CHANGELOG / release notes）。

共 16 chunk，节奏上比 v1.3.0 的 33 chunk 轻。实际 writing-plans 里可能还会细分 TDD 步骤。

---

**上游硬规则（v1.3.0 延续，开发期必须遵守）：**
- `apply_patch` 绝对路径 + `read_text` 先行 + ASCII `+/-`，每个 `@@` hunk ≥1 个 +/- 行。
- `search` 参数 `path` 单数（无 `max_results`）；`list_files` 无 `max_depth`。
- `wait_task` 只接 `task_id`。
- smoke 避开 `| head -1`，用 `awk exit`。
- GitHub Actions 里 `$` 用 env 块规避。
- nginx 不代理 `/metrics`。
- Prometheus PromQL 断言前 `sleep 18`。
- host pytest：`--noconftest -p no:cacheprovider` + `PYTHONPATH=backend`。
- 不改 Project/Volume/Chapter 默认字数；不改 `writing_guides.py:614`。
- 非破坏默认推进。
