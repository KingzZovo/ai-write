# 契约改动 A/B(2026-11-14,n=3)
> 本文件先记 n=3 的初测,末尾追加 **n=8 认定性对照**(含测量器缺陷的复核证据)。
> n=3 的结论已被 n=8 取代,但保留过程记录。


## 这次对照要回答什么

改动前的 `WRITER_CONTRACT_PROMPT` / `preflight_scene_blueprint_prompt` 已经从仓库里消失
(被改成了新版),所以不能只跑"现在",必须能把旧文本重放出来。做法是:

1. `snapshot` 命令从 `git HEAD` 读出旧版 `narrative_contract.py` 与
   `narrative_quality_gates.py`,渲染成 `contract-HEAD.json`;

> 注意(2026-11-14 后续):跑完这次 A/B 之后又发现,我在改写合同时漏掉了原文一条
> 「设定引用自洽」,被 `test_defect_class_immunity` 抓住并已补回。也就是说本目录
> `after-r3` 测的是"少了那一条"的版本,现在的仓库版本比它多一条。差一条不足以改变
> 上面的结论,但**要复现本目录的数字,必须用同一条 `contract-HEAD.json` 之外的
> 当时版本**,不能直接拿当前代码重跑。
2. `run --snapshot` 按块替换当前契约;
3. 两组用同一模型、同一温度、同一 `max_tokens`、每组 3 次采样。

**重放有效性已经用哈希验证**:旧契约重放出的 prompt sha256 与基线运行记录里的
`prompt.prompt_sha256` 逐条相同(6 个场景 × 2 个变体全部一致),并且与当前契约不同。
也就是说"before"组确实是改动前的输入,不是近似。

## 改了什么(全部来自基线证据)

| 条款 | 证据(基线正文原句) |
|---|---|
| 解决问题只用作者已给出的人和资源;不得补亲属/帮手/债主/新钱/新本事 | real_stall「**你哥**......你现在退了,以后拿什么翻身?」;real_ferry「去找供销社的**张阿姨**」「再去找供货的**老陈头**」;mystery_lodger「摸出一根**薄铜片**…拨开木栓」 |
| 已设定条件靠动作和代价体现,不替设定复述原理、次数与上限 | mystery_bridge「驿站的铁律森严,腰牌乃是差役的身份凭证…明日清晨卯时正点的驿站大查验」 |
| 开场直接进入处境与动作,不用通用强度意象起手 | mystery_bridge「**夜色沉如墨**」;mystery_lodger「天阴沉得像要压塌青石镇的屋檐」 |

场景规划合同同时加了 `cast_sources` 字段,让"不许补人"在规划阶段就生效;
整章单发链的 `preflight_scene_blueprint_prompt` 加了同一句。

## 结果(n=3 每格,共 72 次调用,全部成功)

| 类别 | before | after |
|---|---|---|
| pov_breach | 4 | 4 |
| instruction_leak | 0 | 0 |
| rule_recitation | 14 | 10 |
| metaphor_stack | 1 | 2 |
| outcome_unfulfilled | 30 | 31 |
| fact_violation | 69 | 65 |

按变体拆开(hard 合计 / 字数):

| 变体 | before | after |
|---|---|---|
| scene_writer(生产默认) | 22 hard / 16551 字 | **15 hard** / 15348 字 |
| chapter_single_shot | 17 hard / 16275 字 | 18 hard / 17530 字 |

`scene_writer` 里掉得最多的两项是 `fact_violation`(10→6)与 `outcome_unfulfilled`(12→9);
`mechanism_explained`(条件+上限式复述)从 4 次降到 0 次。
`chapter_single_shot` 基本持平,`unauthorized_new_character` 在这条链上仍然偏高。

## 结论(不许美化)

1. **方向上对生产默认链有利,但没有达到"显著下降"**:`scene_writer` hard 从 22 降到 15,

## 配对看(每篇 3 次取均值,再逐篇比)

合计数字会被题材差异掩盖,所以再按"同篇前后配对"看一遍 hard 合计:

| 变体 | 篇目 | before 均值 | after 均值 | 变化 |
|---|---|---|---|---|
| scene_writer | fantasy_herb | 3.00 | 1.67 | **−1.33** |
| scene_writer | mystery_lodger | 4.67 | 3.00 | **−1.67** |
| scene_writer | real_ferry | 1.67 | 2.33 | +0.67 |
| scene_writer | fantasy_bell | 1.00 | 1.33 | +0.33 |
| scene_writer | mystery_bridge | 0.67 | 0.67 | 0 |
| scene_writer | real_stall | 1.67 | 1.67 | 0 |
| chapter_single_shot | 6 篇 | — | — | 3 胜 3 负,幅度都是 ±0.33 |

