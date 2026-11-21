# 全仓审计汇总(2026-11-17)

## 一、覆盖

| 项 | 数值 |
|---|---|
| 分区 | **9**(p1–p8 为确定性 8 分区,每份 48–49 个文件;p9 为 3 个首轮遗漏文件的补审) |
| 覆盖文件 | **386 / 386 = 100.0%**(`backend/app/**/*.py`,排除 `__pycache__`) |
| 复跑命令 | `cd backend && .venv/bin/python -m scripts.ops.audit_coverage --dir ../docs/audit/2026-11-17` |

分区由 `backend/scripts/ops/audit_partition.py` 生成,规则:先按目录聚合,`services/` 按文件名前缀归主题,
任一组超过 48 个文件就切片,再按"当前最小桶优先"贪心均衡;同一文件永远落在同一分区,重复执行结果一致。

## 二、Findings 计数

| 级别 | 数量 | 已修/部分已修 |
|---|---|---|
| P0(会静默损坏数据/让整本书写坏) | 1 | 1 |
| P1(特定条件必现的缺陷) | **30** | **30** |
| P2(设计/可维护性) | 89 | 22 |
| P3(小问题) | 70 | 5 |
| 合计 | 190 | 58 |

第一轮(首轮子代理审计)报出 148 条;第二轮交叉复核(同一批文件由第二组子代理重读)追加 26 条,
其中 14 条 P1。上一轮被判为"误报"的配额路径本轮**改判为真缺陷并已修**(见第七节)。

## 三、P1 逐条状态

| # | 位置 | 问题 | 处置 | 验证 |
|---|---|---|---|---|
| 1 | `character_cognition.py:295` | 台账只认 `completed` 章,降级章的事实从后续章静默消失 | **已修**:边界前"有落库正文"的章(任何状态)都进来源;记录不可用时跳过并计数 `COGNITION_LEDGER_DEGRADED_TOTAL`;completed 章仍严格 | `tests/test_cognition_ledger_status.py` 6 项(含"中间章 `needs_review` 的事实出现在后续章台账") |
| 2 | `api/generate.py:795-826` | 桌面流式自动保存无条件覆盖作者文本(无前提、无行锁) | **已修**:请求开始记正文 sha256 基线 + 落库时 `with_for_update`;冲突时不覆盖,AI 稿落未激活候选并发事件 | `tests/test_chapter_autosave_precondition.py` 6 项 |
| 3 | `numeric_check.py:276-294` | 金额台账对 `metadata_json` 整列读改写,无行锁 | **已修**:版本行 `with_for_update` | `tests/test_numeric_check.py` 既有 + 代码核验 |
| 4 | `api/generate.py:826/834-866` | 正文先提交、版本行后提交,失败只 warning | **已修**:正文与 active 版本切换同一事务,版本写失败整体回滚 | 同 #2 的用例覆盖冲突与回滚路径 |
| 5 | `main.py:246` | CORS 注册在 Auth 内层 → 预检被 401,401/402 缺 CORS 头 | **已修**:CORS 改为最后注册(最外层)+ Auth 放行 OPTIONS | 代码核验(`grep -n add_middleware`) |
| 6 | `main.py:215` | `DISABLE_AUTH=1` 无环境守卫 = 全站匿名可写 | **已修**:production/prod 下忽略并 `logger.error` | `tests/test_audit_guards.py` 5 项参数化 |
| 7 | `tasks/generation_tasks.py:589` | 任何异常都写终态 `failed`,重投只接管 `pending` | **已修**:瞬时错误(超时/连接/DBAPI/Operational/Interface)保留 pending | `tests/test_audit_guards.py` 3 项 |
| 8 | `tasks/generation_tasks.py:2059` | 上层 `except` 吞掉一切并 return,使 #7 对"生成中途断连"无效 | **已修**:无正文可抢救且属瞬时类时 `raise` | 同上(瞬时分类用例) |
| 9 | `constory_checker.py:140` | 一致性检查失败返回默认"满分 0 问题" | **已修**:返回 `score=0.0 + unavailable=True + error`,进 `to_dict()` | 代码核验 + 序列化字段断言 |
| 10 | `strand_tracker.py:115` + `api/quality.py:112` | 无参构造自建会话且从不 `aclose` → 每请求泄漏连接 | **已修**:API 复用 `get_db` 会话 | 代码核验 |
| 11 | `api/knowledge.py:682,731` | Celery 在事务提交前派发,worker 读不到行 → 书/爬取永远 pending | **已修**:派发前 `await db.commit()` | 代码核验 |
| 12 | `api/lora.py:105-108` | `adapter_name` 未校验 → 路径穿越可写任意文件 | **已修**:字符白名单 + `resolve()` 父目录校验,非法 422 | 代码核验 |
| 13 | `services/change_log.py:92` | 缓存失效早于提交 → 并发重建用旧设定并长期驻留(漂移) | **已修**:挂 `after_commit` 再失效,挂不上则告警并退回立即失效 | `tests/test_audit_guards.py` 1 项 |
| 14 | `services/tool_registry.py:148` | `lookup_relation` 引用不存在的列,报错把事务置为 aborted | **已修**:改 ORM 查询(source_id/target_id + name),失败回滚,无作品上下文返回空 | 代码核验 |
| 15 | `services/export_service.py:203` | epub/pdf/docx 构建在事件循环里同步执行 | 导出期间其它请求(含写作 SSE)停摆 | **已修**:`asyncio.to_thread` |
| 16 | `services/foreshadow_confirmation_worker.py:54` | 认领后命中"评审不在 confirming"直接 return → 任务卡 running | **已修**:token 仍属本轮时写 `superseded + last_error` |


