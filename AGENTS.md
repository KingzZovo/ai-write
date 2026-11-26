# ai-write 仓库代理须知

> **本文件只放"当前有效、可判定"的规则与约束。**
> 历次开发批次的详细记录已迁到 [docs/CHANGELOG-batches.md](docs/CHANGELOG-batches.md)
> ——迁出原因:本文件曾达 193 KB / 253 条,而运行时**只加载前 32 KB**,约 84%
> 的内容(含较早的规则)根本不进上下文,导致"跑着跑着就忘"。
> **加新内容前先想清楚:这是规则(留这里)还是批次记录(写 changelog)。**

## 1. 项目与运行环境

- 仓库:`/root/ai-write`(后端 `backend/`,前端 `frontend/`);Python 用 **`/root/ai-write/.venv/bin/python`**(不是系统 python,也不是 `/root/hcx_venv`)。
- **开发/验证一律用隔离库**:`postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/aiwrite_eval`。不要碰生产库。
- 端口:**API 8020**(8000/8010 是别的项目)、Qdrant 56333、Redis 56379、Neo4j `bolt://127.0.0.1:57687`。
- **密钥不写进仓库文件、文档或提交信息**。需要 API key / 启动 env 时用
  `python backend/scripts/ops/dump_service_env.py /tmp/aiwrite.env`(从运行中的 API 进程
  抄 env,含 JSON 的值会自动加引号)。**不要用 `pgrep -f "uvicorn …"` 取 PID** —— 它会
  匹配到你正在执行的这条命令行(见第 9 节),拿到的是自己。

## 2. 必需服务(缺任一,链路会静默不推进)

```bash
cd /root/ai-write/backend
export DATABASE_URL=postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/aiwrite_eval
export SECRET_KEY=... NEO4J_URI=bolt://127.0.0.1:57687 NEO4J_USER=neo4j NEO4J_PASSWORD=...
export REDIS_URL=redis://127.0.0.1:56379/0 QDRANT_HOST=127.0.0.1 QDRANT_PORT=56333
export QUALITY_EVAL_API_KEY=... DISABLE_AUTH=1 APP_ENV=development
export LLM_EXTRA_BODY_JSON='{"thinking":{"type":"disabled"}}'

# 1) API
setsid nohup /root/ai-write/.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8020 >> /tmp/aiwrite_api.log 2>&1 &
# 2) 主 worker —— 必须 --concurrency=1
setsid nohup /root/ai-write/.venv/bin/celery -A app.tasks:celery_app worker --loglevel=info --concurrency=1 -n main@%h > /tmp/celery_main.log 2>&1 &
# 3) pipeline-control worker
setsid nohup /root/ai-write/.venv/bin/celery -A app.tasks:celery_app worker --loglevel=info --concurrency=1 -Q pipeline-control -n pipe@%h > /tmp/celery_pipe.log 2>&1 &
# 4) 认知提取 worker —— 必须独立队列,否则写作排队会把它堵住
setsid nohup /root/ai-write/.venv/bin/celery -A app.tasks:celery_app worker --loglevel=info --concurrency=1 -Q cognition -n cog@%h > /tmp/celery_cog.log 2>&1 &
# 5) recovery worker —— 恢复/派发类 beat 任务(entities.*/foreshadows.*/evaluations.*/cognition.replay_pending)
#    必须独立队列:默认队列是 concurrency=1 的写作 worker,单次 pipeline 生成实测占用
#    1031 秒,期间恢复任务全被饿死 → pending 认知记录派发不出去 → 认知守候超时 → 级联失败。
setsid nohup /root/ai-write/.venv/bin/celery -A app.tasks:celery_app worker --loglevel=info --concurrency=4 -Q recovery -n rec@%h > /tmp/celery_recovery.log 2>&1 &
# 6) beat —— 调度各 *-recovery(30s/60s)与 hourly/daily 维护任务
setsid nohup /root/ai-write/.venv/bin/celery -A app.tasks:celery_app beat --loglevel=info --schedule=/tmp/aiwrite-celerybeat > /tmp/celery_beat.log 2>&1 &
```