`scene_writer` 是 **2 胜 2 负 2 平**,但两处改善的幅度(−1.33/−1.67)明显大于两处退步
(+0.33/+0.67),6 篇的平均变化是 **−0.33 hard/篇**。这个方向性比合计数字可信,
可是 2:2 的胜负有符号检验下不显著。`chapter_single_shot` 则是正负对称,等于没动。

复现这段配对分析:

```bash
cd ../docs/evaluation/quality-eval-20261114-abi
/root/ai-write/.venv/bin/python - <<'PY'
import json, collections, statistics as st
def load(n): return json.load(open(f"{n}/detections.calibrated.json", encoding="utf-8"))
b, a = load("before-r3"), load("after-r3")
def cells(d, v):
    out = collections.defaultdict(list)
    for stem, det in d.items():
        if f"-{v}-" not in stem: continue
        out["_".join(stem.split("-")[:2])].append(sum(det["hard_counts"].values()))
    return out
for v in ("scene_writer", "chapter_single_shot"):
    cb, ca = cells(b, v), cells(a, v)
    for case in sorted(set(cb) | set(ca)):
        print(v, case, st.mean(cb[case]), st.mean(ca[case]))
PY
```
   可是逐项看,单格样本只有 6 篇 × 3 次,多数条目的变化在 ±1–2 之间来回,属于采样波动。
   我不能把 22→15 说成已经验证的改进。
2. **整章单发链没有改善**(17→18),这条链的问题不在契约措辞,而在它拿到的是
   `preflight` 蓝图 + 一次性长提示,规划阶段没有 `cast_sources` 那样的约束。
3. 因此按契约第 5 条("需重写比例显著下降且不存在任一类别上升")判定:**未达标**。
   `metaphor_stack` 1→2 也是上升项,按规则必须写明,不予解释掉。
4. 若要拿到可信结论,下一步应把每格采样提到 10 次以上(或固定 seed + 多次取中位数),
   并把 `chapter_single_shot` 也接上规划层约束——这轮没有做。

## 可复核命令

```bash
cd backend
# 1) 冻住改动前的契约
python -m scripts.quality_eval snapshot --rev HEAD \
  --cases scripts/quality_eval/cases/sample_set_v1.json \
  --variants scene_writer,chapter_single_shot \
  --out ../docs/evaluation/quality-eval-20261114-abi/contract-HEAD.json

# 2) 两组各 3 次
python -m scripts.quality_eval run --cases scripts/quality_eval/cases/sample_set_v1.json \
  --out ../docs/evaluation/quality-eval-20261114-abi/before-r3 --model gemini-3.1-pro \
  --variants scene_writer,chapter_single_shot --temperature 0.3 --max-tokens 4096 \
  --retries 3 --repeats 3 --snapshot ../docs/evaluation/quality-eval-20261114-abi/contract-HEAD.json
python -m scripts.quality_eval run --cases scripts/quality_eval/cases/sample_set_v1.json \
  --out ../docs/evaluation/quality-eval-20261114-abi/after-r3 --model gemini-3.1-pro \
  --variants scene_writer,chapter_single_shot --temperature 0.3 --max-tokens 4096 \
  --retries 3 --repeats 3

# 3) 同检测器重打分(两组都用当前代码)
python -m scripts.quality_eval rescore --dir .../before-r3 --name calibrated
python -m scripts.quality_eval rescore --dir .../after-r3  --name calibrated
```

## 未做

- 未做真人盲评,也没有把两组顺序打乱给外部读者。
- 未测 `bare` 组的同条件对照(本目录只有两条生产链)。


---

# 认定性对照(n=8 每格,共 96 次调用)

```bash
cd backend
PY=/root/ai-write/.venv/bin/python
D=../docs/evaluation/quality-eval-20261114-abi
# 改动前:用 git HEAD 的契约快照重放
$PY -m scripts.quality_eval run --cases scripts/quality_eval/cases/sample_set_v1.json \
  --out $D/before-r8 --model gemini-3.1-pro --variants scene_writer \
  --temperature 0.3 --max-tokens 4096 --retries 4 --repeats 8 --snapshot $D/contract-HEAD.json
# 改动后:当前契约
$PY -m scripts.quality_eval run --cases scripts/quality_eval/cases/sample_set_v1.json \
  --out $D/after-r8 --model gemini-3.1-pro --variants scene_writer \
  --temperature 0.3 --max-tokens 4096 --retries 4 --repeats 8
# 两臂都用当前检测器与样本集重打分
for a in before-r8 after-r8; do $PY -m scripts.quality_eval rescore --dir $D/$a \
  --name calibrated --cases scripts/quality_eval/cases/sample_set_v1.json; done
```

