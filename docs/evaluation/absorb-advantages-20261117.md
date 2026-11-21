# 吸收外部优点:用量可见性、修文硬判据、伏笔缺铺垫、无嵌入召回(2026-11-17)

来源三处,逐条落到代码与实测:

| 来源 | 借的是什么 | 本仓库落点 |
|---|---|---|
| `ExplosiveCoderflome/AI-Novel-Writing-Assistant` | 用量页 + 单章 token 上限 + 缓存口径;`detectNewSevereProseFindings`(修文不得引入新的严重问题);`foreshadow_missing_setup` 冲突类型 | 新 `usage_summary` / `api/usage` / `/usage` 页 / `chapter_budget` 闸门;`chapter_quality_gate._new_severe_findings`;`foreshadow_setup_check` |
| `Zleap-AI/SAG-Wiki`(SAG2) | **无嵌入、无 LLM** 的词法证据打分召回 | 新 `lexical_cognition_rank`,接进 `cognition_retrieval` 的降级路径 + 可选混合重排 |
| `skygazer42/MimirQ` | "换 embedding 必须重建索引"、"引用 trace" 这两条工程纪律 | 本批未新增代码(索引回填与 space 绑定此前已落);BM25 混合的重排部分由 SAG2 轨道顺带实现,默认关闭 |

---

## 1. 用量可见性

### 1.1 数据层

- `llm_call_logs` 新增两列(两条新迁移,均已在本机真实 PG 上 `upgrade` 通过并 `alembic_sync` 复检为 `noop`):
  - `a1001960` → `cached_input_tokens`(缓存命中 token,`input_tokens` 的子集)
  - `a1001961` → `usage_estimated`(这条 token 是估算的)
- 缓存命中数从 provider 层经 `usage_box` 侧信道流入 `log_llm_call`;`prompt_tokens_details.cached_tokens`(OpenAI 形状)与 Anthropic 的 `cache_read` 都已接。
- 迁移链仍是**单 head**:`a1001959 → a1001960 → a1001961`;`scripts/ops/alembic_sync.py` 的 `SCHEMA_PROBES` 已补两行探针。

### 1.2 聚合 API

`GET /api/usage/projects/{id}/summary?group_by=chapter|task_type|model|day&days=30[&chapter_id=]`
`GET /api/usage/chapters/{id}/budget?limit_tokens=100000`

- 聚合全部下推为 SQL(`count()` + 三个 `coalesce(sum())`),**只选 token 列与分组键**,不读 `messages_json` / `response_text`(有测试断言编译后 SQL 的选列)。
- 真实 PG 实测(评估库,100 章项目):

```
TOTALS {"calls":591,"input_tokens":100000,"cached_input_tokens":20000,"output_tokens":15000,
        "total_tokens":115000,"cache_hit_rate":0.2,"estimated_calls":1,"estimated_tokens":70000}
GROUP  scene_writer: calls=165 total=70000 estimated_calls=1
       rewrite:      calls=1   total=45000 estimated_calls=0
```

### 1.3 每章预算闸门

- `CHAPTER_TOKEN_BUDGET_DEFAULT`(默认 **0 = 关闭**,不改变现有行为);`CHAPTER_TOKEN_BUDGET_TOKENS` 可覆盖。
- 位置:`POST /api/generate/chapter` 预检。只拦"再次生成一个**已存在**的章",新章不拦;超限不开始流式生成,返回 SSE 事件 `chapter_budget_exceeded`(带 `used/limit/remaining/by_step`),已保存正文与章状态不变。
- 真实 PG 实测:构造两行调用后 `used=115000, exceeded=True, calls=7`,删除后 `used=0, exceeded=False`。

### 1.4 前端 `/usage`

- 总览 5 卡(总/输入/输出/缓存命中率/调用次数)、分组切换(章节/步骤/模型/按天)、总 Tokens 占比条、单章预算卡。
- 三条诚实性约束:缓存命中率在 `cached_input_tokens=0` 时显示**未记录**而不是 `0.0%`;`estimated_calls>0` 时黄色横幅说明"供应商未回传用量,已按字符数估算,费用以厂商账单为准";估算字段缺省时视为 0(向后兼容)。
- 验证:`npx tsc --noEmit` 0 错误、`vitest` 13/13、`npm run test:run` 614 通过、`next build` 通过且产物含 `/usage`。

### 1.5 顺手修掉的真问题:主模型 usage 全程为 0

实测:`gemini-3.1-pro` 经中转 `http://127.0.0.1:8317` **即使传 `stream_options={"include_usage": true}` 也不回传 usage**;`deepseek-v4-flash` 正常。后果是 100 章长跑的 2,664 条调用日志 token 全是 0 —— 用量页在主模型上会全显示 0,比没有更糟。

