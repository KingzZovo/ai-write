# AI Write v1.4 LLM 路由实施计划

**日期：** 2026-04-23
**对应 spec：** docs/superpowers/specs/2026-04-23-llm-routing-v1.4-design.md
**基线 HEAD：** 9a7eabc (后 chunk-1 spec)
**基线 Alembic head：** a1001300
**目标 Alembic head：** a1001400
**预计 chunk 数：** 16（chunk-1..16，各含独立 commit）

**REQUIRED SUB-SKILL:** superpowers:subagent-driven-development OR superpowers:executing-plans

**任务执行模式：** Inline Execution（本 repo 跟 feature/v1.0-big-bang 直推，不用单独 worktree；King 已明示授权，硬规则 > superpowers:using-git-worktrees）。

---

## 全局执行约定

1. 每个 chunk = 一次 `git_commit` + 一行汇报给 King。
2. 连续两个 chunk 之间不连网 pull，非破坏默认推进。
3. 每个 chunk 自检：`git_status` 干净 + `/api/health` 200（如 chunk 涉及 backend）。
4. 严遵硬规则：`apply_patch` 绝对路径 + 先 `read_text` + ASCII +/-；`search` 用 `path` 单数；host pytest `--noconftest -p no:cacheprovider + PYTHONPATH=backend`；smoke `awk exit` 代 `| head -1`。
5. 回滚街口：任何 chunk 走不下去时，`git reset --soft HEAD~N` 回到未推到的 chunk，Alembic `downgrade -1` 到 `a1001300`。

---

## Task 1 — Alembic a1001400（chunk-3）

**Files / exact paths：**
- 新增：`/root/ai-write/backend/alembic/versions/a1001400_v14_llm_tier.py`
- 修改：`/root/ai-write/backend/app/models/prompt.py`（PromptAsset 加 `model_tier` 列）
- 修改：`/root/ai-write/backend/app/models/llm_endpoint.py`（Endpoint 加 `tier` 列）

**实施步骤：**

- [ ] 读 `backend/app/models/prompt.py` 确认 PromptAsset 表名 = `prompt_assets`、已有 `task_type / is_active` 列。
- [ ] 读 `backend/app/models/llm_endpoint.py` 确认表名 = `llm_endpoints`。
- [ ] 读 `a0504000` 学习同类 migration 写法（已在 chunk-1 探查阶段读过）。
- [ ] `write_file` 创建 `a1001400_v14_llm_tier.py`：
    - header：`revision = "a1001400"; down_revision = "a1001300"`
    - upgrade：
        - `op.add_column("llm_endpoints", sa.Column("tier", sa.String(20), nullable=False, server_default="standard"))`
        - `op.add_column("prompt_assets", sa.Column("model_tier", sa.String(20), nullable=True))`
        - `op.create_check_constraint("ck_llm_endpoints_tier", "llm_endpoints", "tier IN ('flagship','standard','small','distill','embedding')")`
        - `op.create_check_constraint("ck_prompt_assets_model_tier", "prompt_assets", "model_tier IS NULL OR model_tier IN ('flagship','standard','small','distill','embedding')")`
        - `op.create_index("ix_llm_endpoints_tier", "llm_endpoints", ["tier"])`
        - `op.create_index("ix_prompt_assets_model_tier", "prompt_assets", ["model_tier"])`
        - 数据回填：`UPDATE llm_endpoints SET tier='embedding' WHERE LOWER(name) LIKE '%embed%' OR LOWER(COALESCE(default_model,'')) LIKE ANY(ARRAY['%embed%','%bge%','%e5%'])`
        - INSERT 7 条新 prompt_assets 行（task_type 列表见 spec 5.1，template/name 占位符文本，`on_conflict_do_nothing`）
    - downgrade：DELETE 7 条 + drop_index ×2 + drop_constraint ×2 + drop_column ×2。
    - 全部用 `inspect(conn)` idempotent guard。
- [ ] `apply_patch` 在 `backend/app/models/llm_endpoint.py` Endpoint 类加 `tier: Mapped[str] = mapped_column(String(20), default="standard", nullable=False)`。
- [ ] `apply_patch` 在 `backend/app/models/prompt.py` PromptAsset 类加 `model_tier: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)`。
- [ ] `run_command`：`cd /root/ai-write/backend && alembic upgrade head` → 验证 head=a1001400。
- [ ] `run_command`：`alembic downgrade a1001300 && alembic upgrade head` → 验证可来回。
- [ ] `run_command`：`cd /root/ai-write/backend && PYTHONPATH=. pytest --noconftest -p no:cacheprovider app/tests/models/test_prompt.py app/tests/models/test_llm_endpoint.py -x`（若有）。
- [ ] `git_commit` stage_all=true，message = `chunk-3(v1.4): alembic a1001400 adds llm_endpoints.tier + prompt_assets.model_tier`

