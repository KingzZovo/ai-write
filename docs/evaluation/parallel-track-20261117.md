# 并行推进批次(2026-11-17 下半场)

三路并行 + 两件工具 + 一次长跑启动。**含一个此前报告说错的事实更正。**

## A. 金额台账(按名目)

**新判定规则**:冲突 = 台账里**同名目**此前登记过、且本次数额不同;同名目同额、首次出现的名目、
以及计划里没有的**新引入**金额都不报。名目匹配保守:归一后完全一致,或共享 ≥2 个连续汉字,
并去掉"总额/单价/每套"这类分类词(否则"铺租总额"与"工钱总额"会只因共享"总额"被判同名目)。

这直接修掉实测的两类误报:ch08 大纲里的 820、ch18 早在 ch10 正文写过的 3000;
而 ch07"钉子 820 却写 840"的真阳性仍然报。

**我在其基础上补的关键一环**:台账必须随**同一次** `quality_gate` 落库。
核对发生在新版本行创建**之前**,原先的实现只能写"该章已有的最新版本行"——
后果是本章**首次**生成无处登记,整本书的首次遍历都不生效(后续章节永远没有可比对象)。
现在 `ChapterQualityGateResult` 带 `numeric_ledger` 字段并进 `to_safe_metadata()`,
与 `numeric_conflicts` 一起落库;写入器仍保留为兜底。

## B. embedding 不可用 → 可降级(不再阻断整章)

`cognition_retrieval` 新增 `EmbeddingUnavailable` 分类:`ALLOW_COGNITION_DEGRADED_CONTINUATION`
**打开时**,嵌入服务不可用(HTTP/传输/超时,即"所有异常叶子都是嵌入不可用")不再抛错,
改为返回**不含语义排序**的选择(仍带台账渲染)并记降级 warning;
`decision_changed` / `embedding_changed` / 任何 `ValueError` 数据不一致 / 向量库自身故障**仍然阻断**。
开关**默认值不变**,关闭时行为逐字不变(有测试钉住)。

这修的是实测两次的真故障:上游 NVIDIA embeddings 500 时,中段章直接写不出来。

**已知边界(有意保留)**:Qdrant 故障不算"嵌入不可用",仍阻断;降级后的选择在持久回执里
与"成功但零排序"不可区分(receipts 里没有标记)。

## C. 内置提示升级 + 迁移验证

### C1. 修掉一个严重缺陷:alembic **不是单头**

`a1001957→a1001958` 与新迁移 `a1001959` 都从 `a1001956` 分叉:

```
$ alembic heads
a1001958 (head)
a1001959 (head)
$ alembic upgrade head
ERROR [alembic.util.messaging] Multiple head revisions are present for given argument 'head'
```

**`alembic upgrade head` 完全不可用。** 已把 `a1001959.down_revision` 从 `a1001956` 改为
`a1001958`(该迁移从未在任何库应用过,改父节点安全),现在:

```
$ alembic heads
a1001959 (head)
$ alembic branches
(无输出 → 单一线性链)
```

> **更正**:此前报告里写的"a1001959 是单一 head"是**错的**,实际是双头。以上为更正后的实测。

### C2. 迁移在临时库上真的跑过一遍

临时库 `aiwrite_migtest`(127.0.0.1:55432,用完已 `DROP DATABASE`;未碰 `aiwrite_eval`):

- `alembic upgrade a1001959` → `exit=0`,行文为
  `Running upgrade a1001956 -> a1001959, 每角色每章允许多条状态观测…`;
- `\d character_states` 复核:新增 `status_digest`、旧 `uq_character_states_key` 消失、
  新 `uq_character_states_source (project_id, character_id, source_chapter_id, status_digest)` 在位;
- 行为:同章两条不同 digest → `INSERT 0 2`;同源同 digest 重放 → 唯一键冲突(幂等);
  `status_digest IS NULL` 的历史行不受约束;
- `alembic downgrade -1` → `exit=0`,列与旧键恢复,`version_num=a1001956`。

