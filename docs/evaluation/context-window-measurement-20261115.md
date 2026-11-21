# 上下文窗口实测与校准(2026-11-16 修订)

> **本文件更正了 2026-11-15 版本的结论。** 那一版说"中文真实换算比是 4.00 汉字/token",
> 由此推出"平台把用量高估 2.67 倍、实际只用了窗口 1.2%"。**那个数字是错的**,原因见 §1。
> 结论随之改变:换算比本来就是对的,真正的问题是三处写死的窗口 + 预算本身偏小。

## 1. 换算比:1.43~1.48 汉字/token

实测方式:对 `http://127.0.0.1:8317`(`deepseek-chat`)发**真实章节正文**
(`docs/evaluation/continuity-20261114-long24-baseline/ch*.txt`),读 `usage.prompt_tokens`。

| 提示字数 | prompt_tokens | 汉字/token |
|---|---|---|
| 1,654 | 1,116 | 1.482 |
| 4,321 | 2,953 | 1.463 |
| 8,093 | 5,583 | 1.450 |
| 14,248 | 9,945 | 1.433 |
| 20,000 | 13,936 | 1.435 |
| 40,000 | 27,892 | 1.434 |

线性成立。另外测了两条对照:

- 高度重复的填充文本(`"汉字" * 7124`,14,248 字)→ 7,128 token = **2.00 字/token**;
- 罕见 CJK 码位(`U+4E00` 起随机 4000 个)→ 6,709 token = **0.596 字/token**。

即:**统计口径(什么文本)决定这个比值,从 0.6 到 2.0 都能测出来。**
上一版的"4.00"不在任何合理口径内,已作废;不要据此调大常量。

结论:`context_pack.CHARS_PER_TOKEN = 1.5` **本来就是对的**(比实测 1.43~1.48 略偏乐观)。
因此:

- 预算 9500 "token" ≈ 14,250 字 ≈ **9,900 真实 token**(不是旧版说的 3,562);
- 第 18 章装配 9,544 字 ≈ **6,600 真实 token**(不是旧版说的 2,386)。

旧版那条"网关能吃 ≥ 200,000 tokens(800,000 汉字一次通过,`prompt_tokens=200002`)"
用的是同一个 4.0 口径,**本口径下不成立,已标为未复核**;本轮没有重测窗口上限。

## 2. 真正的问题:写死的窗口,不是换算比

| 材料 | 旧实现 | 现在 |
|---|---|---|
| 前章正文 | SQL 层 `right(content_text, 3500)` | `CONTEXT_PREV_CHAPTER_TAIL_CHARS`(默认 3500) |
| 前文摘要 | 查询 `.limit(5)` | `CONTEXT_RECENT_SUMMARY_CHAPTERS`(默认 8) |
| 后文章纲 | 查询 `.limit(10)` | `CONTEXT_FUTURE_OUTLINE_CHAPTERS`(默认 20)+ **块字符上限** `CONTEXT_FUTURE_OUTLINE_MAX_CHARS`(默认 3000) |
| 全书大纲 | 超 2200 字就 `前 1500 + "(中部省略)" + 后 500` | `CONTEXT_BOOK_OUTLINE_HEAD_CHARS` / `_TAIL_CHARS`(默认 4000 / 2000) |
| 写手预算 | `CONTEXT_PACK_TOKEN_BUDGET = 9500` | **20000**(≈30,000 字 ≈20,000 真实 token) |

## 3. 真实 24 章项目逐块 trace:旧预算丢掉的是"后续走向"

`allocate_context` 对**超出本层额度的块是整块省略,不做截断**
(`status='omitted_budget'`);L1 只分到剩余额度的 40%,所以"加预算"和"加块大小"必须一起动。

直接把 `ContextPackBuilder.build` 指向**真实 24 章项目**
(`docs/evaluation/continuity-20261114-long24-baseline`,只读),对同一章分别按旧、新预算渲染:

| 章节 | 预算 | prompt 字数 | `future_outlines` | `book_outline` |
|---|---|---|---|---|
| CH7 | 9500(旧) | 8,853 | **omitted_budget**(丢 3,013 字) | included(101) |
| CH7 | 20000(新默认) | 11,868 | **included 3,013** | included(101) |
| CH18 | 9500 | 9,187 | **omitted_budget**(丢 1,086 字) | included(101) |
| CH18 | 20000 | 10,275 | **included 1,086** | included(101) |
| CH24(末章) | 两者相同 | 9,150 | 无后文,不适用 | included(101) |

**结论(这版把上一版的合成结论纠正了)**:

- 真实书里被整块丢掉的是 **`future_outlines`**——写手在旧预算下**完全看不到后续走向**,
  这正是"长篇越写越飘"的直接来源。抬高预算把它救回来了。
- 上一版说"全书大纲会被整块丢掉"是**我构造的 2000 字大纲造成的**,真实项目的全书大纲
  只有 101 字、从来没被丢过。合成结论不能当真实结论用。
- 顺带把该块自身加了一个字符上限(见 §2 表最后一行):列表按由近到远排列,
  超限就**截尾保近**,而不是等 L1 额度不够时被整块丢。

**测量装置的边界(必须说清)**:这个 harness 只能拿到 `ContextPackBuilder` 的产物,
`resolve_cognition` 因会话里已有未提交事务而失败(`character_cognition_transaction_open`),
所以 `cognition` 块用的是 pack 自身的渲染值 1,291 字 —— 与之前记录的 ch18 认知块(1,283)
同量级,故认为代表真实装配,但**不是**通过完整生成流程取的。
另外该项目的 Neo4j `WorldRule` 节点数为 0(不是故障);第一次跑失败是因为 harness
忘了 `init_neo4j()`,不是产品问题。

## 4. 本轮的代码改动

- `context_pack.CHARS_PER_TOKEN`:维持 `1.5`,补上实测依据与"不要改成 4.0"的说明。
- `config.py`:新增 6 个窗口开关(见 §2);`CONTEXT_PACK_TOKEN_BUDGET` 9500 → **20000**。
- `context_pack._build_proximity`:改为读这些开关;补上本文件其余位置同款的
  `from app.config import settings` 防御式导入。
- `context_pack.to_system_prompt`:`future_outlines` 渲染块按
  `CONTEXT_FUTURE_OUTLINE_MAX_CHARS` 截尾保近(只截渲染块,不动
  `self.future_outlines`,以免 `numeric_pins` 少读到金额)。
- `tests/test_context_window_config.py` 7 项:钉住"(a) 换算比在实测区间内、
  (b) required 块永不被丢、(c) 默认预算下低优先块 included / 旧预算 omitted、
  (d) 后文章纲截尾保近且金额钉子仍读到远章金额"。

## 5. 必须说清的边界

- **已改为真实书实测**(§3)。原版说"脱离生成流程会失败:
  `WorldRuleSourceError: 当前世界规则暂时无法读取`"——那是我的 harness 忘了调
  `init_neo4j()`,不是产品问题;该项目 Neo4j 里 `WorldRule` 节点数为 0。
  现在能直接对真实 24 章项目取逐块 trace。
- **仍未走完整生成流程**:`resolve_cognition` 在 harness 里失败(会话已有未提交事务),
  `cognition` 块用的是 pack 自身渲染值(1,291 字)。要严格取证应跑一次真实生成、
  读日志里的 `ContextPack budget=... trace=...`。
- **加长上下文不会修掉工钱漂移。** 第 7 章自造 840、第 8 章写 820 那条,
  约束当时**就在提示里**(更准确地说:根因是计划里根本没定这个数,
  见 [numeric-drift-instrument-20261116.md](/root/ai-write/docs/evaluation/numeric-drift-instrument-20261116.md))。那是指令遵循/计划缺口,不是窗口大小问题。
- **换算比随模型/网关变化。** 以上都是 `deepseek-chat` + 本机网关的数字;
  换模型要重测,别把 1.43 当普适常数用。
- 成本/延迟随预算上升:输入从 ~6.6k 增到约 2 倍量级,未做成本核算。