- 修法:供应商 input/output **双零且输出非空**时按字符数估算(`TOKEN_ESTIMATE_CHARS_PER_TOKEN`,默认 1.5;实测中文 1.43–1.48,取 1.5 略低估),写入 `usage_box` 并置 `usage_estimated=True`。
- **绝不污染真实语义**:`GenerationResult.usage` 与截断判定(`output_tokens >= max_tokens*0.95`)仍只用真实值,估算只在日志/聚合层(有测试专门断言估算路径下 `result.usage` 仍为 0)。
- 真实探测:`gemini-3.1-pro` 流式 27 字 → `box={'input_tokens':15,'output_tokens':18,'usage_estimated':True}`;同模型**非流式**上游有 usage → `(5,4)` 未被覆盖;`deepseek-v4-flash` 流式 → `(18,6)` 无估算键。

---

## 2. 修文"不得引入新的严重问题"

- 分层:`critical = {story_bible_leakage_count, meta_structure_leakage_count}`;`high = {limited_pov_leak_count, mundane_logic_violation_count, resource_continuity_count, action_causality_count}`。
- `_new_severe_findings(original, candidate)` 只在**候选 > 原稿**时计入;`_candidate_is_acceptable` 在既有全部检查之后新增拒绝分支,`reason="new_severe:critical:..."`。
- 关键细节:传入的是**原稿报告**(`initial_report`),不是滚动中的 `current_report` —— 否则"第二轮修回仍差于原稿"的稿子会被误拒(有专门回归锁死这一点)。
- 15 项新测试 + 8 个既有引用文件的 174 项回归全绿。

---

## 3. 伏笔缺铺垫(`foreshadow_missing_setup`)

- `check_chapter_foreshadow_setup` 只读:读本章正文(单列)+ 本章自己的计划条目(`chapters.outline_json['foreshadows_planted']` / 卷纲 `foreshadows.planted` 解析到本章的条目),判定关键词是否在正文中出现,命中给出前后各 30 字证据(≤120 字)。
- 关键词来源优先级 `description → hint → type`,功能字(`的了着过而或之其已又`)二次切分,长度 <2 丢弃,上限 8 个;`hint` 可能是 JSON,只取其中文片段;默认值 `type='伏笔'` 不作为锚点。
- 落库后发 SSE 事件 `foreshadow_setup_missing`(warning,**不阻断、不写库、不改状态**,`FORESHADOW_SETUP_CHECK_ENABLED` 默认开);另有只读 API `GET /api/quality/projects/{id}/chapters/{cid}/foreshadow-setup`。
- 真实 PG 实测(临时项目,验证后删除):

```
planned=2 grounded=1
  寄信的存根 → grounded=True  证据「…寄信的存根还压在字典下面…」
  青铜钥匙   → grounded=False missing
EVENT {"event":"foreshadow_setup_missing","status":"warning","planned":2,"grounded":1,"missing_count":1,...}
```

---

## 4. 无嵌入召回兜底(SAG2)

- 新模块 `lexical_cognition_rank`(纯标准库):CJK 字符二元组 + ASCII 整词 + BM25(k1=1.2,b=0.75)+ IDF + 长词命中加成(≥3 字,权重 0.5);排序确定性(同分按索引)。
- 接进 `cognition_retrieval`:
  - 降级开关打开且"所有异常叶子都是 `EmbeddingUnavailable`"→ **词法排序的 recall**(不再是"无排序台账");拿不到候选文本或词法自身出错才回落原行为;`COGNITION_RETRIEVAL_DEGRADED_TOTAL` 仍 +1。
  - 混合重排 `COGNITION_LEXICAL_BLEND`(0..1,**默认 0**):`final=(1-w)·minmax(cos)+w·minmax(lex)`,只在已检索到的候选内重排,不会把没召回的条目拉进来。
  - `w=0` 时逐字等价于现状(测试把 `rank` 打桩成抛异常仍通过)。
- 实测性能:2000 条 / 29 万字符 ≈ 0.15s;55 万字符 ≈ 0.24s。
- 42 项测试 + `-k cognition` 415 passed / 40 skipped。

---

## 5. 100 章长跑结果(本批完成)

`docs/evaluation/longrun-100-20261117`,`gemini-3.1-pro`,`--max-tokens 8000`,目标 1200 字/章。

```
章节 100 / 目标 1200 字
中位比 0.887 | 前 1/3 0.891 | 后 1/3 0.897
状态 {"ok": 95, "truncated": 4, "resumed_extract": 1}
非 ok 章节: [4, 9, 15, 53]
低于目标 70% 的章节: [31, 53, 72]
```

