# embedding 上游选型(2026-11-17)

目标:中段章的连续链原先被"上游 embedding 挂了"卡住,换一个能用的模型把链路跑通。

## 1. 本机有哪些凭据(先看能不能换家)

| 来源 | 结果 |
|---|---|
| NVIDIA `nvapi-…`(从运行中的 `ds2api-balance` 容器 env 读出) | **唯一可用的 embedding 凭据** |
| 其它容器(`new-api` / `grok2api-*` / `claude2api-*` / `warp*`) | 只有 `GPG_KEY` / `WARP_LICENSE_KEY`,没有第三方 API key |
| 本机网关 `http://127.0.0.1:8317/v1/embeddings` | **404**(只代理 chat,不代理 embeddings) |
| cliproxyapi 配置 `/root/migration-deploy/cliproxyapi/config.yaml` | 只有 `api-keys` / `claude` / `codex` 段,没有 embedding 上游 |
| Jina / Cohere / Voyage / SiliconFlow / Cloudflare 等免费额度 | **本机没有任何对应 key**,无法在"不注册新账号"的前提下测试 |

结论:**这一轮只能在 NVIDIA 一家里面换模型**。想真的多试服务商,需要先拿到别家的 key;
我把这一步的候选和判据写清楚了(见 §4),拿到 key 即可直接套用同一套探针。

## 2. NVIDIA 上到底有几个 embedding 模型

`GET /v1/models` 返回 33 个模型,其中 8 个是 embedding。
按**应用真实的 payload** 逐个测(应用固定发 `input` 数组 + `modality` 逐条 + `input_type` +
`encoding_format` + `truncate`,见 `model_router.nvidia_embedding_payload`):

| 模型 | 维度 | 单条(passage) | 单条(query) | batch × 32 | 结论 |
|---|---|---|---|---|---|
| `nvidia/nemotron-3.5-embed-1b`(原默认) | 2048 | **HTTP 500** | HTTP 500 | — | **不可用**(`service inference failed`) |
| `nvidia/nemotron-3.5-embed-8b` | 4096 | 0.36s | 14.26s | **HTTP 500** | 不可用(且维度不符) |
| **`nvidia/nemotron-3-embed-1b`** | **2048** | **0.13s** | **0.16s** | **0.33s** | **采用** |
| `nvidia/llama-3.2-nv-embedqa-1b-v1` | — | 404 | 404 | — | 该账号无权限 |
| `nvidia/nv-embedqa-mistral-7b-v2` | — | 404 | — | — | 无权限 |
| `nvidia/embed-qa-4` | — | 404 | — | — | 无权限 |
| `snowflake/arctic-embed-l` | — | 404 | — | — | 无权限 |
| `nvidia/llama-3.2-nemoretriever-1b-vlm-embed-v1` | — | 404 | — | — | 无权限 |

两个实测细节值得记下来:

- `nemotron-3.5-embed-*` 是**非对称**模型,不带 `input_type` 会 400
  (`'input_type' parameter is required for asymmetric models`)—— 我第一次手搓请求拿到的 400
  就是这个,不是网络问题。
- 原默认模型带 `input_type` 后才是真正的 500:**上游服务本身坏了**,不是调用姿势不对。

## 3. 换用与影响

- 代码:`scripts/quality_eval/continuity_probe.py` 的 `DEFAULT_EMBED_MODEL` 默认值改为
  `nvidia/nemotron-3-embed-1b`(仍可用 `QUALITY_EVAL_EMBED_MODEL` 覆盖)。
- 数据:评估库 `aiwrite_eval.prompt_assets` 里 127 条 `task_type='embedding'` 路由的
  `model_name` 同步为新模型(**只动评估库,不碰用户库**)。
- **维度不变(2048)**,所以现有 Qdrant 集合不需要重建。
  但要说清:旧向量是那个已挂模型产出的,而集合名里带 **space 指纹**(provider/model/base_url),
  换模型等于换空间 —— 实测当前空间的认知集合是 **0 个点**,维护任务还会把旧空间的点删掉。
  分块索引已用自带入口重建(60 个点);**认知**索引本轮补了 `backfill_project_cognition` 并跑过(当前空间 0 → 142 点,召回返回 64 个键)。
  详见 [embedding-switch-index-rebuild-20261117.md](/root/ai-write/docs/evaluation/embedding-switch-index-rebuild-20261117.md)。

效果:同一份 24 章项目、同一条命令,ch18 生成从
`RequiredContextUnavailable: character_cognition_retrieval_unavailable` 变成
`status: ok`,`nvidia_embed` 报错 **0 次**。

## 4. 如果要继续多试服务商(下一步判据)

拿到 key 后逐个跑同一套探针即可,判据只要三条:

1. 接受**数组输入**(`input: [str, ...]`),因为认知索引是 32 条一批;
2. 返回 OpenAI 形状(`data[i].embedding`),且**维度固定**;
3. batch × 32 的延迟在秒级,且**连续 3 次不报错**。

优先候选(都有免费额度,均需自备 key):Jina `jina-embeddings-v3`、Cohere `embed-multilingual-v3`、
Voyage `voyage-3`、SiliconFlow `BAAI/bge-m3`、Cloudflare Workers AI `@cf/baai/bge-m3`、
Google `text-embedding-004`(Gemini API key)。其中 bge-m3 一类多语模型对中文长文更稳,
值得作为"与 NVIDIA 互备"的首选。