两臂各 **48/48 成功返回**(第一次跑失败,是网关被并发打崩;清空目录后单独重跑)。

## 数字

| 类别 | before | after | 变化 |
|---|---|---|---|
| pov_breach | 11 | 7 | **−4** |
| instruction_leak | 0 | 0 | 0 |
| rule_recitation | 19 | 6 | **−13** |
| metaphor_stack | 2 | 1 | −1 |
| outcome_unfulfilled | 29 | 42 | **+13** |
| fact_violation | 100 | 73 | **−27** |
| hard 合计 | 79 | 88 | +9 |
| 每篇字数 | 10940 | 9849 | −10% |

被针对性改的三个类别(凭空补人 → `fact_violation`;复述设定 → `rule_recitation`;
通用强度比喻开场 → `metaphor_stack`)都下降,而且幅度不小。**但 `outcome_unfulfilled` 上升**,
所以按标准第 5 条("不存在任一类别上升")仍然不达标。

## 这一项**不是**合同的退步,而是我的测量器不可用

对 after 臂逐条复核 `outcome_unfulfilled` 的最大两项,用的是**否定式判据**(比"有没有出现某词"可靠):

### `sister_stays`(要求:弟弟上船、姐姐留岸)

复核方式:搜"周芸是否上船/登船"。8 篇全部**没有**她上船的证据。

| 篇 | 检测器 | 实际写法 |
|---|---|---|
| r1 | 判未兑现 | 「周芸站在面摊的塑料棚边…**她没有动**,直到那艘渡船…缓缓离岸,她才转过身」 |
| r3 | 判未兑现 | 「周芸**坐在原处**,看着他踩上渡船」 |
| r8 | 判未兑现 | 同上,姐姐全程留在摊边 |

→ 3 次"未兑现"全是误报:要求的事件确实发生了,只是没有写成「岸上/岸边」这两个词。

### `handles_waiter`(要求:苏姐先处理阿六)

| 篇 | 检测器 | 实际写法 |
|---|---|---|
| r2 | 判未兑现 | 「阿六应了一声,脚下却有些迟疑」 |
| r3 | 判未兑现 | 「吩咐道:**把桌子收拾干净,鞋子收到杂物筐里**」 |
| r6 | 判未兑现 | 「阿六张了张嘴想说什么,迎上苏姐冷静的眼神,**只得应了一声**」 |
| r8 | 判未兑现 | 「**阿六,下来**」 |

→ 4 次里至少 3 次是误报,指令就写在文本里。

**结论**:`outcome_unfulfilled` 这一类用的是"字面词/正则是否出现"的代理,它的假阳性率与
效应量同阶,因此**它对本次对照没有分辨力**。我不会拿它当"合同变差了"的证据,也不会
因为它把总数抬上去就宣称"合同变好了"。

## 认定性结论(替代前面 n=3 的部分)

1. 改动对**它针对的三个类别**有可测量的改善:fact_violation −27、rule_recitation −13、
   pov_breach −4,并且是在 n=8、两臂各 48 次成功调用下测到的。
2. `outcome_unfulfilled` 的上升**不可归因于合同**:逐条复核显示主要是代理误报。
3. 因此按标准第 5 条的**字面要求**——"不存在任一类别缺陷计数相对基线上升"——**未达标**,
   因为该类别数字确实上升了,而我没有把它校正成一个可信的指标。
4. 一个真实的副作用:每篇字数 **−10%**(10940→9849)。这与"少补人、少加资源"的方向一致,
   但和连续章探针里"字数系统性低于目标"的问题是同一方向的叠加,需要单独处理。

## 下一步(未做)

把 `outcome_unfulfilled` 从"词/正则命中"换成**独立评审**:用不同模型、遮住版本、
把"本场必须完成的结果"清单交给它逐条判定并给出原句。只有换了测量器,第 5 条才有结论。
另外`--repeats` 也可扩到 10 以上,但收益远小于换测量器。
- 未把 3 次采样扩到 10 次,因此置信度不足,结论按"方向性"记录。


---

# 独立评审:用换测量器的方式给第 5 条下结论

上面证明了"词/正则命中"这类代理不能用来判断"结果是否交付"。于是换测量器:

- **换模型**:评审用 `deepseek-chat`,与生成模型 `gemini-3.1-pro` 不同家族;
- **遮版本**:只给正文 + "本场必须完成的结果"清单,不给任何分组信息;
- **要证据**:判"已交付"必须给出正文里的一段依据;判"未交付"不需要(不交付没法引用)。

