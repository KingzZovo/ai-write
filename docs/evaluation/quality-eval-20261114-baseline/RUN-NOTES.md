# 基线:固定样本集上的确定性检测结果(机器生成)

本目录只记录事实:调用参数、模型、prompt 哈希、检测计数。文学判断见 `REVIEW.md`。

- 样本集:`sample_set_v1`(6 个场景 / 3 个题材:现实生活、东方幻想、悬疑)
- 网关:`http://127.0.0.1:8317/v1`,模型 `gemini-3.1-pro`,temperature 0.3,max_tokens 4096
- 变体:生产场景链 `scene_writer`、整章单发链 `chapter_single_shot`、对照 `bare`
- 每个变体 6 次调用,共 18 次,全部成功返回

## 两次打分的区别(必须一起看)

| 文件 | 含义 |
|---|---|
| `detections.json` / `summary.json` | 基线运行当时的检测器与当时的样本集 |
| `detections.calibrated.json` / `summary.calibrated.json` | 同一批正文,用**标定后**的检测器与样本集重打分 |

标定的原因与证据写在 `REVIEW.md`:第一版检测器把"字面没出现"当成"事实没保住",
在 6 篇正文上产生了一批可核对的误报(例如正文写"宽仅尺许"而 token 表只认"只够一个人")。
标定只放宽**事实确实在场、只是措辞不同**的 token;事实真正缺失的条目(如 `three_years`)
仍然照报。标定前后两套数字都保留,不做覆盖。

## 标定后基线(scene_writer,生产路径)

| 类别 | 命中 | 其中 hard |
|---|---|---|
| pov_breach | 0 | 0 |
| instruction_leak | 0 | 0 |
| rule_recitation | 6 | 0 |
| metaphor_stack | 0 | 0 |
| outcome_unfulfilled | 2 | 2 |
| fact_violation | 11 | 2 |

三个变体合计 hard:scene_writer 6 / chapter_single_shot 10 / bare 13。
即"给不给写作合同"这件事本身有可测量的差别,这就是对照组的用途。

## 命令

```bash
cd backend
python -m scripts.quality_eval run \
  --cases scripts/quality_eval/cases/sample_set_v1.json \
  --out ../docs/evaluation/quality-eval-20261114-baseline \
  --model gemini-3.1-pro --variants scene_writer,chapter_single_shot,bare \
  --temperature 0.3 --max-tokens 4096 --retries 3

python -m scripts.quality_eval rescore \
  --dir ../docs/evaluation/quality-eval-20261114-baseline --name calibrated \
  --cases scripts/quality_eval/cases/sample_set_v1.json

python -m scripts.quality_eval detect \
  --dir ../docs/evaluation/quality-eval-20261114-baseline   # 同一输入两次检测逐字一致
```