- **长程无衰减**(前 1/3 0.891 → 后 1/3 0.897),4 章截断集中在前期,后期没有变差。
- 检查器 findings 50 条分布:`word_below_target 30`、`time_progress_missing 8`、`unknown_character 6`、`time_of_day_missing 3`、`book_fact_conflict 2`、`pov_missing 1`。
- 两条读数:
  1. **篇幅系统性偏低约 11%**(中位 0.887),30 章跌破 85% 阈值。这不是漂移,是阈值策略:续写兜底只在"场景 < 85% 目标"时触发,而模型常年落在 0.85–1.0 区间,兜底几乎不介入。要改就得抬 `SCENE_LENGTH_MIN_RATIO`(例如 0.95),代价是更多一次生成。
  2. **`time_progress_missing` 8 章是真空缺**:大纲声明"次日清晨/一周后",正文里没有任何时间推进说法。场景合同有 `time_delta` 字段,但它没被注入成"必须在正文体现"的硬约束 —— 可以照 `numeric_pin_lines` 的做法把声明的相对时间钉进 user content。

> 说明:`frontend/src/__tests__/mobileSave.test.tsx` 有一条**既有 flaky**(`requires a clear volume target...`)。对照实验:加/不加本批的 i18n 键,各跑 2 次都是"1 过 1 挂",与本批改动无关;全量跑时偶发,单独跑多数通过。未在本批修。
---

## 6. 验证汇总

| 项 | 结果 |
|---|---|
| 后端全目录离线 | **4972 passed / 762 skipped / 0 failed**(254.13s;本轮新增约 115 项) |
| 新增测试 | 用量 22 · 修文硬判据 15 · 伏笔 12 · 词法召回 20(42 含既有)· 估算 33 · 预算闸门 13 |
| 迁移 | 单 head `a1001961`;两条新迁移在真实 PG `upgrade` 通过,`alembic_sync` → `noop` |
| 真实 PG 端到端 | 预算闸门 `used=115000 exceeded=True`;聚合 `estimated_calls=1`;伏笔 `planned=2 grounded=1 missing` |
| 前端 | `tsc` 0 错误 · `vitest` 13/13 · 全量 614 通过 · `next build` 通过(含 `/usage`) |
| 100 章长跑 | 100/100,无长程衰减 |

---

## 7. 未做 / 已知缺口 / 待决策

**缺口(已确认,未修)**

- ~~`ModelRouter.stream_by_route` 与 `AnthropicProvider.generate_stream` 仍记 `0/0`~~ → **2026-11-17 当天补齐**:两条路径都会把 provider 报的用量写进 `usage_box` 并回填日志上下文;Anthropic 流式口径与非流式一致(input = 未缓存 + 缓存写入 + 缓存命中)。provider 不报用量时仍记 0(不编造),`tests/test_stream_by_route_usage.py` 5 项覆盖。
- 历史 2,664 条零 token 日志**不回填**,只对改后新调用生效。
- Prometheus `llm_token_total` 在非流式路径仍读真实 usage,gemini 这类"流式无 usage"的调用指标仍是 0(DB/页面已修)。
- `usage_quotas` 会吃进估算值,未标注"含估算"。
- 伏笔检查只覆盖"本章自己的计划",不覆盖"该回收未回收";`hint` 常是**回收条件**而非铺垫,只由 `hint` 命中的条目应视为弱命中(当前未区分)。

**待决策(需要口径而非代码)**

- 篇幅阈值:`SCENE_LENGTH_MIN_RATIO` 是否从 0.85 抬到 0.95(更贴目标 vs 多一次生成)。
- `COGNITION_LEXICAL_BLEND` 是否默认开(开了会改变既有召回排序,需要一次 A/B 才有依据)。
- 结构感知切块(MimirQ 那条):`chapter_chunker` 现在是"段落组 + 句边界",不含场景边界;要做需要落库时记录场景切分。

**未验证**

- `/usage` 页没有对着**运行中的后端**做浏览器实测(后端接口用 ASGI + 真实 PG 验证过,页面用 mock 验证过)。
- 交叉盲评未跑在长跑样本上(`cross_judge` 需要 case 形态,长跑产物是章节 txt)。

---

## 8. 本批文件

新增:`app/services/{usage_summary,chapter_budget,lexical_cognition_rank,foreshadow_setup_check}.py`、`app/api/usage.py`、`alembic/versions/{a1001960,a1001961}*.py`、`frontend/src/app/usage/page.tsx`、`frontend/src/lib/usageTypes.ts`
修改:`app/{config,main}.py`、`app/models/call_log.py`、`app/api/{generate,quality}.py`、`app/services/{llm_call_logger,model_router,chapter_quality_gate,cognition_retrieval}.py`、`scripts/ops/alembic_sync.py`、`frontend/src/{components/Navbar.tsx,lib/i18n/messages.ts}`
测试:`tests/test_{usage_summary,usage_estimation,chapter_budget_gate,chapter_quality_gate_new_severe,foreshadow_setup_check,lexical_cognition_rank}.py`、`frontend/src/__tests__/usagePage.test.tsx`