## 四、P2/P3 已修的 4 条

| 位置 | 问题 | 处置 |
|---|---|---|
| `outline_generator.py:1496` | 模型给 `chapter_count=5000` → 1000 次 240s 付费调用 | `_clamp_chapter_count`(单卷上限 200),`tests/test_audit_guards.py` 2 项 |
| `api/projects.py:309,349` | 中文书名导出把标题拼进响应头 → latin-1 报错 500 | 复用 `export.py` 的 RFC 5987 `_content_disposition` |
| `api/generate.py:1016` | 伏笔检查引用了事件流里不存在的 `save_db`,`NameError` 被吞 → **从未执行** | 抽出 `_foreshadow_setup_event()` 自开会话,回归用例断言其被调用 |
| `llm_call_logger.py:170` | 用量恒记 `user_id="king"`,配额按 JWT 用户查 → 永不触发 | 鉴权写 ContextVar(`middlewares/request_identity.py`),日志按请求身份归属;新增 `USAGE_CENTS_PER_1K_TOKENS`(默认 0);Celery 侧从 `params_json` 沿用 |
| `cascade_regenerator.py:150` | 卷内序号跨卷比较 → 后卷漏判 | 新增 `modified_volume_idx`,同卷按号/后卷全量/前卷跳过(仓内无调用方) |
| `chapter_quality_gate.py` | 篇幅验收阈值写死 0.80、与续写 0.95 两套口径;`missing_char` 每章几十条噪声 + `except: pass` | 阈值收敛到 `CHAPTER_MIN_TARGET_WORD_RATIO`(值不变 0.80);删除噪声检查 |
| `config.py` | `SECRET_KEY` 默认值 + 生产无校验 | `APP_ENV=production/prod` 且仍是默认值时**启动即失败** |

## 五、未修的 45 条(P2/P3)为什么没修

分三类,均已在各 `pN.md` 写明位置与影响:

1. **需要产品口径**(约 20 条):检测器误报阈值(钩子/时间倒退/读者拉力)、"跳过维度是否算满分"、书源是否允许执行 JS、导出是否放行软删作品等 —— 改哪一边都要先定判据。
2. **改动面跨模块**(约 15 条):`llm_call_logs` 缺 `_log_meta` 的分阶段大纲调用、`context_pack` 在事务内做向量查询、`usage_service` 无行锁累加、`Chapter` 缺唯一约束(需迁移)、`tool_registry` 与 `log_llm_call` 签名不匹配等。
3. **低风险但需单独验证**(约 10 条):会话/驱动泄漏点、`except: pass` 噪声、路由注册顺序、种子文案错字等。