```bash
cd backend
for arm in before-r8 after-r8; do
  python -m scripts.quality_eval judge \
    --dir ../docs/evaluation/quality-eval-20261114-abi/$arm \
    --cases scripts/quality_eval/cases/sample_set_v1.json \
    --name deepseek --model deepseek-chat
done
```

## 评审自己也会改写引用,所以用三级"可定位性"而不是二值

实测:评审给的"逐字引用"自己也不是逐字的——把全角引号写成半角、偶尔把两句拼起来。
二值判据下交付率非 0% 即 100%,没有分辨力。所以改成:

| 等级 | 含义 |
|---|---|
| `exact` | 逐字包含 |
| `normalized` | 去掉标点与空白后包含(评审把 `“”` 写成 `""` 属于这一级) |
| `partial` | 最长公共子串 ≥ 12 字(确实指向正文里某一段) |
| `unlocated` | 其余,视为编造 |

判"已交付"必须至少 `partial`。测试见 [test_judge_locator.py](/root/ai-write/backend/tests/test_judge_locator.py)。

## 结果(47 篇两臂都判成功的配对样本)

| 臂 | 条目 | 判定已交付 | 交付率 | 未交付 | 依据等级 |
|---|---|---|---|---|---|
| before | 220 | 214 | **97.27%** | 6 | exact 14 / normalized 199 / partial 1 |
| after | 220 | 214 | **97.27%** | 6 | exact 21 / normalized 193 / partial 0 |

**两臂逐位相同**:214/220、未交付 6 条。分题材也一样(fantasy_bell 40/40、fantasy_herb 40/40、
mystery_lodger 40/40、real_stall 32/32;mystery_bridge 35/40 → 34/40;real_ferry 27/28 → 28/28)。

也就是说:**之前那个 `outcome_unfulfilled` +18/+13 完全是测量器造成的**,独立评审下
"结果交付"没有变差,一条都没多丢。(48 篇里 `before` 有 1 篇评审连续两次失败,所以配对取 47 篇;
该篇是 `real_ferry-scene_writer-r3`,不影响结论方向,但如实记在这里。)

## 第 5 条的结论

把两个测量器合起来:

| 指标 | 测量器 | before | after | 判断 |
|---|---|---|---|---|
| fact_violation | 确定性检测 | 100 | 73 | 下降 |
| rule_recitation | 确定性检测 | 19 | 6 | 下降 |
| pov_breach | 确定性检测 | 11 | 7 | 下降 |
| metaphor_stack | 确定性检测 | 2 | 1 | 下降 |
| instruction_leak | 确定性检测 | 0 | 0 | 持平(零) |
| 结果交付 | 独立评审 | 97.27% | 97.27% | **持平** |

→ **"改进后不存在任一类别缺陷计数相对基线上升"这一条成立**(用各自可用的测量器)。

→ 但另一条"被编辑式审读判为'需重写'的样本比例相对基线显著下降"**无从体现**:基线本身就是
**0/6 需重写**(基线审读结论是 2 篇可交付、4 篇需局部修订)。比例已经在 0,不可能再降。
这是标准与基线不匹配,不是改动的功劳,所以我把第 5 条记成
**"达成(其中'需重写比例下降'一条因基线已在 0 而无从体现)"**,而不是打勾了事。

## 复现配对统计

```bash
cd ../docs/evaluation/quality-eval-20261114-abi
/root/ai-write/.venv/bin/python - <<'PY'
import json
def load(arm):
    latest = {}
    for r in json.load(open(f"{arm}/judged.deepseek.json", encoding="utf-8")):
        latest[r["stem"]] = r
    return latest
b, a = load("before-r8"), load("after-r8")
common = [s for s in b if s in a and b[s].get("status") == a[s].get("status") == "ok"]
for name, data in (("before", b), ("after", a)):
    items = sum(data[s]["items"] for s in common)
    delivered = sum(data[s]["delivered"] for s in common)
    print(name, delivered, "/", items)
PY
```

## 这套评审的已知限制

- 评审模型(ds 系)对长文本偶尔连续失败;48 篇里 `after` 全成、`before` 有 1 篇两试未成。
- `partial` 等级(≥12 字公共子串)比 `normalized` 弱:它只说明"指向了正文的某一段",
  不保证整句连续。本轮 before 侧出现 1 次,after 侧 0 次。
- 评审与生成是**不同模型但同一条网关**,不能排除共同的措辞偏好;要更强的结论需要真人或
  第三方模型复核。本轮没有真人参与。
- 判定只有"交付/未交付"两档,不评价写得好不好;文学判断仍以编辑式审读为准。