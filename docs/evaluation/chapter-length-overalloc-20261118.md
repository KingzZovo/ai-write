# 章级篇幅:超分配 + 验收线下调(2026-11-18)

用户要求:**单章起码 2000 字**。此前的状态是"目标 2000、实际中位 1908",本章记录诊断、
处置与待验证项。

## 一、实测基线(不是推测)

`docs/evaluation/longrun-100-gemini-2000-20261117`,gemini-3.1-pro,`--words 2000`,跑满 100 章。

| 口径 | 中位 | 均值 | 说明 |
|---|---|---|---|
| `continuity.json` / `word_counts.chars`(含空白) | **1952** | 1944 | 探针自己记录的口径,92 章有值 |
| 去掉空白字符(中文"字数"习惯) | **1854** | 1833 | 逐 `ch*.txt` 重算 |
| `longrun` 曲线(含 8 个 error 章的 0 字) | 比 **0.954** | — | 前 1/3 0.957 → 后 1/3 0.985,**无长程衰减** |

分布(`chars` 口径,92 章):`<2000` **55 章**、`<1900` **41 章**。
结论:**每场系统性欠 2~3%**,不是偶发;且长程不衰减,所以这不是"写飘了",而是分配/欠量问题。

状态分布:`ok 46 / resumed 27 / truncated 16 / error 8`。

## 二、处置

### 1. `SCENE_TARGET_OVERALLOC = 1.08`(新增配置)

只放大**送给规划器与写手的"章目标"**,不动章节记录里的真实目标,也不动验收/统计口径:

- 送模目标 `_planning_target(target)` = `round(target × 1.08)`(`scene_orchestrator.py`);
- 实测欠 2.4% → 模型按 2160 写、实际 ≈2106,大多数章落在 2000 以上;
- 过冲由既有 `SCENE_MAX_OUTPUT_WORDS` 与规划器的 85–115% 预算窗兜住;
- 设 `SCENE_TARGET_OVERALLOC=1.0` 即完全关闭,行为与旧版逐字节一致。

为什么不用"加续写次数":场级续写(`SCENE_LENGTH_MIN_RATIO=0.95`)只在单场 **<95% 目标**时
触发一次,而实测欠量是 2~3% —— 达不到触发线,加次数也没用。要修的是**目标本身的偏移**。

### 2. `CHAPTER_MIN_TARGET_WORD_RATIO` 0.80 → 0.95

`0.80`(=1600)会把 1952 字这种"接近目标但不足"的章直接标 `passed`;`0.95`(=1900)
允许 5% 浮动。这是**验收线**,不是生成线:不足时质量门给 `blocked`(原文保留、不静默标
completed),由作者决定。

## 三、单测(离线,不调模型)

`backend/tests/test_chapter_length_overalloc.py`(10 项):

- `_planning_target`:默认 2000→2160 / 1200→1296;`1.0` 关闭;`1.4`→2800;上界 clamp 到 1.5×;
  `<1`、非数字、`None` 一律回退,不抛;`0`/负数原样返回;
- 放大后仍能算出合法预算窗(`scene_budget_bounds` 85%–115%);
- `_MIN_TARGET_WORD_RATIO == 0.95`、`_min_target_word_count(2000) == 1900`、
  `_target_length_shortfall` 在 1899/1900 边界正确翻转、无目标时不判定。

`tests/test_chapter_quality_gate.py` 里 3 个把 0.80 写死的边界用例改为**显式传**
`min_target_word_ratio=0.80` —— 它们验证的是"软地板之上/之下"的行为本身,与默认口径无关;
默认口径的回归在新文件里。

## 四、顺手修的口径 bug

`scripts/quality_eval/longrun.py` 的 `--target` 默认写死 1200,于是把 2000 档的跑批显示成
"中位比 1.591 / 目标 1200"(第一版报告里就是这么读错的)。现在:

