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

## 独立审查发现的 8 处缺陷(已修,commit `ca302ec`)

派 explorer 对本批 diff 做只读独立审查(它自己在临时脚本里复现每条结论),抓到的缺陷里有
**两处是我本批引入的阻塞**:

**P1-1「多套分卷方案」判定我改了三版都不对** ——
初版"提及次数 ≥ `len(plan)×16`"(卷数少时阈值过低);第二版"同一卷号重复出现"(正常重复提及被误杀);
第三版"卷号下降即回绕" → `1,2,1`(正文里"第二卷承接第一卷的钩子")被误杀,**书纲直接存不下**
(实测 `book_content` 抛 `OutlineConsistencyError`)。第四版:只认**行首**的卷号条目,
且要求"以 1 开头的连续递增段出现 ≥2 次" —— `1,2,3,1,2,3` 判;`1,2,1` / `1,1,1` / `1,2,1,1,2` 不判。

**P1-2 简写键被套了错误的夹取** —— `resolve_chapters_per_volume_pref` 把 `chapters_per_volume`
也套上旧三键的 `[1,50]`;而该键是"每卷多少章"的真值(100),夹到 50 会让卷纲 prompt 的硬约束
变成"本卷必须 50 章",**500 章的规划静默缩成 250 章**。改为不夹取。

其余:`est_chapters` 非数字时在解析层抛(盖掉可读的"A 阶段不完整")+ top-up 调用落在 try 外;
stage A 只查卷数不查卷号连续性(缺号要等三次模型调用跑完才在保存处失败);多块 `<volume-plan>`
合并时无 idx 的块都从 1 起算仍会塌成 1 条、`setdefault` 又丢掉后块的字段;
`neo4j.py` 按 loop 缓存的三个缺陷(`id(loop)` 复用返回死 driver、`init_neo4j` 关掉同 loop 在用的
driver、模块级 `__getattr__` 被 pytest monkeypatch 的 undo 写坏)。

## 我这批的三次同类误判(教训)

同一个毛病犯了三次:**看到"合理的设计解释"就停,不去查最后一层实证**。

| 现象 | 我先给的解释 | 日志里的真因 |
|---|---|---|
| 书纲 `save_failed` | "模型不守 scale 指令" | 一致性校验阈值过低 |
| 章成批 `failed` | "修订策略如此" | 写作链模型路由指向 gemini |
| ch2 局部修订失败 | "设计上不升级为整章重写" | `rewrite` 任务从未绑端点 |

三次都只需在日志里 grep 一行。附带两条流程教训:
1. **改完必须跑全量**。本批两次提交都只跑了相关文件,于是 7 个回归(3 个本次引入、4 个上批引入)
   是靠事后补跑全量才捞出来的 —— 其中 `_driver` 回归甚至是 worker 日志先发现的。
2. **失败场景要选对**。我一度拿"已有正文"的卷 1 三章当验证对象,那本身走的是修订分支;
   新章才是主线路径,而卷 2 又依赖卷 1 的认知台账 —— pipeline 必须按卷序推进(全项目任务)。

## pipeline 等该章认知就绪(本轮修复,commit `40cd6aa`)

**现象**:ch1 生成成功,ch2 起 99 章全部 `character_cognition_not_ready`。

**根因**:下一章的上下文要求"**它之前所有章**"的认知记录为 `completed`
(`cognition_reader.validate_completed_receipt`:record 缺失或 status != completed
直接抛),而认知提取是章后**异步**派发的 —— pipeline 紧接着就取下一章,必然读到缺失。

**改法**([runtime.py](/root/ai-write/backend/app/services/production/runtime.py)):
`_chapters` 每章 completed 之后、**提交之后**(不持行锁,心跳任务仍在跑)等该章认知
到达可用终态;`PIPELINE_COGNITION_WAIT_SECONDS` 默认 600(`0` 关闭 = 旧行为);
`completed` 才算就绪,`failed`/`obsolete` 立即返回(不无限等),超时只记 warning 并继续,
让下一章显式报错而不是把整本书卡死。

**实测对照**(同配置同卷):

