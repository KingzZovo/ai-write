# 未闭合项收口(2026-11-17)

上一批列的 5 件事,本批按 0 → 1 → 2 → 3 → 4 执行。**逐项结论先摆出来,包括没做到的。**

| # | 项 | 结论 | 证据 |
|---|---|---|---|
| 0 | 金额纯函数下沉产品层 | **达成** | `numeric_ledger.py`;脚本只 import;测试断言同一函数对象 |
| 1 | 写后金额核对 warning(非阻断) | **达成** | 7 项单测 + 真实链日志显示"抽取失败→跳过→章节仍 `ok`" |
| 2 | `compare_arms --amounts` | **达成** | 6 臂实跑,见 §3 表 |
| 3 | 真实生成流程取证 | **部分达成** | 真实生成日志已取到(§4);**中段章未取到**,原因见 §4 末 |
| 4 | 计划阶段定数 | **大部分达成** | 11 项单测 + 真实规划模型已填 `money_totals`;两章真链未验证 |
| — | 全量离线回归 | **达成** | **4780 passed / 762 skipped / 0 failed**(338.09s),本批新增 27 项 |

---

## 1. 金额纯函数下沉(步骤 0)

新增 [numeric_ledger.py](/root/ai-write/backend/app/services/numeric_ledger.py):
`parse_amount` / `parse_amounts` / `looks_like_total` / `totals_for` / `planned_amounts_from_text` /
`conflicts` / `drift_against`,以及模型提示 `AMOUNT_EXTRACTION_SYSTEM`。

[scripts/quality_eval/amounts.py](/root/ai-write/backend/scripts/quality_eval/amounts.py) 改为 import,
不再有第二份实现;测试直接断言 `amounts.parse_amount is ledger.parse_amount` 等 4 个函数对象。
产品层不许依赖脚本层,所以提示词也搬到了产品层。

## 2. 写后金额核对(步骤 1)

新增 [numeric_check.py](/root/ai-write/backend/app/services/numeric_check.py);
挂钩在 [apply_chapter_quality_gate](/root/ai-write/backend/app/services/chapter_quality_gate.py:598)
的**外层包装**上 —— 三个落库路径(桌面流式 / 后台任务 / 管线)都经过它,一次覆盖。

设计约束(都有对应测试):

- **只在计划里确实有金额时才调用模型**:计划为空 → 0 次调用(`test_no_model_call_at_all_without_a_plan`)。
- **严格非阻断**:抽取抛错 / 返回空 / 计划读不出来 → 只 warning 日志,`status` 不变、正文不改、不抛。
- `numeric_conflicts` 字段进 `to_safe_metadata()`,随 `ChapterVersion.metadata_json["quality_gate"]` 落库。
- 会话约定跟随既有实现:调用方给了 `db` 就用,**没给就自己开一个短会话**
  (`generate.py` 的流式路径传的就是 `db=None`,不自己开会话这里永远不生效)。

**真实链证据**(新项目 ch1):

```
amount check: extraction returned nothing; skipping (empty != no amounts)
{"index": 1, ..., "status": "ok"}
```

即核对确实跑了、**没有阻断**、并且把"抽取失败"和"没有金额"分开了。但这也暴露了价值上的短板:
那次抽取返回空,**warning 就没产生**。抽取可靠性是这条链的薄弱环节 ——
脚本层用 `deepseek-chat`(3000–6000 token)稳定,而产品路径走
`task_type="evaluation"` 的路由回退,实测第一次就空了。要真正起作用,应把该任务路由显式指到一个
稳定的非推理模型。**本批没有改路由**(没有证据说明该改哪个),如实记在这里。

## 3. `compare_arms --amounts`(步骤 2)

默认关闭(会调模型),`--amounts` 打开。金额行从正则改为模型抽取,
输出每臂"本章总额与后一章对不上"的**漂移条数**,按 `(名目, 数额)` 去重
(不去重时 baseline 同一个 840 会被算 3 次)。

6 臂实跑(`--model deepseek-chat --max-tokens 6000`):

| 臂 | ch07 总额 | ch08 总额 | 漂移条数 |
|---|---|---|---|
| `continuity-20261114-outline10` | 880 | 820 | **1**(校服工钱总额 880) |
| `continuity-20261114-long24-baseline` | 840 | 820 | **1**(去重前 3 条同值) |
| `continuity-20261115-scene600` | 820 | 820 | 0 |
| `continuity-20261115-scene600b` | 900(毛收) | 900/820 | 0 |
| `pair6-1a` | 820 | 820 | 0 |
| `pair6-2b` | 840 | 820 | **1**(校服工钱总额 840) |