**验证：**
- `curl -s http://localhost:8080/api/health | jq .status` = `"ok"`
- `psql -c "\d llm_endpoints" | grep tier`
- `psql -c "\d prompt_assets" | grep model_tier`
- `psql -c "SELECT task_type FROM prompt_assets WHERE task_type IN ('critic_hard','critic_soft','consistency_llm_check','rag_query_rewrite','characters_extraction','world_rules_extraction','relationships_extraction')"` → 7 rows

---

## Task 2 — Schema + API tier 读写（chunk-4）

**Files：**
- `/root/ai-write/backend/app/schemas/prompt.py`
- `/root/ai-write/backend/app/schemas/model_config.py`（或 llm_endpoint.py）
- `/root/ai-write/backend/app/api/prompts.py`
- `/root/ai-write/backend/app/api/llm_endpoints.py`（或 model_config.py）

**实施步骤：**

- [ ] `search regex="class PromptAssetRead|class PromptAssetUpdate" path=/root/ai-write/backend/app/schemas` 定位。
- [ ] `apply_patch` `schemas/prompt.py`：
    - `PromptAssetRead` 加 `model_tier: Optional[str] = None`
    - `PromptAssetUpdate` 加 `model_tier: Optional[str] = Field(None, pattern="^(flagship|standard|small|distill|embedding)$")`
    - `PromptAssetCreate` 同步
- [ ] `apply_patch` `schemas/model_config.py` (或对应 llm_endpoint schema 文件)：
    - `LLMEndpointRead` 加 `tier: str = "standard"`
    - `LLMEndpointUpdate` / `Create` 加 `tier: Optional[str] = Field("standard", pattern=...)`
- [ ] `apply_patch` `api/prompts.py`：PATCH/POST 处理函数接入 `model_tier` 字段（直接赋值即可，sqlalchemy ORM 已支持）。
- [ ] `apply_patch` `api/llm_endpoints.py`：同上，加上 `tier` 字段。
- [ ] `run_command`：smoke 片段（局部）：
  ```
  curl -s -u king:Wt991125 http://localhost:8080/api/llm-endpoints | jq '.[0].tier'
  curl -s -u king:Wt991125 http://localhost:8080/api/prompts | jq '.[0] | has("model_tier")'
  ```
- [ ] `git_commit`：`chunk-4(v1.4): schema + API tier read/write for endpoints & prompts`

---

## Task 3..13（chunk-5..14，下窗口落盘）

**下面每 Task 一个 chunk。本 plan 先给 outline，实施细节留给下窗口在 executing-plans 阶段 expand。**

### Task 3 — chunk-5 `prompt_registry` BUILTIN_PROMPTS 扩 + resolve fallback
- `backend/app/services/prompt_registry.py`：`BUILTIN_PROMPTS` 加 7 条（template、name、name_en、description、category、model_tier）。
- `resolve_route(task_type)` 新增 fallback 字典：
  - `critic_hard|critic_soft|consistency_llm_check` → fallback `critic`
  - `characters_extraction|world_rules_extraction|relationships_extraction|rag_query_rewrite` → fallback `extraction`
- 新 helper `resolve_tier(task_type) -> Optional[str]`。
- 单测：`tests/services/test_prompt_registry.py` 加 4 个用例（有 prompt、无 prompt fallback、tier 读出、无 tier）。

### Task 4 — chunk-6 `model_router` tier 路由 + `/api/llm-routing/matrix`
- `backend/app/services/model_router.py`：新 `_pick_endpoint_by_tier`，在 `_get_route` 前调。
- 新 `list_routes_matrix()` 返回 `List[RouteMatrixRow]`。
- `backend/app/api/llm_routing.py`（新）：`GET /api/llm-routing/matrix?tier=...`。
- `backend/app/main.py`：注册 router。
- 单测：tier 匹配优先级、过滤详谈。

