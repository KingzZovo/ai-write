# 连续 24 章真实链 · 修复后(对照臂)

目录:`docs/evaluation/continuity-20261114-long24-pins/`

```bash
cd /root/ai-write/backend
/root/ai-write/.venv/bin/python -m scripts.quality_eval.continuity_probe \
  --database-url "postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/aiwrite_eval" \
  --out ../docs/evaluation/continuity-20261114-long24-pins --chapters 24 --limit 24
```

与基线同大纲、同模型、同温度。这一臂比基线多了四件事:

| # | 改动 | 性质 |
|---|---|---|
| 1 | `numeric_pins` 把大纲已定的金额注入写作上下文 | 产品 |
| 2 | `AT_LOCATION` 同章第二地点不再建边(记进 `alternate_locations`) | 产品 |
| 3 | 证据定位容忍全角/半角标点形式差异 | 产品 |
| 4 | 探针续跑时补跑提取(`resumed_extract`) | 测量装置 |

## 1. 两臂对照

| 指标 | 基线 | 本臂 |
|---|---|---|
| 章节 `status: ok` | 24/24 | 24/24 |
| 认知记录 completed | 24/24 | 24/24 |
| **实体投影 ok** | **22/24** | **23/24** |
| `AT_LOCATION` 唯一约束报错 | **2 次** | **0 次** |
| 字数中位比值 | 0.967 | 1.105 |
| 长度≥目标 80% | 19/24 | 20/24 |
| 检查器命中(已校准) | 18 | 4 |
| — `word_below_target` | 10 | 4 |
| — `time_progress_missing` | 3 | 0 |
| — `time_of_day_missing` | 1 | 0 |
| — `unknown_character` | 4 | 0 |

本臂唯一的实体失败是**测量装置的问题**,不是产品:

```
ch18 entity_error: RelationshipScopeError: relationship_scope_prompt_not_configured
```

探针只种了 5 个 prompt 资产,漏了 `relationship_scope`,而它不像别的任务那样走 tier 回退,
要求 `snapshot.endpoint_id` 非空。已修(`_PROMPT_TASKS` 补上,并把真实 endpoint id 传给
`_seed_prompts`),资产层已验证 `relationship_scope endpoint_id=SET`;
再跑一臂的端到端结果见 `continuity-20261114-long24-fixed/REVIEW.md`。

## 2. 确实有效的:`AT_LOCATION` 修复

基线在 ch11(周芸先医生办公室、后病房)、ch24 各丢一整章实体投影,本臂 24 章零报错,
包括基线出错的 ch11。

这是**机制性**结论,不是方差:旧写法在唯一约束下必然抛错,已用真实 Neo4j 复现
(旧写法抛 `ConstraintValidationFailed`,新写法一条边 + `alternate_locations: ['病房']`)。
回归测试:`tests/test_location_projection_guard.py`(结构守卫离线跑,连库用例在
`ENTITY_TEST_NEO4J_URI` 下跑)。

## 3. 无效的:把大纲已定的金额写进提示,并没有阻止模型算错

**基线的问题**(见 `long24-baseline/REVIEW.md` 第 4 节)在本臂**完整复现**:

| 章 | 正文 | 大纲 |
|---|---|---|
| ch07 | "扣除布料损耗和线本费,净落到手里的工钱刚好是**八百四十块**" | 没给数,只说"算出这批活能挣多少" |
| ch08 | "实打实拿到手的工钱是**八百二十块**" | 钉死"收下八百二" |

先确认注入确实生效:用运行期间数据库里的 ch08 大纲重放 `ContextPack`,

```
future_outlines 行: [VOL-1/CH-8; id=9bbd4462…]《第八章 校服的钱》: 时间:一周后;…校服交了,周芸收下八百二。…
pin 块: [CH-8] 校服交了,周芸收下八百二
        [CH-8] 铺租八百付掉后只剩二十
        [CH-8] 本场需完成:周芸算清收八百二、付八百、余二十
进入提示: True
```

也就是说,ch07 的写手**看到了**"第 8 章收下八百二",仍然自己编了"每套二十八块 × 三十套 = 840"。

结论:**提示里给数字不等于约束住数字**。这类"模型在大纲没给数的地方自造一个数、
与后文章节钉死的数冲突"的缺陷,靠提示注入解决不了(至少在本模型、本样本上测不出来)。

要真正解决需要另一条路,本批**没有做**:

1. 写完后按"名目"抽取已写下的金额,存成台账(需要能识别名目,不是纯正则能做好);
2. 与大纲里已钉死的数额比对;
3. 冲突时**重写**产生冲突的那一章(gate 现在在"正文合格但 B 类问题"时并不进入改写循环)。

第 1 步就是本批被删掉的那个检查——删是因为它的误报率接近 100%。所以这不是"再调调正则",
而是需要一个产品级的数字台账。

## 4. 无法归因的差异(诚实说明)

`time_progress_missing` 3→0、`time_of_day_missing` 1→0、`unknown_character` 4→0、
字数中位 0.967→1.105,这些**与本臂的四个改动没有机理上的因果关系**(`numeric_pins`
只管金额,不碰时间词与人名)。它们更可能是 run-to-run 方差(单 seed)。
不作为修复有效的证据,也不作为退步的证据。

## 5. 未验证

- 没有真人盲评。
- `relationship_scope` 的端到端修复未在本目录验证(见第 1 节,留给 fixed 臂)。
- 时间单调性、知情边界仍无检查。
