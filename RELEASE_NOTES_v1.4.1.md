# v1.4.1 — v1.4 收尾 + NVIDIA embeddings + 测试可见性

**发布日期**：2026-04-24  
**基准**：v1.4.0  
**数据库头**：`a1001400`（**无新迁移**）  
**Git tag**：无（同 v1.4.0 约定，仅在 CHANGELOG / RELEASE_NOTES 层面记录）

## TL;DR

- **Chunk 17**：`/api/llm-routing/matrix` 按前端 `MatrixRow` 完全对齐（补齐 `prompt_name` / `endpoint_tier` / `model_tier` / `effective_tier` / `overridden` 等字段），三级 tier 回落由共享 `compute_effective_tier()` 实现；新增 18 个纯函数 pytest（全绿）。
- **Chunk 18**：端点测试不再只返回延迟。后端发送字面量 `"hi"` 并抓取模型实际回复；embedding 端点返回 `dim` + 前 3 位浮点。前端 `settings/page.tsx` 添加独立的 **测试过程** 面板（`data-testid="endpoint-test-panel"`）。
- **Chunk 19**：NVIDIA 平台 embeddings 接入为一类独立 `provider_type="nvidia"`；`NvidiaEmbeddingProvider` 直接调用 `https://integrate.api.nvidia.com/v1/embeddings`，具备 NVIDIA 专有的 `modality` / `input_type` / `encoding_format` / `truncate` 字段。

## 1. `/llm-routing/matrix` 字段对齐（chunk-17）

之前后端仅返回 `{task_type, endpoint_id, endpoint_name, model, tier, temperature, max_tokens}`，而 `frontend/src/app/llm-routing/page.tsx` 的 `MatrixRow` 期待更宽的字段集；有 “空单元格” 显示问题。v1.4.1 收尾修正：

- 后端改为驱动 DB 查询：对每个 `is_active=1` 的 `PromptAsset` 左联 `LLMEndpoint`，逐行用 `compute_effective_tier(prompt.model_tier, endpoint.tier)` 计算有效 tier；`overridden = prompt 有效 tier 且 ≠ endpoint tier`。
- 共享定义落在 `backend/app/services/model_router.py`：
  ```python
  VALID_TIERS = frozenset({"flagship","standard","small","distill","embedding"})
  def is_valid_tier(t): return bool(t) and t in VALID_TIERS
  def compute_effective_tier(prompt_tier, endpoint_tier):
      # prompt ≫ endpoint ≫ "standard"
      ...
  ```
- 新增 `backend/tests/services/test_model_router_tier.py`（18 passed in 0.04s）涵盖：
  - 五个 canonical tier 均被视为合法；`None` / `""` / `"Flagship"` / `"bogus"` 一律被拒；
  - prompt tier 胜出 endpoint tier；非法 prompt tier 回落到 endpoint tier；
  - 两级均缺位时结果 = `"standard"`；embedding tier 不被隐式降级。

非法 tier 仍然走 200 + `error: "invalid tier"`（未变）。

## 2. 端点测试可见性（chunk-18）

### 用户可见行为

在 设置 → API 端点 → 测试 之后，端点卡下新增一个面板：

- **发送**：字面量 `hi`
- **请求**：`POST anthropic.messages model=... max_tokens=32 content='hi'` （或 openai `/chat/completions`／openai `/embeddings`／nvidia `/embeddings`）
- **响应**：
  - chat 模型：模型实际回复文本（`response_preview` 前 400 字）
  - embedding 模型：`dim=<N> head=[f1, f2, f3]`
- **错误**（如有）：红色文本展示 `Connection failed: …`

### 字段变化

`TestResult` pydantic 模型新增（均为 Optional，旧端自动降级兼容）：

| 新字段 | 类型 | 含义 |
|--------|------|------|
| `sent_text` | `str \| null` | 实际发送到端点的字符串（chat 为 `"hi"`） |
| `request_summary` | `str \| null` | 一行可读请求摘要 |
| `response_text` | `str \| null` | chat 模型完整回复 |
| `response_preview` | `str \| null` | 前 400 字的安全预览 |
| `embedding_dim` | `int \| null` | embedding 模型的向量维度 |
| `response_first_floats` | `number[] \| null` | embedding 前 3 位浮点 |

同时将旧的探测文本 `"Say hi"` 统一替换为字面量 `"hi"`（满足 v1.4.1 需求）。

## 3. NVIDIA embeddings 兼容（chunk-19）

### 背景

NVIDIA 在 `https://integrate.api.nvidia.com/v1/embeddings` 上提供一套 OpenAI 超集格式的 embeddings API，多出四个 OpenAI SDK 不会发的字段：

```bash
curl -X POST https://integrate.api.nvidia.com/v1/embeddings \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $NVIDIA_API_KEY" \
  -d '{
    "input": ["What is the civil caseload in South Dakota courts?"],
    "model": "nvidia/llama-nemotron-embed-vl-1b-v2",
    "modality": ["text"],
    "input_type": "query",
    "encoding_format": "float",
    "truncate": "NONE"
  }'
```

### 实现

- **后端**：`backend/app/services/model_router.py::NvidiaEmbeddingProvider`。放弃 openai SDK，改用 `httpx.AsyncClient` 直接 POST。
  - `embed_one(text, input_type="query")` 、 `embed_many(texts, input_type="passage")` 、`embed(text)` 兼容 `EmbeddingProvider` 接口。
  - 请求体固定为数组输入（与 OpenAI 不同，NVIDIA 不接受纯字符串 `input`）；`modality` / `encoding_format` / `truncate` 可通过构造参数覆盖。
  - HTTP 非 2xx 時抛出包含服务端文本前 400 字的 `RuntimeError`，`test_endpoint` 再包装为 `TestResult.message`，方便 UI 排故障。