## 六、回归与产物

| 项 | 结果 |
|---|---|
| 后端全目录离线 | **5117 passed / 762 skipped / 0 failed**(268.08s) |
| 新增测试 | `test_cognition_ledger_status.py`(6)、`test_chapter_autosave_precondition.py`(6)、`test_audit_guards.py`(14) |
| 迁移 | 无新增;`alembic heads` = **单 head `a1001961`** |
| 长跑 | `docs/evaluation/longrun-100-gemini-2000-20261117` 持续产出(审计期间 46 → 71 章,只增不减) |
| 对照页 | `docs/evaluation/_public/{index,2000,dsvflash}.html` 仍可生成 |

## 七、误报更正(原先判为误报、复核后确认是真缺陷)

- **配额默认路径**:上一轮判成"相邻字符串字面量拼接 ⇒ 没问题",**判错了**。`_DEFAULT_QUOTA_PATHS`
  是 **tuple**,而 `_quota_prefixes()` 对 `raw.split(",")` 取值 —— `QUOTA_ENABLED=1` 且未设
  `QUOTA_PATHS` 时必然 `AttributeError: 'tuple' object has no attribute 'split'`,中间件整体 500。
  **已修**:按类型分派(tuple 直接用,str 才 split);`tests/test_audit_guards.py` 新增 2 项,
  实测默认返回 6 个前缀、无尾逗号。
  教训:上一轮只 grep 了常量定义、没有真的调一次 `_quota_prefixes()`;凡"边界值/默认值"结论
  必须**跑一次**再下判。

## 八、审计方法边界

- 审计阶段只读,未执行测试;修复由主代理执行并逐条补离线回归。
- 子代理报告在传输中会截断,`p1/p3/p4/p6/p8` 的记录由"精简重述"补齐;`p7/p9` 为直接记录。
- 覆盖 100% 指"文件被打开并给出结论",不代表每条路径都被动态验证。

## 九、第二轮交叉复核:追加修复清单

同一批文件由第二组子代理重读后追加 26 条,已修 24 条(2 条明确不动,理由见末表)。

覆盖面:同一批 386 个文件由第二组读取并复核;传输中被截断的 3 份报告已用"只回 P0/P1 摘要"的方式补齐,
其中 2 份回答**无新增**(其发现已在 `p1.md` / `p9.md` 记录,且 p9 的 P1 已在本轮修复)。

### P1(14 条)