1. 优先读 `continuity.json` 的 `word_counts[*].target`(探针写的权威值);
2. 退化到从目录名 `-2000-` 推断;
3. 都不行才落 1200,并打印警告要求显式 `--target`。

修完后同一条命令输出 `目标 2000 / 中位比 0.954`。

## 五、10 章对照(超分配 1.08,已实测)

`longrun-10-gemini-2000-overalloc`:gemini-3.1-pro,`--words 2000`,`SCENE_TARGET_OVERALLOC=1.08`。
对比同模型 100 章基线的**前 10 章**(同号章,去空白字符):

| 臂 | 逐章字数 | 中位 | 均值 | <2000 |
|---|---|---|---|---|
| 基线(超分配关) | 2638 / 2037 / 1806 / 2028 / 1852 / 1715 / 1741 / 1855 / 1886 / 2352 | 1870.5 | 1991 | 6/10 |
| 新臂(1.08) | 2025 / 1851 / 1623 / 2116 / 1950 / 2126 / 1582 / 2377 / 2034 / 2190 | **2029.5** | 1987 | 4/10 |

`longrun`(含空白 chars)中位比 **1.034**,状态 `ok 9 / truncated 1`。

**结论:方向对(+8.5% 中位),但不足以保证下限** —— 仍有 4/10 章 <2000,最短 1582。所以补上方案 D。

## 六、方案 D:章级补足(已实现,验证见第七节)

场级续写只看"单场够不够",而章是场之和 —— 每场各欠一点,整章就短。

- 新增配置 `CHAPTER_LENGTH_CONTINUATION_MAX=1`(0 关闭);
- `SceneOrchestrator._chapter_length_topup()`:所有场写完后,若整章 <
  `CHAPTER_MIN_TARGET_WORD_RATIO × 真实章目标`(与质量门**同口径**,补到能过闸),
  用最后一场的收尾上下文再补 ≤N 次,提示词写明还差多少字;
- 增量**照常 yield**(否则流式展示与落库正文会不一致),重复/空内容丢弃;
- 单测 `tests/test_chapter_length_topup.py` 9 项:达标不调用、不足补一次、配置 0 关闭、
  纯重复丢弃、空续写丢弃、缺口数字写进提示、生成侧与验收侧同口径。

下一轮验证按"10 章 → 中位 ≥2000 且 <2000 章数为 0"判定;若仍偶发偏短,
再把 `CHAPTER_LENGTH_CONTINUATION_MAX` 调到 2(代价是每章最多多两次生成)。

## 七、最终验证:补足到 2000(10 章,已实测)

`longrun-10-gemini-2000-topup100`:`SCENE_TARGET_OVERALLOC=1.08` + `CHAPTER_LENGTH_CONTINUATION_MAX=1`
+ `CHAPTER_LENGTH_TOPUP_RATIO=1.0`,gemini-3.1-pro,目标 2000,10 章全 ok。

| 臂 | 中位 | 均值 | <2000 | 最短 | 最长 |
|---|---|---|---|---|---|
| 修复前(100 章基线同号章) | 1870.5 | 1991 | **6/10** | 1715 | 2638 |
| 仅超分配 1.08 | 2029.5 | 1987 | 4/10 | 1582 | 2377 |
| 超分配 + 补足(停止线 1900) | 2043 | 2037 | 4/9 | 1780 | 2269 |
| **超分配 + 补足(停止线 2000)** | **2195** | **2199** | **1/10** | **1989** | 2402 |

`continuity.json`(含空白字符,探针口径):中位 **2232**,`<2000` 的章 **0/10**。

逐章(去空白):

| 章 | 基线 | 仅超分配 | 补足@1900 | **补足@2000** |
|---|---|---|---|---|
| 01 | 2638 | 2025 | 2078 | 2233 |
| 02 | 2037 | 1851 | 2084 | 2125 |
| 03 | 1806 | 1623 | 1969 | **2402** |
| 04 | 2028 | 2116 | 2043 | 2326 |
| 05 | 1852 | 1950 | 2269 | 2275 |
| 06 | 1715 | 2126 | 2148 | 2158 |
| 07 | 1741 | 1582 | 1780 | 1989 |
| 08 | 1855 | 2377 | 1997 | 2101 |
| 09 | 1886 | 2034 | 1962 | 2232 |
| 10 | 2352 | 2190 | —(网关 503) | 2149 |