## 4. 真实生成流程取证(步骤 3)

真实生成日志里的原话(新项目、真实模型 `gemini-3.1-pro`):

```
ContextPack budget=20000 estimated_tokens=1436 trace=[...]
```

逐块(该章是第 1 章,所以没有前章正文与近章摘要,符合预期):

| status | in | out | req | source |
|---|---|---|---|---|
| included | 556 | 556 | 1 | world_rules |
| included | 288 | 288 | 1 | authoritative_fact_contract |
| included | 235 | 235 | 1 | character_cards |
| included | 273 | 273 | 0 | writing_guidance |
| included | 400 | 400 | 0 | future_outlines |
| included | 232 | 232 | 1 | current_outline |
| included | 146 | 146 | 0 | numeric_pins |
| included | 101 | 101 | 0 | book_outline |
| included | 25 | 25 | 0 | volume_outline |
| included | 107 | 107 | 1 | roster |

这补上了上一批"只用 harness 近似、没走完整生成流程"的缺口,也确认新预算 20000 在真实管线里生效。

**未取到的部分(必须说清)**:中段章(ch18)在真实生成里**无法产出** ——
上游 NVIDIA embedding 端点持续返回 **HTTP 500**(两次运行共 20 次请求全失败),
报 `RequiredContextUnavailable: character_cognition_retrieval_unavailable`。
换路也走不通:本机网关 `http://127.0.0.1:8317/v1/embeddings` 返回 404(不代理 embeddings),
评估库里唯一的 embedding 端点是 `https://integrate.api.nvidia.com/v1`。
所以"中段章在真实生成里的逐块 trace"**仍未有**;§5 的表格(预算 9500 → 20000 让
`future_outlines` 从整块省略变成 included)来自直接调用 `ContextPackBuilder.build` 的实测,
不是完整生成流程。

## 5. 计划阶段定数(步骤 4)

改动四处:

1. `SceneBrief.money_totals` + `_CONDITIONAL_CONTRACT_FIELDS`(可选字段,不写不算错);
2. `CHAPTER_CONTRACT_FIELDS['money_totals'] = '本场已定金额'` → 章节大纲块渲染;
3. `SCENE_CONTRACT_FIELDS_PROMPT`(代码侧字段清单)加入该字段;
4. `numeric_pins` 把本章计划已定的金额作为**前置块**注入(`pin_block` 有条数上限,靠后会被挤掉)。

**真实规划模型证据**(单场结账场景):

```
money_totals='校服工钱=820(三十套校服结算); 铺租=800(今日到期房租); 剩余现金=20(820-800)'
写手提示: 【本场已定金额】校服工钱=820(三十套校服结算); 铺租=800(今日到期房租); 剩余现金=20(820-800)
```

即规划**确实会写出总额**,而且写手提示里拿到了它;数值也与后章钉子(八百二)一致。

**顺带查出的一个真实限制**:`prompt_assets` 的 `seed_builtins` **只补缺失的 task_type,
不升级已存在的版本**(`if p["task_type"] in existing_task_types: continue`)。
也就是说,**改 `prompts/scene_writing.py` 里的默认提示,对已经跑过的库不生效** ——
该库的 `scene_planner` 提示仍是 version 1,`money_totals` 在里面根本没有。
本批因此把字段清单放在**代码侧**的 `SCENE_CONTRACT_FIELDS_PROMPT`(不经 DB,即时生效);
代码侧默认提示也同步补了(新装有效)。**提示升级机制本身没有动** —— 那是一个独立特性
(要动版本号/迁移/UI),不在本批范围。

**未验证的部分**:计划里要求"两章真链里 ch07/ch08 不再冲突"。被同一件事挡住 ——
中段章需要 embedding,而上游 500。所以这一条**未验证**,不是"没冲突"。

## 6. 明确没做

- 金额冲突**自动改写**(判定错就改稿,风险大于收益);
- 为"没写金额"而重规划或阻断;
- 前端新增展示(现有 UI 不消费 quality meta);
- 提示 Registry 的版本升级机制(见 §5);
- 改 `numeric_check` 的模型路由(没有证据说明该改哪个,只记录了现象)。

## 7. 本批新增回归