| 位置 | 事实 | 处置 | 验证 |
|---|---|---|---|
| `middlewares/quota.py:49` | `_DEFAULT_QUOTA_PATHS` 是 tuple,却对它调 `.split(",")` → 开配额即 500 | 按类型分派;补 `ge` 与文档 | `test_audit_guards.py` 2 项,实测返回 6 前缀 |
| `services/model_router.py:1812` | 无 embedding 端点时返回 **1536 维零向量**:与任何查询等距(检索=随机)+ 与 2048 维模型不匹配 | 抛 `EmbeddingUnavailableError`;上层的降级/阻断分支接住 | `test_audit_guards.py` 1 项 |
| `services/usage_service.py:118` | `record_usage` 是 ORM 读-改-写,并发丢更新 → 配额永不触发 | 改 `UPDATE ... SET col = col + delta`(PG 行锁串行化) | 代码核验 + 全量回归 |
| `services/memory_compactor.py:174` | 压缩点 id 用内建 `hash()`(按进程加盐)→ 同一摘要每次重算不同 id,重复压缩把同一段记忆塞进 prompt 多次 | 改 `uuid5(固定命名空间, project:idx:summary)` | 全量回归 |
| `tasks/__init__.py:374` | `_run_async_safe` 只等主协程就 `loop.close()`,任务内 `create_task` 发射的自动压缩被直接丢弃 | 关循环前排空剩余任务(上限 120s) | 全量回归 |
| `api/knowledge.py:733` | `crawl_book.delay` 在 commit 前派发 → worker 读不到行,爬取永久 pending | 派发前 commit | 代码核验 |
| `api/projects.py:350` | EPUB 导出把中文标题直接拼进响应头 → latin-1 编码 500 | 复用 RFC 5987 `_content_disposition` | 代码核验 |
| `api/generate.py:1323` | revise 轮 SceneOrchestrator 失败时 `break` 跳过 `aborted_with_blocking=True` → blocking 回滚/best-draft 保护不执行 | break 前置位 | 代码核验 |
| `api/styles.py:936` | `GET /api/styles/authors` 注册在 `/{style_id}` 之后 → 永远 422,作者聚合不可达 | 静态段前置 | 代码核验 |
| `services/outline_readiness.py:226` | 就绪检查按 `created_at.asc()` 取**最旧**书纲,写手取材取 `version.desc()` 最新 → 放行 A 版、写作 B 版 | 与读取侧统一排序 | 代码核验 |
| `models/__init__.py:17` | `CognitionRevision` 未被导出,只 `import app.models` 的上下文(autogenerate)会把它判成多余表并生成 `DROP TABLE` | 补 import 与 `__all__` | 全量回归 |
| `services/model_router.py:1789` | `generate_with_fallback` 把主路由的 model 名透传给**所有**备用端点(可能 404 或被静默映射) | 按端点解析默认模型(该方法仓内无调用方,仍修) | 代码核验 |
| `services/provider_response_guard.py:65`、`model_router.py:861/1019` | 中转在结束/拒绝帧发 `delta=null` → `.delta.content` 抛 `AttributeError`;此时已 yield 正文,不再重试 → 整章异常收场 | 三处改 `getattr(delta,"content",None)` | `test_audit_guards.py` 1 项 |
| `api/chapters.py:250` | 带正文的通用 PUT 在缺 `expected_content_text` 时抛的 `chapter_content_precondition_required` **无人接**,返回 500;同一语义在 `/content` 是 428 | 统一映射 428/409/400 | 代码核验 |

### P2(8 条)

| 位置 | 事实 | 处置 |
|---|---|---|
| `services/qdrant_store.py:168/192` | 建集合的"查-建"竞态:并发首次写时后到者抛 already-exists,被记成"向量写入失败" | create 失败后再 `get_collection` 视为已存在 |
| `tasks/__init__.py:75-105` | 8 个周期任务无 `expires` → 单轮超周期就叠加运行(小时级全量重抽取可打满 CPU) | 逐条补 `expires` |
| `tasks/analysis_tasks.py:16` | 置 `extracting` 后只有成功路径改回;崩溃则永久 extracting | 任务级兜底置 `error` + 错误信息 |
| `services/style_detection.py:196` | 16 维风格 JSON 只用 1024 token → 截断 → `json.loads` 失败 → 整段 LLM 维度静默降级 | 抬到 3000 并识别 `finish_reason=length` |
| `services/incremental_sync.py:94` | 每次改稿都付一次实体抽取,**结果只写日志**且恒返回 `synced=False` | 探针改为 `INCREMENTAL_SYNC_ENTITY_PROBE`(默认关) |
| `api/chapters.py:311` | `status` 无校验,可把空稿直接标 `completed`,绕过质量门 | `completed` 要求正文非空 |
| `api/changelog.py:69` | `total` 取的是"本页条数" → 前端第一页就判定到底 | 真实 `count()` |
| `api/ask_user.py:63/83` | 答复先 publish 后 commit;cancel 不看状态,可抹掉已答复记录 | 先 commit 再 publish;cancel 仅 pending |

### P3(3 条)