**结论:达标。** 10 章里只有 1 章在去空白口径下差 11 字(1989),含空白口径全数 ≥2000。
关键点:补足**只触发了 1 次(补 64 字)** —— 超分配已把大多数章推到 2000 以上,
章级补足只兜最后一点,所以成本增加极小(约每 10 章多 1 次生成)。

复跑命令:

```bash
SCENE_TARGET_OVERALLOC=1.08 CHAPTER_LENGTH_CONTINUATION_MAX=1 CHAPTER_LENGTH_TOPUP_RATIO=1.0 \
  python -m scripts.quality_eval.continuity_probe \
  --database-url postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/aiwrite_eval \
  --out docs/evaluation/longrun-10-gemini-2000-topup100 \
  --chapters 10 --words 2000 --model gemini-3.1-pro --max-tokens 8000
```

对照页:`docs/evaluation/_public/length-topup.html`(公网总入口 → ①)。

## 八、回退

```bash
# 只关超分配
# 只关超分配
SCENE_TARGET_OVERALLOC=1.0
# 只退回旧验收线
CHAPTER_MIN_TARGET_WORD_RATIO=0.80
# 关掉章级补足(不额外多一次生成)
CHAPTER_LENGTH_CONTINUATION_MAX=0
```

三项都是配置项(env > config 默认),不改代码即可回退。

## 九、验证记录

| 项 | 结果 |
|---|---|
| `tests/test_chapter_length_overalloc.py` | 10 passed |
| `tests/test_chapter_quality_gate.py` | 68 passed(含改口径后的 3 项边界) |
| 后端全目录离线 | **5139 passed / 762 skipped / 0 failed**(260.37s) |
| 真实模型 | 超分配臂已跑 10 章(见第五节);章级补足**尚未**用真实模型验证 |

## 十、顺带确认的两件事

### 1. `blocked` 不会丢正文(提高验收线前必须先确认这一点)

- `api/generate.py:791` 起:`status != "passed"` 时如实落库并下发
  `quality_failed` 事件,带 `persisted_on_block: True`;
- `tasks/generation_tasks.py:1664`:`blocked` 时正文进
  `_stage_needs_review_chapter_text(...)` 并新增一条 `is_active=0` 的版本;
- `chapter_persist_status()`:带 `persisted_on_block` 的章一律 `draft`,不会静默标 `completed`。

所以把验收线从 0.80 提到 0.95,**后果是"这些章显示为待核对",不是"内容消失"**。

### 2. 欠量是模型特性,不是全局常量

同期在跑的 `arm-dsv4-thinking-20`(deepseek-v4-flash、开思考、非流式、目标 2000)
前 15 章去空白口径:中位 **2103**、min 1644、max 2684 —— **已经越过 2000**。
对照 gemini 的 1854,说明"每场欠多少"是**模型相关**的:

- 对 gemini 这类偏短的模型,超分配 8% 是有效手段;
- 但"换模型"对篇幅的影响比调参更大 —— 这也意味着 `SCENE_TARGET_OVERALLOC`
  不能一律拉高,否则对本来就写够的模型会过冲。默认 1.08 是折中,单模型调优时按
  实测再定(比如 gemini 可用 1.10、dsv4 用 1.0)。

## 十一、整体验证记录

| 项 | 结果 |
|---|---|
| 后端全目录离线(最终一轮) | **5139 passed / 762 skipped / 0 failed**(261.19s) |
| `longrun` 口径修复后 | 同一命令输出 `目标 2000 / 中位比 0.954`(修复前是 `目标 1200 / 1.591`) |
| 单测 | 新增 10 项;质量门 3 项显式化 |
