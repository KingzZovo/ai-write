# 连续 24 章真实链 · 修复臂(补上 prompt 资产后)

目录:`docs/evaluation/continuity-20261114-long24-fixed/`

```bash
cd /root/ai-write/backend
/root/ai-write/.venv/bin/python -m scripts.quality_eval.continuity_probe \
  --database-url "postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/aiwrite_eval" \
  --out ../docs/evaluation/continuity-20261114-long24-fixed --chapters 24 --limit 24
```

比上一臂多的一处改动:探针补种 `relationship_scope` prompt 资产并绑定真实 endpoint id
(上一臂第 18 章因此丢了一整章实体投影)。

## 1. 结果

| 指标 | 基线 | pins 臂 | 本臂 |
|---|---|---|---|
| 章节 `status: ok` | 24/24 | 24/24 | **24/24** |
| **实体投影 ok** | 22/24 | 23/24 | **24/24** |
| 认知记录 completed | 24/24 | 24/24 | **24/24** |
| `AT_LOCATION` 唯一约束报错 | 2 次 | 0 次 | **0 次** |
| `relationship_scope_prompt_not_configured` | 0 | 1 次 | **0 次** |
| 字数中位比值 | 0.967 | 1.105 | **0.786** |
| 长度≥目标 80% | 19/24 | 20/24 | **11/24** |
| 检查器命中 | 18 | 4 | 16 |

**实体投影 24/24 是这一臂的主要结论**:两个产品缺陷(同章第二地点建边、
探针漏种资产)都修掉了,连续 24 章不再有任何一章丢掉投影数据。

## 2. 这一臂没有复现 840/820,但这不算修复有效的证据

ch07 本轮**没有**去算这笔工钱(正文只提到"八百块的铺租"),
ch08 按大纲写"面额不多不少正好是八百二十元" —— 两章之间没有矛盾。

但 ch07 本身也短(715 字),它干脆没写那个"算账"场面。
**这不是"注入拦住了模型",而是这一轮模型没走到那个坑上。**
想证明注入有效,需要让 ch07 必须给出一个数——本批没有做这样的定向样本。

所以 `numeric_pins` 的结论仍是 [pins 臂第 3 节](/root/ai-write/docs/evaluation/continuity-20261114-long24-pins/REVIEW.md) 那个:**提示里给数字约束不住数字**。

## 3. 篇幅:本臂明显偏短,原因无法归因

中位比值 0.786,13/24 章低于目标 85%。三臂的中位分别是 0.967 / 1.105 / 0.786 ——
跨度 0.32,单 seed 无法区分"改动引入"与"run 间方差"。

做过一项检查:`numeric_pins` 会不会挤掉别的上下文(`to_system_prompt` 有 9500 token
默认预算,超预算的低优先级部件会被丢弃)。用接近真实规模的 `ContextPack` 重放:
注入块 **included**,`budget_trace` 里没有出现因它导致的丢弃。但真实运行没有打点,
**不能据此断言没有挤占**。

列为**未解决**:需要同一 seed 跑 n≥3 或在离线编排出打印每章 `budget_trace` 才能判定。
本批不做。

## 4. 剩余命中(已逐个核对)

| 检查 | 命中 | 核对 |
|---|---|---|
| `word_below_target` | 13 | 真(见第 3 节) |
| `time_progress_missing` | 1(ch02) | 真但轻:大纲"次日清晨",正文没有"过了多久"的说法 |
| `unknown_character` | 1(张叔) | 真:ch02 出现的未点名人物 |

`pinned_number_conflict` 0 条:三个钉死数额(复查费一千二、停航补偿八百、新铺租九百)
正文全部遵守。中途出现过的 3 条误报已定位并修掉——那是**窗口截断**造的假数:
按 12 字切窗口后再找金额,把"一千二百"截成"一千",于是凭空多出 1200→1000 的矛盾;
另外"把复查费**和那**一千元旧账扣在一处"这种并列句也被误当成同一名目。
现在改成"先在全篇找完整金额,再判距离 + 名目与金额之间不能隔句读或并列连词"。