- **provider 白名单**：`backend/app/api/model_config.py::VALID_PROVIDER_TYPES` 新增 `"nvidia"`。`EndpointCreate.provider_type` 的 description 更新。
- **test 分支**：`test_endpoint` 增加 `provider_type == "nvidia"` 分支，调用 `NvidiaEmbeddingProvider.embed_one("hi", input_type="query")`，输出 `embedding_dim` + `response_first_floats` + `request_summary`。
- **前端**：`PROVIDER_OPTIONS` 新增 `{ value: 'nvidia', label: 'NVIDIA Embeddings' }`；`MODEL_SUGGESTIONS.nvidia = ['nvidia/llama-nemotron-embed-vl-1b-v2', 'nvidia/nv-embedqa-e5-v5']`；base_url 表单对 `nvidia` 同样必填，占位符为 `https://integrate.api.nvidia.com/v1`。

### 使用示例

在 设置 页添加一个端点：

- 名称：`NVIDIA 向量`
- 供应商类型：`NVIDIA Embeddings`
- 模型等级：`embedding`
- 基础地址：`https://integrate.api.nvidia.com/v1`
- API 密钥：`$NVIDIA_API_KEY`
- 默认模型：`nvidia/llama-nemotron-embed-vl-1b-v2`

保存后点测试，应看到 `发送: hi` 与 `向量: dim=<N> head=[...]`。

## 4. Smoke 矩阵

`scripts/smoke_v1.sh` 添加 3 个新 section：

- `[38/38] v1.4 routing matrix full fields + tier helpers (chunk-17)`
- `[39/39] v1.4 endpoint test visibility (chunk-18)`
- `[40/40] v1.4 NVIDIA embeddings provider (chunk-19)`

Static 断言全部 grep 式，不依赖 docker；runtime 分支增加了 pytest `test_model_router_tier.py`、`/api/llm-routing/matrix` 行级字段校验、以及使用虚构 key 创建 nvidia 端点测试 `POST /endpoints` 是否不再拒绝 `provider_type=nvidia`。

## 5. 相关文件

- 后端：`backend/app/services/model_router.py`，`backend/app/api/llm_routing.py`，`backend/app/api/model_config.py`
- 测试：`backend/tests/services/test_model_router_tier.py`
- 前端：`frontend/src/app/settings/page.tsx`
- smoke：`scripts/smoke_v1.sh`（`[38/38]` / `[39/39]` / `[40/40]`）
- 文档：`CHANGELOG.md`（`[1.4.1]` 段），本文

## 6. 演进路径提示

- 若未来需将 NVIDIA 接入扩展到 chat（reranker 或 LLM），可在 `NvidiaEmbeddingProvider` 旁实现 `NvidiaChatProvider` 并扩展 `ModelRouter.register_provider("nvidia_chat", …)`；目前 `provider_type == "nvidia"` 仅被视为 embedding 类别，`test_endpoint` 也仅走 embed 分支。
- 若下一个 patch 需要验证 `response_preview` UI 截断边界，建议构造一个返回长文本的 fake endpoint，再把 400 字截断推到 e2e。
- 若后续 `v1.5` 有 vector store 关联，可构造 `KnowledgeBaseTier` 将 tier `embedding` 导入 ingest pipeline 的 endpoint 选择策略。

## 7. 大纲生成稳定性升级（对写书流程的实际影响）

这一轮收尾同时把 “写书 → 大纲 → 章节” 的链路里最脆弱的一环（大纲长输出）给加固了。
以下条目每条一句话，说明对用户前台流程的可感知改变：

- **端点测试探针**：以后打“测试”不会再对着空白感叹，空回复 / 推理模型也会显示 `finish_reason` /
  `usage` / `reasoning_tokens` 以及 “看起来是个推理模型，试试加大 max_tokens” 的提示。
- **Prompt max_tokens 提高**：普通任务 4096 → 8192，大纲任务（全书 / 卷 / 章）统一再拉高到 16384；
  直接结果：长篇大纲不再被截断在第几段。
- **全书大纲分段生成**（v1.4.1 已落地，此处补记）：全书大纲默认分三段生成（骨架 → 角色 + 世界观并行），
  每段单次输出控制在模型健康区间内；直接结果：长篇不再中途烂尾，输出质量在后十分之九不会断崖下跌。
- **大纲进度可见**（任务 B）：在工作台 “生成全书大纲” 后，预览区上方新增三个进度圈 “骨架 / 角色 / 世界观”；
  灯转蓝表示正在流式生成，转绿表示该段完成。后两段可同时亮（并发），文字内容按到达顺序交替填入预览；生成结束后
  预览会自动换成按九段拼接的完整大纲。不再监视 “进度不动是不是卡死了”。
- **卷大纲分段生成**（任务 C）：卷大纲现在先出卷元信息（转折点 / 新角色 / 伏笔 / 本卷情感 / chapter_count等），
  再每 10 章一批生成章节摘要，加载上一批末尾 3 章作上下文；直接结果：30+ 章的长卷不再 “摘要列表输到第 20 章就跳号 / 断尾”，
  chapter_idx 由代码强制连续，即使模型偶尔抄错也会被纠正。
- **端上体验 “大纲按钮 → 文字流 → 确认” 不变**：所有上述改动是后端 / 底层行为层面，确认大纲 / 保存 outline_id 的
  工作流未改；默认开启 “分段进度”，回退到老纯文本流只需在请求 body 或查询参数里传 `staged_stream=0`。