| 文件 | 项数 | 内容 |
|---|---|---|
| [test_numeric_check.py](/root/ai-write/backend/tests/test_numeric_check.py) | 7 | 冲突记录 / 一致不记 / 失败吞掉不阻断 / 无计划零调用 / 无会话返回空 |
| [test_money_totals_contract.py](/root/ai-write/backend/tests/test_money_totals_contract.py) | 11 | 可选字段 / 渲染 / 坏值语义 / 钉子前置 / 无字段行为不变 |
| [test_quality_eval_amounts.py](/root/ai-write/backend/tests/test_quality_eval_amounts.py) | +9(共 45) | 单一实现断言 / 计划值比对 / 上限 / 漂移指标 / 聚合输出 |

全量离线回归 **4780 passed / 762 skipped / 0 failed**。

## 8. 下半场:换掉 embedding 上游后补上的三个结论

中段章原先被上游 embedding 卡住(详见
[embedding-provider-sourcing-20261117.md](/root/ai-write/docs/evaluation/embedding-provider-sourcing-20261117.md))。
换成 `nvidia/nemotron-3-embed-1b`(2048 维、batch×32 0.33s、原默认模型已 500)后,三件事有了结论。

### 8.1 步骤 3 现在完全闭合

ch18 真实生成成功(`status: ok`,`nvidia_embed` 报错 0 次)。逐块 **24 个块全部 included、0 省略**:
`cognition_boundaries` 1278(真实检索)、`future_outlines` **1086**、`recent_summaries[0..7]`、
`previous_chapter_text` 1099、`numeric_pins` 51,合计 10,176 字 ≈ 6,800 真实 token。
其中 `future_outlines` 正是旧预算 9500 下被**整块丢掉**的那一块 —— 这条现在有了真实生成流程的现场证据。

### 8.2 步骤 4:计划层收敛后,**同名目的漂移修好了**

三阶段(每轮都把上一轮暴露的问题修掉,详见
[plan-money-reconcile-20261117.md](/root/ai-write/docs/evaluation/plan-money-reconcile-20261117.md)):

1. **只加 `money_totals` 字段** → 写手不再自造,但**规划者**自造:ch07 计划写 840;
2. **加计划层收敛**(`services/money_plan.py`):与大纲冲突的金额条目丢掉。第一版有两个真缺陷 ——
   条目只按分号切(ch08 用逗号分隔的三条被并成一条整串删掉)、权威集合**漏了本章自己的大纲**
   (ch08 的 820 被判"无出处")—— 四轮里逐轮暴露并修掉;
3. **第四轮**:

```
[chain4] ch07 总额=[820, 400]  ch08 总额=[820, 820]  冲突=1  二十件校服工钱总额=400
```

`校服工钱总额 820 = 820`,**同名目一致**,原来自造的 840 / 880 不再出现;
唯一被标出的 `400` 是 ch07 **新引入**的"另一批二十件校服"(ch08 没提),不是与已定数冲突。

**残余边界**:工具分不出"新引入的项"与"与已定数冲突的项",两者都表现为"计划里没有这个总额";
要分开需要按名目持久化的金额台账。见 [chain RUN](/root/ai-write/docs/evaluation/money-totals-chain-20261117/RUN.md)。

### 8.3 步骤 1 的核对:一个真阳性 + 两个误报(修掉一个半)

同一轮真链里核对真的响了:

| 章节 | 报出的项 | 判定 |
|---|---|---|
| ch07 | `工钱总额=840` ×3 | **真阳性**(计划/钉子写的是 820) |
| ch08 | `校服工钱总额=820` | **误报** —— 820 就写在**本章大纲**里 |
| ch18 | `垫了总数=3000` | **误报** —— 3000 早在 ch10 正文写着,但大纲与摘要都没有 |

两处修正:

1. 核对范围加入**本章自己的大纲**(ch08 的 820 因此不再误报);
2. 加入**最近 8 章核实过的摘要**(对"前文已定"有用,但**摘要有损**:ch18 的 3000 不在任何摘要里,
   所以那条**仍会误报**)。

实测计划集:ch07 含 820 **不含 840**(会命中),ch08 含 820(**不再误报**)。
真正的解法是把"实际写出过的金额"持久化成台账(`numeric_ledger` 的原始设想);本批未做。

### 8.4 仍未闭合

- "大纲与摘要都没记"的数字仍会误报 → 需要金额台账;
- 两章真链的"ch07/ch08 一致"**已达成**(8.2:同名目 820 = 820);
- 换模型后 Qdrant 旧向量未重建(维度一致 2048,但语义空间不同,检索质量需重跑);
- 全量离线回归仍是 **4780 passed / 762 skipped / 0 failed**。
