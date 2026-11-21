# 篇幅根因诊断(规划侧 vs 写手侧)

目录:`docs/evaluation/continuity-20261114-lengthdiag/`

```bash
cd /root/ai-write/backend
/root/ai-write/.venv/bin/python -m scripts.quality_eval.continuity_probe \
  --database-url "postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/aiwrite_eval" \
  --out ../docs/evaluation/continuity-20261114-lengthdiag --chapters 5 --limit 4
```

`chapters.json` 里记录了每章的 `plan`(场景规划结果):

| 章 | 场景数 | Σtarget_words | chapter_target | 实际字数 | 比值 |
|---|---|---|---|---|---|
| 01 | 1 | 1200 | 1200 | 759 | 0.63 |
| 02 | 1 | 1200 | 1200 | 2155 | 1.80 |
| 03 | 1 | 1200 | 1200 | 1117 | 0.93 |
| 04 | 1 | 1200 | 1200 | 1466 | 1.22 |

结论:**规划侧不是瓶颈**。规划器把整章目标(1200)全数分给了场景,
和 `chapter_target` 完全一致,没有"每场只给 300 字"这类欠配。

写手侧忽高忽低(0.63–1.80)的真正原因随后查明:这一轮的探针把章节大纲写在了
渲染器不认识的字段名下,大纲在提示里被截断成一行标题,**写手没有本章大纲可展开**,
只能凭上一章正文自由发挥——字数因此随题而不随目标。

修复见 `continuity-20261114-outline10/REVIEW.md`:装置修好后 10 章中位比值 0.94。

> 注:本目录 `chapters.json` 的 `chars` 是当时的值;探针续跑分支曾经用文件字节数
> 顶替字符数,已修(见 `continuity_probe.py` 续跑分支)。
