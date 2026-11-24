# embedding 模型 EOL 事故与恢复(2026-11-24)

## 1. 现象

- 11-23 之后每一轮 pipeline 都在 **80 秒内整批 failed**:`completed 33 / failed 67`。
- 67 章的错误文案完全一致:
  `正文生成:人物认知检索暂不可用,请稍后重试或检查嵌入与检索服务配置。`
- 认知台账本身是好的(`chapter_cognition_extractions` 无 pending),所以**不是**认知记录缺失。

## 2. 根因:上游 embedding 模型下线

```bash
curl -X POST https://integrate.api.nvidia.com/v1/embeddings \
  -H "Authorization: Bearer $KEY" -d '{"input":["hello"],"model":"nvidia/nemotron-3-embed-1b"}'

# {"status":410,"detail":"The model 'nvidia/nemotron-3-embed-1b' has reached its
#  end of life on 2026-11-23T09:00:00Z and is no longer available."}
```

时间线严丝合缝:11-23 01:27 认知链还正常 → **11-23 09:00 模型 EOL** → 此后所有
认知检索调用 410。而 `cognition_retrieval.resolve_prompt_selection` 的兜底是

```python
except Exception as exc:
    raise RequiredContextUnavailable('character_cognition_retrieval_unavailable') from exc
```

于是**任何**异常(410、超时、连接失败)都收敛成同一句文案,逐个错误码无法区分 ——
这正是 `AGENTS.md` 第 8 节点名的"兜底文案对应多个错误码"的又一例。

## 3. 第二个阻塞:ch10 有正文但没有认知记录

修好 embedding 后单独跑认知回填,仍然全 `character_cognition_not_ready`。
逐章对照发现 **ch10(global_idx=10)有正文 5156 字,却一条认知记录都没有**:

```sql
select ch.chapter_idx, coalesce(e.status,'(none)')
from chapters ch join volumes v on v.id=ch.volume_id
left join chapter_cognition_extractions e on e.chapter_id=ch.id
where v.project_id='<PID>' and ch.chapter_idx=10;
-- 10 | (none)
```

认知边界是**全有或全无**:写第 N 章要求"边界之前所有有正文的章"认知 `completed`,
ch10 一条不合就把 ch11 起的整卷堵死并级联。

## 4. 修复(全部走项目自己的入口)

1. **换 embedding 模型 —— 命令走 API,不直连改库**:

   ```bash
   PUT /api/model-config/endpoints/6ba128a2-3237-4e8e-ac81-0cf1cf8a7747
       {"default_model": "nvidia/nemotron-3.5-embed-1b"}
   PUT /api/prompts/c3741620-933d-4e0b-82f3-e72a847c0481?confirm_mismatch=true
       {"model_name": "nvidia/nemotron-3.5-embed-1b"}
   ```

   选它的理由是**同维度**:旧 `nemotron-3-embed-1b` 与 `nemotron-3.5-embed-1b`
   都是 **2048 维**(`nemotron-3.5-embed-8b` 是 4096)。`nvidia/embed-qa-4`、
   `llama-3.2-nv-embedqa-1b-v1` 等在本账号下是 404(未开通)。

2. **重启 API 与 worker**(配置在启动时加载):用 `backend/scripts/ops/restart_services.sh`。

3. **认知补齐与索引重建都交给 pipeline 自己**:
   - ch10 这类"有正文无记录"的章由 `chapter_executor._ensure_cognition_for_saved_body`
     自动 `enqueue_cognition`;
   - 索引是**懒建**的:`rank_cognition` 解析上下文时顺手 `ensure_collection + prepare_index`。
     新空间集合从 0 点自己涨到 257 点,无需人工回填。

## 5. 必须记住的三件事

- **`space_identity` 含 model 名**(`provider/model/base_url/modality/encoding/truncate`),
  换模型 = 换集合名 = 换向量空间。集合名形如
  `chapter_cognition__<project去横线>__<space_id>__<dim>`。
  本次验证:旧模型 → `a37fd218...`,新模型 → `d3219c81...`。
- **`pgrep -f` / `pkill -f` 会匹配到自己的命令行**。写"含关键字字面"的 kill 循环时,
  被检查进程列表里包含**当前 shell** → 自己被 SIGTERM(exit 143)。
  规避:把 kill 逻辑放进**脚本文件**执行(调用方 cmdline 只有脚本路径)。
- **从 `/proc/<pid>/environ` 抄 env 时,含 JSON 的值必须重新加单引号**:
  `LLM_EXTRA_BODY_JSON={"thinking":{"type":"disabled"}}` 直接 source 会被 shell
  剥掉引号 → 退化成 `{thinking:{type:disabled}}` → worker 打印
  `LLM_EXTRA_BODY_JSON is not valid JSON; ignored` 后**静默继续**(thinking 没被禁用)。
  另:cmdline 用 `\0` 分隔,字符串匹配前要 `.replace('\0',' ')`。

## 6. 固化产物

- `backend/scripts/ops/dump_service_env.py` —— 从运行中的 API 进程抄 env,JSON 值正确加引号。
- `backend/scripts/ops/restart_services.sh` —— 重启 API / celery(main / pipeline-control /
  cognition / beat),默认只重启 celery,`--with-api` 连 API 一起。

## 7. 验证证据

| 项 | 结果 |
|---|---|
| `current_embedding_space().identity` | `d3219c81...`(与预期一致) |
| 单次 `space.embed()` | `dim=2048` |
| worker 日志 | `POST https://integrate.api.nvidia.com/v1/embeddings 200 OK` |
| 新空间集合点数 | 0 → **257** |
| pipeline | `failed(67)` → **`generating, completed 21/100`** |
| 正文产出 | 42 章 → 43 章(继续增长中) |

## 8. 未解决

- 上游 NVIDIA 仍可能再次 EOL。当前没有本地 embedding 通道(cliproxyapi `:8317`
  不提供 `/v1/embeddings`,返回 404),所以 embedding 仍然依赖外部云。
  **建议**:要么在本地网关补一个 embedding 模型,要么把"空间索引为空/embedding 不可用"
  做成显式告警(目前是静默失败)。