### Task 5 — chunk-7 `critic_service` 拆分 `critic_hard` + `critic_soft`
- `backend/app/services/critic_service.py`：LLM 层改 `asyncio.gather(run_structured_prompt("critic_hard"), run_structured_prompt("critic_soft"))`。
- 合并两份 JSON 到原 `CriticReport`。
- 配置开关 `CRITIC_SPLIT_ENABLED` (env, 默认 True)，False 走旧路径。
- 单测：两路合并、一路失败兜底到单 critic 。

### Task 6 — chunk-8 `consistency_llm_check` 触发点
- `critic_service.py`：critic_hard 命中 `consistency` 标签 → 再调 `run_structured_prompt("consistency_llm_check")` 深度判官。
- 开关 `CRITIC_CONSISTENCY_LLM_ENABLED` (env, 默认 False)。

### Task 7 — chunk-9 `settings_extractor` 拆分三路
- `backend/app/services/settings_extractor.py`：`extract_settings_from_outline` 改 `asyncio.gather`。
- `SettingsBundle(characters=..., world_rules=..., relationships=...)` 合成。
- 任一子任务失败 → `return_exceptions=True` → 降级单 JSON 调用。
- 单测：三路都走、子任务失败降级。

### Task 8 — chunk-10 `context_pack` rag_query_rewrite 钩子
- `backend/app/services/context_pack.py`：`_build_rag(query)` 前插 `_maybe_rewrite_query(query)`。
- `backend/app/core/config.py`：`RAG_QUERY_REWRITE_ENABLED: bool = False`。
- 单测：开关开关关关两种应举。

### Task 9 — chunk-11 合入 matrix API smoke
- 上游 chunk-6 开的 API 加 smoke 步骤。只动 smoke 脚本，不改代码。

### Task 10 — chunk-12 前端 `ModelConfig.tsx` 加 `tier` 下拉与列
- `frontend/src/pages/ModelConfig.tsx`（或对应的 Endpoint 管理页，需先 `search` 定位）。
- `frontend/src/api/llmEndpoints.ts`：`Endpoint` 类型加 `tier`。
- 表格新增 Tier 列；编辑表单 select（5 枚举）。
- 手工验证：http://localhost:5173/model-config 能打开、保存不报 400。

### Task 11 — chunk-13 前端 `Prompts.tsx` 加 `model_tier` + 过滤
- `frontend/src/pages/Prompts.tsx`：编辑 drawer 加 `model_tier` select（6 选含「不限」）。
- endpoint 下拉框根据所选 `model_tier` 过滤。
- 表格显示 Tier 列。

### Task 12 — chunk-14 前端 `LlmRouting.tsx` 新矩阵页
- `frontend/src/pages/LlmRouting.tsx`（新）。
- `frontend/src/api/llmRouting.ts`（新）：`getRoutingMatrix(tier?)`。
- `frontend/src/App.tsx`：新路由 `/llm-routing`。
- 导航章（如有 Sidebar）加入口。

### Task 13 — chunk-15 smoke `[22/22]` + release notes
- `scripts/smoke_v1.sh`：加 `[22/22] v1.4 llm-routing matrix & tier` 章，五个断言（见 spec §7）。
- `docs/RELEASE_NOTES_v1.4.md`（新）。
- `README.md` v1.4 项。

### Task 14 — chunk-16 CHANGELOG + tag 预热
- `CHANGELOG.md`：add v1.4.0 章。
- 不结 tag。Tag/PR 由 King 决定时机。

---

## 收尾

- [ ] 所有 16 个 chunk 单独 commit。
- [ ] `smoke_v1.sh` 全绿 22/22。
- [ ] `alembic current` = `a1001400`。
- [ ] `alembic downgrade a1001300 && alembic upgrade head` 可来回。
- [ ] `/llm-routing` 页面人工补充截图验证。
- [ ] 按 superpowers:finishing-a-development-branch 的 options 给 King 选合并方式。

---

## 升级/回退触发条件

- chunk-3 如 alembic upgrade 失败：检查 `llm_endpoints` 是否有 name 列 → 修改数据回填的匹配表达式后重跑。
- chunk-6 `_pick_endpoint_by_tier` 如返回空：降级走原 `_get_route`，不抑制任务。
- chunk-12 / 13 / 14 前端页面定位不到：`list_files frontend/src/pages` + `search 'ModelConfig|Prompts|endpoint' path=frontend/src` 再补。
- MCP 断连：重试 3 次，还不行时 → `system.wait` 60s 再来。

---

## 附：下窗口接力点

本窗口在 chunk-1 (spec) + chunk-2 (plan) 完成后停。下窗口从 chunk-3 开始，顺序推 chunk-4..16。
