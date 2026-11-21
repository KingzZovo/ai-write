# 召回恢复后的中段章(ch18,2026-11-17)

```bash
cd /root/ai-write/backend
python -m scripts.quality_eval.continuity_probe ... --ids <本目录>/ids.json \
  --out ../docs/evaluation/recall-restored-20261117 --chapters 24 --limit 18 --resume \
  --model gemini-3.1-pro --max-tokens 8000
```

在"分块索引已重建(60 点)+ 认知索引已回填(当前空间 142 点)"之后跑的同一章,
用来和 [trace-20261117-ch18](../trace-20261117-ch18/RUN.md)(召回为空时)对照装配量。

| 指标 | 召回为空 | 召回恢复 |
|---|---|---|
| `estimated_tokens` | 6,883 | 7,106 |
| `cognition_boundaries` | 1,278 | 1,278 |
| `related_chapter_recall` | 218 | 233 |
| 块数 / 合计字符 | 24 / 10,176 | 24 / 10,573 |

**这只是装配量**:说明索引回来了、召回有内容;不说明章节写得更好。
质量影响未量化(要按内容对照或评审)。
