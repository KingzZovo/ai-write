# Release Notes — v1.4.0 (LLM Tier Routing)

v1.4 引入 **tier-based LLM routing**：把“端点”与“模型能力等级（tier）”解耦，每个 prompt 可以指定自己的 `model_tier`，路由在运行时按 `prompt.model_tier ≫ endpoint.tier ≫ 'standard'` 的优先级解析出真正要调用的模型。配套在后端引入了 critic 分拆、consistency LLM 复核、settings extractor 三路分拆与 RAG query rewrite 四个可按 env 开关的能力，前端在 settings / prompts 页面加了 tier 可视化，并新建了 `/llm-routing` 路由矩阵页。所有变更向前兼容，默认行为 = v1.3。

## Tier 枚举

| Tier       | 定位                                              |
| ---------- | ------------------------------------------------- |
| flagship   | Claude / GPT-4 级，最强推理 / 长链路生成          |
| standard   | 默认中档（如 GPT-4o-mini / Claude Haiku）         |
| small      | 本地或轻量云模型，做摘要/抽取/分类                |
| distill    | LoRA / 蒸馏小模型，做润色 / 风格收敛              |
| embedding  | 向量化模型（Jina / BGE）                          |

路由优先级：`prompt.model_tier` ≫ `endpoint.tier` ≫ `'standard'`。

## Env flags

| 变量                              | 默认 | 作用                                                    |
| --------------------------------- | ---- | ------------------------------------------------------- |
| `CRITIC_SPLIT_ENABLED`            | 1    | critic 拆为 `critic_hard` + `critic_soft`，关时回落单 critic |
| `CRITIC_CONSISTENCY_LLM_ENABLED`  | 0    | critic_hard 命中 consistency 时触发 LLM 复核            |
| `SETTINGS_EXTRACTOR_SPLIT_ENABLED`| 1    | settings_extractor 拆为 characters / world_rules / relationships 三路 |
| `RAG_QUERY_REWRITE_ENABLED`       | 0    | context_pack L3 前置一次 query rewrite                  |

## 变更摘要（chunk-1 … chunk-14）

### 后端

- **chunk-1/2 — spec + implementation plan**：`docs/V10_DESIGN.md` + `docs/V10_CHUNKS.md` 落地 v1.4 tier routing 方案。
- **chunk-3 — alembic `a1001400`**：给 `llm_endpoints` 加 `tier TEXT`，给 `prompt_assets` 加 `model_tier TEXT`，两列都为 nullable，保持老数据兼容。
- **chunk-4 — API tier read/write**：`/api/model-config/*` 和 `/api/prompts/*` 读写 `tier` / `model_tier`，ORM 同步补字段。
- **chunk-5 — `prompt_registry` 内建 7 个 task_type**：提供默认 prompt + tier 的 fallback，新增 `resolve_tier(prompt, endpoint)` 统一解析。
- **chunk-6 — `model_router` tier registry + `GET /api/llm-routing/matrix`**：返回 `{ rows: MatrixRow[], total, tier, error? }`，支持 `?tier=<enum>` 过滤，非法 tier 返回 `error` 字段但不 5xx。
- **chunk-7 — `critic_service` 拆分**：`critic_hard`（一致性 / 连续性 / OOC）+ `critic_soft`（节奏 / 读者拉力 / anti-AI），env 关时自动回落到单 critic。
- **chunk-8 — `consistency_llm_check`**：critic_hard 命中 consistency 时按 env 触发 LLM 复核，合并分数。
- **chunk-9 — `settings_extractor` 3-way split**：characters / world_rules / relationships 各自独立 tier，失败时回落到单 extractor。
- **chunk-10 — `context_pack` RAG query rewrite hook**：env 关时行为与 v1.3 完全一致。
- **chunk-11 — `scripts/smoke_v1.sh [22/22]`**：8 条断言覆盖 alembic head = `a1001400`、tier 字段暴露、prompt_registry builtin、`/api/llm-routing/matrix` 基本 + 过滤 + 非法 tier 等。

### 前端

- **chunk-12 — settings ModelConfig tier 下拉 + tier 徽章**：`TIER_OPTIONS` / `TIER_BADGE_CLASS` 公共配色落位在 settings 页。
- **chunk-13 — prompts `model_tier` 列 + tier 徽章 + endpoint 过滤**：prompts 表增加 `model_tier` 下拉和 tier 徽章，顶部新增按端点过滤。
- **chunk-14 — `/llm-routing` 路由矩阵页**：按 `task_type × mode` 分组展示每个 prompt 的 endpoint(tier) / model / effective_tier，overridden 时在 effective_tier 旁标 `*`；顶部支持 tier 过滤，右侧显示总数 / 覆盖数 / 各 tier 计数；NavBar 和 i18n（zh/en）同步加 `nav.llmRouting`（“路由” / “Routing”）入口。

### API

- **新增 `GET /api/llm-routing/matrix`**
  - 可选 query：`tier=flagship|standard|small|distill|embedding`
  - 响应：
    ```json
    {
      "rows": [
        {
          "task_type": "outline_book",
          "mode": "structured",
          "prompt_id": "...",
          "prompt_name": "全书大纲",
          "endpoint_id": "...",
          "endpoint_name": "Claude Sonnet",
          "endpoint_tier": "flagship",
          "model_name": "claude-sonnet-4",
          "model_tier": null,
          "effective_tier": "flagship",
          "overridden": false
        }
      ],
      "total": 14,
      "tier": null
    }
    ```

### Schema / 迁移

- 新 alembic head：`a1001400`
- 新增列：`llm_endpoints.tier TEXT`、`prompt_assets.model_tier TEXT`
- 升级命令：`docker compose exec backend alembic upgrade head`

### 兼容性

- 所有 env flag 关闭时，v1.4 行为与 v1.3 等价。
- 旧端点 / 旧 prompt 的 `tier` / `model_tier` 允许为 NULL，routing 会回落到 `'standard'`。
- 前端 `/llm-routing` 是新增路由，未开启也不影响老页面。

## smoke 矩阵

- `scripts/smoke_v1.sh [22/22]` 在 runtime 全栈下完整通过；`SMOKE_STATIC_ONLY=1` 子集同样绿。
- 运行时重点断言：
  - `alembic current = a1001400`
  - `/api/model-config` 返回含 `tier`
  - `/api/prompts` 返回含 `model_tier`
  - `prompt_registry` builtin 7 个 task_type 到位
  - `GET /api/llm-routing/matrix` 返回 `total`
  - `GET /api/llm-routing/matrix?tier=standard` 返回 `tier="standard"`
  - `GET /api/llm-routing/matrix?tier=bogus` 返回 `error` 字段（非 5xx）

## 相关文件

- 规范 / 计划：`docs/V10_DESIGN.md`, `docs/V10_CHUNKS.md`
- 后端：`backend/app/services/model_router.py`, `backend/app/services/prompt_registry.py`, `backend/app/services/critic_service.py`, `backend/app/services/settings_extractor.py`, `backend/app/services/context_pack.py`, `backend/app/api/llm_routing.py`
- 前端：`frontend/src/app/settings/page.tsx`, `frontend/src/app/prompts/page.tsx`, `frontend/src/app/llm-routing/page.tsx`, `frontend/src/components/Navbar.tsx`, `frontend/src/lib/i18n/messages.ts`
- smoke：`scripts/smoke_v1.sh`（[22/22] 段）
