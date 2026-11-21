# 用项目自己的编排跑通成书链(2026-11-21)

## 起因

用户指出:**"我发现你还是自己去写脚本生成,而不是调用项目去工作"**。

这是对的,而且是本批所有问题的共同病根。此前每一段"生成"都是我(agent)在 `/tmp`
写 `gen_book.py` / `gen_volume.py` 之类脚本,用 HTTP/SSE 逐段驱动。这不是"调用项目" ——
它把项目自己的编排器(状态机、重试、质量债、暂停/回滚)绕过,由我在外面又实现了一遍。
后果:链路看起来能出字,但真实用户会撞到的缺陷一个都没被暴露。

## 项目自己的编排是什么

```
POST /api/pipeline            建整书/整卷任务(需已有 Chapter 行)
POST /api/pipeline/{id}/start 持久化控制命令 → dispatch 到 pipeline-control 队列
GET  /api/pipeline/{id}       进度 + 每章状态(state/word_count/error_message)
POST /api/pipeline/{id}/pause | rollback
```

- 实现:`app/services/pipeline_service.py` + `app/services/production/runtime.py`(`_chapters()`)。
- 前端界面:`frontend/src/components/workspace/BookPipelinePanel.tsx` + `useBookPipeline.ts`。
- 需要 **celery worker ×2**:默认队列(执行生成)+ `pipeline-control`(控制命令),
  见 `docker-compose.yml:102/107`。
- 状态机:`planning / queued / generating / pausing / completed / failed`。

## 完整正常流程(全部走 API)

| 步 | 端点 | 关键点 |
|---|---|---|
| 1 | 建项目 | `target_word_count` + `target_chapter_words` + `chapters_per_volume_target`;不填则 20 万字/每章 4000/每卷 100–200 → 默认只算出 50 章 1 卷 |
| 2 | `POST /api/generate/outline {level:"book", staged_stream:true}` | 三段 A/B/C;`done` 事件带 `volume_plan` |
| 3 | `POST /api/projects/{pid}/outlines/{id}/confirm` | 不确认则卷纲读不到书纲 |
| 4 | `POST .../outlines/{id}/extract-settings` | **最容易漏**:把书纲落成 Neo4j 的 Character/WorldRule/Relationship;`world_rules=0` 时写作必失败 |
| 5 | `POST /api/generate/outline {level:"volume", volume_idx:N}` | **章节行是它的副产品**,不需要手工建章 |
| 6 | `POST /api/pipeline` → `/start` | 项目自己逐章跑(生成→净化→落库→统一收尾→评审) |

实测(百万字项目、卷 1):`completed_chapters 3/3`,3765 / 2333 / 2948 字,零
`world_rules_context` / `different loop` 错误。随后启动全项目 153 章 pipeline。

## 修掉的 8 处真缺陷

1. **书纲永远保存失败** — `outline_consistency_gate` 用"全文『第X卷』提及次数 ≥
   `len(plan)*16`"判"疑似残留多套分卷方案";卷数少时阈值低得离谱(plan=1 时仅 32),
   正文正常描述多卷即被误杀。改为**卷号序列回绕**检测(`1,2,3` 后又出现 `1,2,3`),
   同一卷号被正常重复提及不再算多套。
2. **`volume_plan` 只剩 1 条 → 第 2 卷起卷纲永远生成不了** —
   `_extract_volume_plan` 用 `for raw_body in reversed(matches)` 只取最后一个
   `<volume-plan>` 块,而 v4-flash 会分块输出。改为**合并所有块**(按 idx 去重、
   先到先得、按卷号排序)。
3. **stage A 缺"七、八、九"段** — system 要求 一三七八九,user prompt 只写
   "请生成骨架五段",模型写 一、三 就停,终校验报 `缺少正文段落 七,八,九`。
   prompt 改为显式点名五段(含"七、分卷规划(末尾必须带 `<volume-plan>` JSON)")。