- **快速重启**(改完代码 / 改完模型路由 / 换 embedding 模型后必须重启,否则 prefork 子进程
  仍用旧代码与旧 DB 路由):`bash backend/scripts/ops/restart_services.sh` 默认只重启 celery
  四个角色,加 `--with-api` 连 API 一起。脚本从**运行中的进程**提取 env,不硬编码密钥;
  它是唯一推荐的重启入口 —— 手写 kill 循环会踩第 9 节的"杀自己"坑。

- **`--concurrency=1` 是硬要求**:项目对同一项目的实体投影/认知是**独占**语义,并发会报 `EntityProjectionBusy('another entity worker owns this project')`。
- **认知提取必须走 `cognition` 独立队列**(`task_routes` 里已配)。它与写作共用 worker 时,写作单章 2–4 分钟会把提取排到十几分钟后;而认知边界是全有或全无,一章 pending 就把后续整批章堵死(实测 293 次 `not_ready` 全指向同一章 pending)。
- **改完代码必须重启 worker 与 API**——prefork 子进程只在启动时加载代码。这条踩过多次。

## 3. 正常流程(全部走 HTTP,不要自己写调度脚本)

```
建项目(设规模)→ 书纲 → 确认 → 提取设定 → 卷纲(章节行是副产品)→ pipeline
```

| 步 | 端点 | 要点 |
|---|---|---|
| 书纲 | `POST /api/generate/outline {level:"book", staged_stream:true}` | 三段 A/B/C;`done` 带 `volume_plan` |
| 确认 | `POST /api/projects/{pid}/outlines/{id}/confirm` | 不确认则卷纲读不到书纲 |
| 提取设定 | `POST /api/projects/{pid}/outlines/{id}/extract-settings` | **最容易漏**;落成 Neo4j 的 Character/WorldRule/Relationship |
| 卷纲 | `POST /api/generate/outline {level:"volume", volume_idx:N}` | **章节行是它的副产品**,不要手工建章 |
| 卷纲重建 | `POST /api/projects/{pid}/volumes/{vid}/regenerate` | 报"本卷已有章节"时用这个入口 |
| 整书任务 | `POST /api/pipeline` → `POST /api/pipeline/{id}/start` | 项目自己逐章跑 |
| 进度 | `GET /api/pipeline/{id}` | **先读每章 `error_message`,再看日志** |

- `state`:`planning/queued/generating/pausing/completed/failed`。
- `pause` / `rollback` 是恢复手段;`failed` 的任务要**重建**(章节清单变了旧任务会拒绝启动)。
- **不要用 `/tmp` 脚本逐章打 SSE** —— 那等于把项目的编排器重写一遍,会掩盖真实缺陷。

## 4. 规模与字数的口径(容易记混)

| 项 | 值 |
|---|---|
| 代码**兜底默认** | `CHAPTER_DEFAULT_WORD_COUNT = 4000` 字/章 |
| 每卷章数默认 | min 100 / max 200 / target 150 |
| 项目 `target_word_count` 默认 | 200000(20 万字) |
| 白蜡树项目实际设置 | `target_chapter_words=2000`、`chapters_per_volume_*=100` → **500 章 / 5 卷 / 每卷 100 章** |

**项目设置优先于代码默认**,但设置里**有两套历史键名**,读取必须双侧回退:

- 规范键:`target_chapter_words`、`chapters_per_volume_min/max/target`(前端写的是这套)
- 简写键:`chapter_words`、`chapters_per_volume`(脚本/探针写的是这套)

统一入口在 `app/services/chapter_target_words.py`(`resolve_chapter_words` /
`resolve_chapters_per_volume_pref` / `resolve_volume_chapter_range` / `build_planning_scale`)。
**新代码一律走它**,不要再写 `settings.get("target_chapter_words")` 这种单键读取。

## 5. 模型路由(DB 配置,不在 git;新环境要配一次)

- `prompt_assets` 里**每个 task_type 可能有多行**(每端点一行),只有 1 行 `is_active=1`;
  那行的 `endpoint_id` 可能对,但**网关按 `model_name` 路由** —— 写错就会调用另一个模型。
- 用户指定:**写作/规划链用 `deepseek-v4-flash`**(端点 `novel-v4flash`,base_url `127.0.0.1:8317/v1`)。
- **例外**:`chapter_cognition`(章认知提取)单独用 **`gemini-3.1-pro`**(端点 `continuity-probe`)——
  实测同一章:deepseek-v4-flash 对"依据必须逐字可定位"这条要求**四次全败**,
  gemini **一次成功**;它返回的 `evidence` 会跨句拼接,而判据`evidence_locatable`
  **有意不容忍拼接**(那是"引用 vs 改写"的分界,不得放宽)。