**两个真实边界(未修)**:

1. 对"已有等价手工 DDL、alembic 仍认为在 a1001956"的库(评估库就是这种),
   同一个 upgrade **无法应用** → `DuplicateColumnError: column "status_digest" … already exists`。
2. 降级路径在 `created_at` 为 NULL 的同章重复行上会失败
   (`UniqueViolationError: could not create unique index "uq_character_states_key"`),
   要手工删掉那一行才能降级。建议加固。

### C3. `upgrade_builtins()`:内置提示终于能升级了

`seed_builtins()` 只补缺失的 task_type,所以改 `app/services/prompts/*.py` 的默认内容,
对**已经存在该 task_type 的库永远不生效**(实测:评估库 `scene_planner` 一直是 v1)。
新增 `PromptRegistry.upgrade_builtins()`,规则非破坏性:

- 库里活动行指纹 == 当前默认 → 不动;
- == **已发布过的旧默认**指纹(运维从未改过)→ 插入新 version 行(内容为当前默认,
  继承路由绑定)并把旧行置 inactive;
- 其它内容(运维改过)→ **完全不动**,列入 `needs_review`;
- 无任何行 → 归 `seed_builtins`。

**我把它接进了启动路径**(`app/main.py`:先 seed、再 upgrade、有变化才 commit 并记日志)——
否则这个能力是死的:仓库里没有独立的 seed 端点,原实现没有任何生产调用点。

**已知边界**:`scene_planner`/`generation`/`scene_writer` 三条历史指纹取自 git 历史,
测试内无法自动核验;指纹只覆盖 `mode + system_prompt`,不含 `output_schema`
(只改过 schema 的自定义行会被判为"未改过"并激活当前默认,原行仍保留)。

## D. 交叉盲评工具(为"第二个模型盲评"准备)

新增 `scripts/quality_eval/cross_judge.py`:两个评审模型同批判一遍,然后逐条比对。
只统计**两方都判过**的条目(覆盖率差异不是分歧),输出一致率 + 分歧清单(带双方原句,
便于只把分歧交给真人)。5 项离线测试通过。

**尚未实跑**:长跑正在占用网关,并发会互相拖慢。等长跑结束再跑。

## E. 100 章长跑 + 曲线工具

新增 `scripts/quality_eval/longrun.py`:逐章篇幅比、状态分布、实体/认知终态,
以及前 1/3 与后 1/3 的中位比对照 + 一行符号曲线(6 项离线测试)。

长跑已启动(自建项目,`gemini-3.1-pro`,`--max-consecutive-errors 5`,用 `setsid` 脱离工具会话):

| 时点 | 章节 | 中位比 | 前 1/3 | 后 1/3 | 状态 |
|---|---|---|---|---|---|
| 21/100 | 21 | — | 1.077 | 1.06 | `ok` 为主,2 章 `truncated` |

**目前没有衰减信号**(后 1/3 与前 1/3 持平),样本还太小。注意:探针只有 10 个手写章节设定,
第 11 章起按同一模板循环 → **剧情是循环的,这份曲线只能说明工程稳定性与篇幅趋势,
不能用于文学判断**。

## 验证

- 全量离线回归 **4859 passed / 762 skipped / 0 failed**(282.98s);
  其中本批新增 65 项(台账 26、降级 13、提示升级 10、交叉盲评 5、长跑曲线 6、我的台账落库 2 等)。
- 长跑仍在进行,不阻塞其它工作;跑完后用 `longrun.py` 出曲线,并跑 `cross_judge`。

## 仍未做的

- 交叉盲评实跑(等长跑);
- 台账的真实 PG 端到端(只用方言编译核对过 SQL,没连真库);
- 迁移降级的 `created_at IS NULL` 加固;
- 手工 DDL 库的 upgrade 路径(需要运维侧 `stamp` 策略);
- 降级路径的可观测性(回执里区分不出"降级"与"零排序")。