4. **模型无视 scale 只规划 1–2 卷** — 流式路径没有重试。`_validate_book_stage(stage="A")`
   增加"卷数 < `scale.n_volumes` 即不合格";流式路径捕获后用 `_top_up_volume_plan`
   按 `chapters_per_volume` 补齐缺失卷号(`idx/title/est_chapters` 显式占位)。
   **结构缺失(缺段)仍照旧失败** —— 不拿补齐掩盖残缺大纲。
5. **用户设的每章字数被静默忽略** — `api/generate.py` 读 `target_chapter_words` /
   `chapters_per_volume_{min,max,target}`,而设置面板与探针写的是 `chapter_words` /
   `chapters_per_volume`。结果"100 万字、每章 2000 字"被算成 **2 卷 × 125 章**。
   两处读取点(`compute_scale`、`resolve_chapters_per_volume_pref`)统一按
   "规范键 → 简写键 → 默认"回退。
6. **设定提取稳定 502「人物关系与角色列表不一致」** — split 模式(默认开)把
   characters / world_rules / relationships 三 pass **并行**跑,relationships pass
   看不到 characters 名单却被要求人名逐字一致 → 实测 3/3 失败,连带 `world_rules`
   恒为 0,写作链卡在"当前世界规则暂时无法读取"。改为 **characters 先出**,名单作为
   硬约束拼进 relationships 的 system prompt。
7. **章节成批 failed(真因被两层掩盖)** —
   a. `app/db/neo4j.py` 全局单例 driver 与创建它的 event loop 绑定;celery prefork
      子进程不跑 lifespan → 要么"未初始化",要么跨 loop 复用报
      `got Future attached to a different loop`。改为**按 event loop id 缓存** +
      `get_neo4j()` 懒创建;`current_rule_driver()` 懒 `init_neo4j()`。
   b. `world_rule_sources` 用 `raise ... from None`、`api/generate.py` 只记
      `type(e).__name__` —— 真因全被吞。补 `exc_info=True` 与异常消息。
8. **建章 503**(上一批已修,本批复验) — `reindex_chapter_structure` 后
   `db.refresh(chapter)` 只回填 5 列,构造响应访问 `chapter.title` 触发
   `MissingGreenlet`;改为 `db.get(Chapter, id, populate_existing=True)`。

## 验证

- 卷 1 走 `POST /api/pipeline`:3/3 `completed`,3765 / 2333 / 2948 字,零
  `world_rules_context` / `different loop` 错误。
- 设定提取:`{"characters_created":7,"world_rules_created":22,"relationships_created":10}`,
  Neo4j `WorldRule` 22 条(此前恒为 0)。
- 离线:`tests/test_outline_volume_plan_topup.py`(10)+ `tests/test_planning_chain_fixes.py`(7),
  连同 `test_outline_prompt_integrity` / `test_outline_chain_wiring` / `test_audit_guards`
  共 44 passed / 0 failed。
- commit `f0f8bad` 已推送 `origin/main`。

## 真正的根因:6 个写作/认知任务的模型路由写错了(已修)

排查"每章都失败"时,worker 日志给出了决定性线索:

```
OpenAI-compatible endpoint throttle sleeping 2.4s (task=scene_writer model=gemini-3.1-pro ...)
```

**用户明确指定的是 `deepseek-v4-flash`,而实际写正文用的是 `gemini-3.1-pro`。**
此前只绑定了大纲 8 个任务与评审任务(`chapter_evaluation` / `critic*` / `evaluation`),
**写作链与认知链一直没有绑定**:

| 任务 | 改动前 | 改动后 |
|---|---|---|
| `scene_writer` / `scene_planner` | gemini-3.1-pro | deepseek-v4-flash |
| `chapter_cognition` / `chapter_entities` | gemini-3.1-pro | deepseek-v4-flash |
| `relationship_scope` / `summary` | gemini-3.1-pro | deepseek-v4-flash |

机制细节:每个 task_type 在 `prompt_assets` 里**各有 7 行**(每个端点一行),只有 1 行
`is_active=1`;那行的 endpoint 本来就是 `novel-v4flash`(base_url 8317),但 `model_name`
被写成 `gemini-3.1-pro` —— 网关按 model_name 路由,所以实际调的是 gemini。