- 改法走项目自己的入口,不要直连改库:
  `PUT /api/prompts/{prompt_id}?confirm_mismatch=true` body `{"endpoint_id":..., "model_name":...}`
  改完**重启 worker/API**。
- 自检(应返回空列表):
  `curl -s "http://127.0.0.1:8020/api/prompts" -H "X-API-Key: $K" | python3 -c "import json,sys;print([p['task_type'] for p in json.load(sys.stdin) if p.get('is_active') and not p.get('endpoint_id')])"`
- `GET /api/model-config/tasks` 是 **stub**(返回空),不是路由入口。

- **embedding 走外部 NVIDIA 云,不在 8317 网关里**:`http://127.0.0.1:8317/v1/embeddings`
  返回 **404**(网关只有 chat 模型)。当前模型 `nvidia/nemotron-3.5-embed-1b`(**2048 维**),
  由 `prompt_assets(task_type='embedding')` → `llm_endpoints` 决定;改它走
  `PUT /api/model-config/endpoints/{id}` + `PUT /api/prompts/{id}?confirm_mismatch=true`。
- **`space_identity` 含 model 名** → 换 embedding 模型 = 换向量空间 = 换集合名
  (`chapter_cognition__<project>__<space_id>__<dim>`),旧索引不复用;**新集合由写作时的
  `rank_cognition` 惰性重建**,不需要人工跑回填。
- **上游 EOL 的症状是"整批章 failed"**,文案 `正文生成:人物认知检索暂不可用…`
  (兜底码 `character_cognition_retrieval_unavailable`,对应 410/超时/连接失败多个原因)。
  先脱敏验证 provider:
  `python backend/scripts/ops/dump_service_env.py /tmp/e; . /tmp/e; `
  然后用 `/tmp/e` 里的 key 直连 `POST {base_url}/embeddings` 看真实状态码。
  本次事故复盘:[docs/evaluation/embedding-eol-20261124.md](docs/evaluation/embedding-eol-20261124.md)。

## 6. 认知链(长篇最容易卡死的地方)

- **边界是全有或全无**:写第 N 章时,`load_ledger` 要求"边界之前**所有有正文的章**"
  的认知记录为 `completed`(`cognition_reader.validate_completed_receipt`:记录缺失或
  `status != completed` → 抛 `character_cognition_not_ready`)。一条不合就整卷停摆,并**级联**。
- **pipeline 已实现"推进下一章前等该章认知就绪"**(`runtime._wait_chapter_cognition`):
  等待上限 `PIPELINE_COGNITION_WAIT_SECONDS`(**默认 600**;`0` 关闭)、轮询
  `PIPELINE_COGNITION_POLL_SECONDS`、每轮检查 `should_stop`(pause/所有权)。
  实测:180s 时链卡在 ch6;600s 后 completed 连续增长。
- **`failed` 的处置**(实测得来,别想当然):
  - 立刻重投**无效**:Celery `autoretry` 是秒级的 3 次,同一 prompt 下重复同一错误;
  - 只改 `replay_pending_cognition` 的 `due` **也没用**:`run_cognition_extraction`
    对 `failed` + 非原 delivery_id 的投递直接 skip(`model_retry_requires_original_task`);
  - **有效路径**:冷却(默认 90s)后**先置 `obsolete` → 再 `enqueue_cognition`**
    ——它专门重开 obsolete,新 cycle、`attempt_count` 归零。
    直接置 `pending` 会被预算上限拦住(`attempt_count=4` 时任务 0.2 秒就返回
    `model_retry_budget_exhausted`,连模型都没调)。
- **pipeline 对已有正文的章默认不重写**(`chapter_executor._should_skip_existing_chapter`):
  每轮重跑都会改写正文 → `content_hash` 变 → 该章按旧 hash 绑定的认知记录作废 → 自锁。
  需要真重写时传 `options.force_rewrite=true`。跳过时还会**补齐该章当前正文缺失的认知**。
- `chapters.content_hash` 由触发器 `chapter_content_hash_changed` 维护;迁移 `a1001962`
  负责幂等重装并回填(曾整库丢失,导致 1096 章正文被当成"没有正文")。
  核查:`select count(*) from chapters where coalesce(content_hash,'')='' and coalesce(content_text,'')<>''` 应为 0。