| 位置 | 事实 | 处置 |
|---|---|---|
| `main.py:142/186` | 两个连续 `except`(第二个不可达);`engine` 未绑定时关机 `UnboundLocalError` | 删重复分支;按需 import + 兜底 |
| `api/vector_store.py:38` | 每请求新建 `AsyncQdrantClient` 从不 close;删除丢弃返回值固定回"成功" | `asynccontextmanager` 统一关闭;回执带真实 status |
| `api/call_logs.py:27` | `limit` 只有上界,`?limit=-1` 直送 SQL → 500 | `ge=1` 与 `offset ge=0` |

### 明确不动(2 条,附理由)

| 位置 | 报告主张 | 不动的理由 |
|---|---|---|
| `services/model_router.py:900` | 非流式 `attempts=1` 让 `LLM_NONSTREAM_TASKS=*` 下所有任务单发 | **有意设计**:真非流式单发是为了不叠加 `LLM_NONSTREAM_TIMEOUT_SECONDS`;重试由 tier fallback / 任务级负责。改成 2 会被 `test_force_internal_stream::test_true_nonstream_empty_burn_raises_retryable` 明确判为回归(实测复现该失败)。保留说明性注释 |
| `services/style_profile_resolver.py:41` | 服务内部 `commit()` 破坏调用方事务 | 两个调用方(`reference_ingestor`、`book_tasks`)都在自己 commit 之后调用、且之后不再提交会话,改成 flush 会让 profile 行在 Celery 里被回滚 —— 换来的风险大于收益 |

### 复核后判为不成立(1 条)

- `services/outline_consistency_gate.py` 的《神裔》硬锚点:报告称为"跨项目串味、任何项目都可能被判错"。
  实测该分支由 `_is_shenyi_payload()` 内容门控(**标题含"神裔"** 或 **entity_registry 同时含那三个专名**),
  不会命中无关项目;仅"正文里提到《神裔》"这一条过宽,影响面小。**未改**,留作产品口径讨论。

## 十、本轮回归

| 项 | 结果 |
|---|---|
| 后端全目录离线 | 见最终一轮:`pytest --offline -q` 结果记录在本节下方"最终一轮" |
| 新增用例 | `test_audit_guards.py` 由 18 → **26** 项(配额 2、零向量 1、null delta 1;第三批:脱敏 1、命中率 1、质量分"未评成" 1、提取失败抛错 1) |
| 迁移 | 未新增;`alembic heads` 仍为单 head |

### 最终一轮

<!--FINAL-RESULT-->

| 项 | 结果 |
|---|---|
| 命令 | `cd backend && .venv/bin/python -m pytest --offline -q -p no:cacheprovider tests` |
| 结果 | **5129 passed / 762 skipped / 0 failed**(274.59s,第三批修复 + 4 项新用例之后) |
| 语法 | `app/**/*.py` + `scripts/**/*.py` + `tests/*.py` 全量 `ast.parse` 通过 |
| 迁移 | `alembic heads` = **单 head `a1001961`**,本轮无新增迁移 |
| 中途修掉的回归 | ① `attempts=2` 打破 `test_true_nonstream_empty_burn_raises_retryable` → 已回退并写注释;② `style_detection` 编辑多出一个 `)` → 收集期 SyntaxError,已修 |

### 附:真实模型长跑的并发问题

`scripts/ops/run_thinking_arm_after_gemini.sh` 在 gemini 长跑未跑满(90/100)时就起了思考臂,
两个 harness 同时打同一网关 —— 这正是之前打崩网关的形态。已 `kill` 思考臂与看守脚本,
gemini 继续跑满;思考臂目录 `docs/evaluation/arm-dsv4-thinking-20` 只落了 1 章,稍后用
`--resume --ids` 单独续跑。

## 十一、第三批(迟到的两份分区报告)追加修复

报告来自 `foreshadow/arc/compass` 与 `observability/production/quality` 两个分区,外加一份
`models/usage` 复核。其中 4 条在报告到达前已修完(`record_usage` 加锁、`crypto` 抛错、
`llm_call_logger` 按身份归属、`tool_registry` 的 `project_id=""`、`strand_tracker` 复用会话),
其余 8 条本轮修掉:

| 位置 | 事实 | 处置 |
|---|---|---|
| `services/quality_scorer.py:121` | 解析失败返回全 0 分,与"真的低分"不可区分 → `api/knowledge.py` 把参考书永久标 `low_quality`,禁止风格学习 | 加 `available` 标志;只有评估跑成才改状态 |
| `services/feature_extractor.py:129` | 失败时返回 `PlotFeatures(summary=text[:100])`,把正文切片伪装成提取结果,调用方随即置 `plot_extracted=1` | 改抛 `PlotExtractionError`,保留未提取状态供重试 |
| `observability/logging.py:63` | 字符串只脱敏 `Bearer xxx`,`,?api_key=`/`?token=` 原样进日志与 Sentry(其 `query_string`/`data` 走的就是 `redact`) | 新增查询参数脱敏正则,两条路径同时生效 |
| `observability/sentry_init.py:42` | `float(os.environ["SENTRY_TRACES_SAMPLE_RATE"])` 在 try 外,写错即启动崩 | 解析失败降级 0.05 并告警 |
| `observability/sentry_init.py:106` | 注释写"prefer over-redaction",实际 return 未脱敏 event | 脱敏自身出错时丢弃该事件(`return None`) |
| `services/usage_summary.py:88` | `cached_input_tokens > input_tokens` 时 `/usage` 显示 >100% 命中率 | 分子夹到 ≤ 分母 |
| `services/title_quality_checker.py:231` | 模型返回顶层数组时 `parsed.get` 抛 `AttributeError`,坏章名整批保持原样 | 非 dict 直接返回 `{}` 并告警 |
| `services/foreshadow_manager.py:315` | 兑现核查不传 `_log_meta` → router 只记 Prometheus、不写 `llm_call_logs`,每章至多 `ceil(未兑现/8)` 次核查的 token 在 `/usage` 与每章预算里完全不可见 | 补 `_log_meta`(project_id + task_type) |
| `services/compass_service.py:290` | 卷纲 `json.dumps(...)[:4000]` 把 JSON 从中间切断,模型在残缺对象上更新 `open_threads`/`scale_hint` | 超长改"字段降级"(`_drop_chapter_detail`,丢章级细纲保卷级),送模的始终是合法 JSON |

### 本轮明确不动(附理由)

| 位置 | 主张 | 理由 |
|---|---|---|
| `services/model_router.py:1613` | 带 `_log_meta` 的流式生成从开流到结束独占一个池连接 | 属实,但要改成"流完再开短会话写日志"会重排 usage 采集与 `log_llm_call` 的上下文管理器;当前是单用户桌面部署(实测并发流 <5,池 10+20),先记录不重构 |
| `services/foreshadow_review_worker.py:73` | 核查期间连接长期 idle in transaction | 同属"长 IO 持会话",改造需把 manager 的会话边界整体重划;单用户场景未见连接耗尽 |
| `services/logic_critic.py:181` | 短稿返回 `available=True, clean=True` | 是有意的"超短稿不耗调用";长度门在更早的质量闸门里已先行拦截 |
| `services/compass_service.py:176` | `save_compass` 内部 `commit()` | 两个调用方都持独立会话,改成 flush 会让 compass 在异常路径被静默回滚 |
| `models/project.py:50` | 级联无 `passive_deletes=True`,`purge=true` 会把全部版本正文载入内存 | 需要与迁移里的 `ON DELETE CASCADE` 逐一对齐后再改,属独立工单 |
| `services/writing_engine_seed.py:130` | 种子文案与正则有多处错字("霖的一下""小钙子"等) | 是数据修正,且种子只插不改 → 已有库不会更新;应单独做一次"种子修订 + 回填脚本" |
| `services/prompt_cache.py:337` | `_ensure_flusher` 无锁 | 该函数是同步的、在单线程事件循环里不会重入,多 flusher 需要多线程同时首次调用,当前无此路径 |
| `models/pipeline.py:59` | `pipeline_chapter_status` 缺 `(pipeline_id, chapter_id)` 唯一键 | 需要新迁移 + 去重;当前靠服务层假设,无实测重复写入 |