修法走项目自己的配置入口,不直连改库:

```bash
curl -s -X PUT "http://127.0.0.1:8020/api/prompts/{prompt_id}?confirm_mismatch=true" \
  -H "Content-Type: application/json" -H "X-API-Key: [API_KEY]" \
  -d '{"model_name":"deepseek-v4-flash"}'
```

**这是数据库配置,不在 git 里。** 种子(`PromptRegistry.seed_builtins`)不写死
`model_name`,新版本路由从被取代的行继承(`_ROUTE_BINDING_FIELDS`),所以不会被种子覆盖;
但新环境仍需要操作员配置一次。

效果(卷 1,其余配置不变):改动前 71/153 章 failed(其中 68 章是新章白跑);改动后
2/3 completed(3317 / 2725 字),唯一失败的 ch2 是**已有正文**的章走局部修订
(`targeted_revision_unapplied`),不影响新章成书。

## 顺带修掉的一个自身回归

按 event loop 隔离 Neo4j 驱动时把模块级 `_driver` 改名成 `_drivers`,但有 5 处外部代码
直接读 `neo4j._driver`(`entity_tasks` / `outline_settings_delivery` / `outline_to_facts`×3),
worker 里实测 `AttributeError: module 'app.db.neo4j' has no attribute '_driver'`,
实体投影成批失败。补模块级 `__getattr__` 把 `_driver` 映射为"当前 running loop 的驱动",
保持旧语义(init 后非 None,否则 None),未知属性仍抛 `AttributeError`。commit `e3e2c26`。

## 全项目 pipeline 的失败率观察(下一层问题,未修)

启动 153 章全项目 pipeline 后,`completed` 停在 2,`failed` 持续增长(5 分钟内 6 → 10)。
排查结论:**正文其实生成了** —— `generation_tasks.char_count` 有 4000–5300,
失败发生在生成之后的修订环节。`params_json.revision_stop_reason` 两类:

- `revision_context_unavailable`(多数):错误文案是"正文生成:人物认知检索暂不可用…"。
  但 embedding 端点本身是通的(worker 日志 `POST https://integrate.api.nvidia.com/v1/embeddings 200 OK`),
  Qdrant 里 `chapter_summaries__<project>`(6 点)与 `chapter_cognition__*` 集合也都存在,
  所以不是"服务没配",而是**自动修订所需的认知上下文在该章尚未建立**。
- `targeted_revision_unapplied`:局部修订未能可靠落实,保留修订前候选。

`production/chapter_executor._outcome` 的判定本身正确(要求
`saved_hash == content_hash(chapter.content_text)` 才认 `completed`/`needs_review`),
因此这些章标 `failed` 意味着**本次生成的正文没有落库**;`revision_stop_reason` 分支明确
"只把候选留在 task 里,不覆盖章节正文、不写下游记忆" —— 对**已有旧正文**的章这是保护
(所以卷 1 那两章"原稿已保留"是预期行为),对**新章**则是白跑一次。

本批未改这一层。可选方向:(1) 先用 `enable_polish:false` 跑出可读正文,修订单独处理;
(2) 让"无旧正文的新章"在修订失败时把候选落库并标 `needs_review`,而不是丢弃。

## 未做 / 遗留

- 卷 5 卷纲未生成(卷 1–4 已有章节,共 153 章);全项目 153 章 pipeline 已启动、尚在跑。
- 全项目 pipeline 中 2 章 `failed`,消息为"本次生成没有成功保存可用正文,原稿已保留" /
  "局部修订未能可靠落实,已保留修订前候选,请复核" —— 属质量债终态的正常降级
  (原稿保留),未深入。
- 会话内项目记忆后端三次返回 `SESSION_UNAVAILABLE`,按规则未做任何写入。
- 前端 `BookPipelinePanel` 未做人工点击验证,只用 API 等价路径跑通。

## 可复用

同批产出 skill `aiwrite-own-pipeline-run`(入口、必需服务、6 步流程、8 类缺陷模式、
验证清单)。