## 7. 测试与提交纪律

- 后端测试:`cd backend && /root/ai-write/.venv/bin/python -m pytest --offline -q -p no:cacheprovider`
- **改了代码就必须跑全量**,不要只跑相关文件 —— 本仓库有过三次"只跑相关文件、回归从别处冒出来"的记录。
  注意 `--offline` 会**跳过**真实 PG 集成测试(`tests/test_pipeline_runtime.py` 等),
  它们需要 `PIPELINE_TEST_DATABASE_URL`;碰 pipeline 逻辑时至少要确认这些用例的语义没被改变。
  涉及等待/重试的用例请在 `canonical()` 之类的 fixture 里显式关闭等待(否则会白等满超时挂死 CI)。
- 加迁移后要在 `scripts/ops/alembic_sync.py` 的 `SCHEMA_PROBES` **登记一行**
  (kind 支持 `column|unique|table|trigger`),否则 `tests/test_alembic_sync.py` 会红。
- 提交前:`git status` 清理本任务 scratch;提交信息写清"现象→根因→改法→验证"。
- 常设授权范围内可 commit + push;**不**强推、不改写历史、不提交无关改动。

## 8. 排查纪律(踩过三次的同一个坑)

**看到"合理的设计解释"就停、不去查最后一层实证 —— 同一个错误犯过三次。**

| 现象 | 我先给的解释(错) | 日志里的真因 |
|---|---|---|
| 书纲 `save_failed` | "模型不守 scale 指令" | 一致性校验阈值过低 |
| 章成批 `failed` | "修订策略如此" | 写作链模型路由指向 gemini |
| 局部修订不生效 | "设计上不升级为整章重写" | `rewrite` 任务从未绑端点 |
| ch2 认知补不上 | "置回 pending 即可" | `attempt_count` 用尽 → 走 obsolete 重开 |

**规矩**:`error_message` 是**兜底文案**,它对应多个错误码。先读 `error_message` →
反查代码里的错误码 → 再 grep worker 日志确认 → 最后才下结论。不要跳过中间两步。

## 9. 常用状态查询

```bash
# pipeline 进度(先看这里,再看日志)
curl -s "http://127.0.0.1:8020/api/pipeline/project/<PID>?limit=1" -H "X-API-Key: $K" | python3 -m json.tool | head -40
# 认知台账
psql ... -c "select status,count(*) from chapter_cognition_extractions group by 1"
# 章节产出
psql ... -c "select v.volume_idx,count(*),min(ch.word_count),max(ch.word_count) from chapters ch join volumes v on v.id=ch.volume_id where v.project_id='<PID>' and ch.word_count>0 group by 1"
```

- `chapters` 表**没有** `project_id`(要 join `volumes`);`outlines` 表**没有** `volume_idx`(在 `content_json->>'volume_idx'`)。
- `pkill -f` / `pgrep -f` 会匹配自己的命令行。`pkill -f /tmp/x.py` 会杀掉自己;更隐蔽的是
  **kill 循环**(遍历 `/proc/*` 按 cmdline 关键字 kill):当这段代码与关键字**字面**出现在
  同一条 shell 命令里时,被检查的进程列表包含当前 shell 本身(exit 143)。
  规避:把 kill 逻辑放进**脚本文件**执行 —— 调用方 cmdline 只有脚本路径,不含关键字。
  另:读 `/proc/<pid>/cmdline` 得到的是 `\0` 分隔串,要匹配 `--port 8020` 这种**跨参数**串,
  先 `.replace('\0', ' ')`(否则永远匹配不上,表现为"找不到进程")。

## 10. 当前项目(白蜡树)

- `project_id = 8b084406-7470-497c-9073-e236542cf19d`,目标 100 万字 / 5 卷 / 每卷 100 章 / 每章 2000 字。
- 卷 1(100 章)正在按 pipeline 逐章推进;卷 2–4 各 50 章待卷 1 完成。
- 书纲《白蜡树下》(林默 / 洛基血 / 白蜡树学院),"招生简章"起卷。

---

**历次批次记录(252 条)→ [docs/CHANGELOG-batches.md](docs/CHANGELOG-batches.md)**