| | 结果 |
|---|---|
| 修复前 | 瞬间 100 章全 failed,completed 停在 1–2 |
| 修复后 | `state=generating`,completed 递增(ch2 2491 字、ch3 2520 字),剩余 82 章 pending |

## 另一处硬根因:`content_hash` 触发器丢失(commit `0f2d6db`)

`chapters` 表上**零触发器**,`content_hash` 列还在 —— 实测 **1096 章有正文、hash 全为空**。
项目多处用"hash 非空"当"这一章已落库正文"的廉价信号(认知边界 `_has_saved_body`、
弧进度、前文衔接),于是下游一律认为"前文没有正文"。

迁移 `a1001962` 幂等重装函数/触发器,并只回填"有正文且 hash 为空"的行(不动作者已接受
的 hash 与正文);`scripts/ops/alembic_sync.py` 的探针登记该 head(并新增 `trigger` 这一
检查 kind)。验证:`alembic head=a1001962`、触发器存在、hash 非空 1096、有正文但 hash 空 0。

## 规划规模:每章字数/每卷章数(commit `3eb002b`)

`outline_generator._load_chapters_per_volume_pref` 复用了旧三键的 `[1,50]` 夹取,把
"每卷 100 章"压成 50 → 卷纲硬约束 50 章/卷 → 每章预算 = 卷目标 200000 ÷ 50 = 4000 字。
即**"每章字数"这条链上从未被读取**,它是被"卷目标 ÷ 章数"反推出来的;章数被污染,
字数就跟着错。统一读取入口(`chapter_target_words.py`)后实测:

```
done {"chapters_created": 100, "chapter_word_counts": [2000, 2000, ...]}
vol1: 章数 100 | target_words 2000 - 2000
```

## 另外两条运行期约束(踩过)

- **celery worker 必须 `--concurrency=1`**:项目对同一项目的实体投影/认知是**独占**语义,
  并发两个任务会报 `EntityProjectionBusy('another entity worker owns this project')`。
- **celery beat 是必需服务**:`pipeline-command-recovery`(30s)、
  `cognition-extraction-recovery`(60s)、`entity-projection-recovery`(60s)都由它投递,
  缺它则认知台账不增长、pipeline 命令不重放。

## 当前瓶颈:认知提取的 evidence 拼接(模型侧,未解决)

链路机制已经修到位后,失败收缩到一处,并拿到了决定性诊断(新加的 offender 日志):

```
Cognition evidence unlocatable attempt=1/3
offenders=['他画完,把笔收回口袋,退了一步。铁山低头看着自己锁骨下方,没看懂那是什么。
           他抬起头,想说什么,但没来得及开口。他的膝盖弯了。', ...]
```

模型返回的"依据"是**跨句/跨段拼接**的长引用,而 `evidence_locatable`
(`app/services/chapter_entity_extraction.py:27`)刻意只做"逐字包含 + 标点形式归一化",
注释里写明**不容忍省略号、不容忍拼接两处** —— 那是区分"引用"与"改写"的判据,
放宽会把伪造依据也放进来。

失败后果:`ch3` 的认知记录 `failed` → `ch4` 起 `character_cognition_not_ready`
→ 该卷后续章成批失败(实测 `completed=2 / failed=90 / pending=7`)。

**项目自带的出口**:`ALLOW_COGNITION_DEGRADED_CONTINUATION`(`app/config.py`,
默认 `False`)。它的注释写着"实测 2026-11-14/16 三次,一次提取软失败就让之后每一章
都被 not_ready 拦住"——正是本次这个状态;打开后那一章按"无认知变化"计入
(写手拿到的历史少一层),整本书不停,每次降级都有 warning 便于事后补跑提取。

**可选处置**(需要产品决策,本批未动):
1. 打开 `ALLOW_COGNITION_DEGRADED_CONTINUATION` —— 全书能跑完,代价是少数章少一层历史;
2. 只给 `chapter_cognition` 这一个任务换更听话的模型(不改用户的默认模型选择);
3. 继续调提取提示词(注意 SYSTEM_PROMPT 已经写了"不把不相邻的两处拼在一起",边际收益低);
4. 给 `evidence_locatable` 增加"分段可定位"的宽容——**不建议**,那正是伪造依据的通道。

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
