# 两章真链:计划定数后 ch07/ch08 是否还对不上(2026-11-17)

```bash
cd /root/ai-write/backend
python -m scripts.quality_eval.continuity_probe ... --ids <本目录>/ids.json \
  --out ../docs/evaluation/money-totals-chain-20261117 --chapters 24 --limit 8 --resume \
  --model gemini-3.1-pro --max-tokens 8000
```

做法:复制 24 章基线目录,删 `ch07.txt`/`ch08.txt`,只重生成这两章。四轮都 `status: ok`。

## 四轮的结果(每一轮都改掉了上一轮暴露的问题)

| 轮次 | 代码状态 | 结果 |
|---|---|---|
| chain1 | 只有 `money_totals` 字段 | ch07 **840** vs ch08 820 → 仍冲突;查规划响应发现 **ch07 的计划本身就写 840** |
| chain2 | 加了计划层收敛(只按分号切条目) | ch07 的 840 被丢;但 ch08 的三条被并成一条整串丢掉(820 一起没了) |
| chain3 | 收敛加了逗号分隔 | ch08 正常;但 ch07 的 820/800 被判成"无出处"整串丢掉 —— 权威集合**漏了本章自己的大纲** |
| chain4 | 权威集合并入本章大纲 + 判定改为"完全不相交" | **ch07 820 = ch08 820,同名目不再漂移** |

第 4 轮的口径(命令见 [plan-money-reconcile-20261117.md](/root/ai-write/docs/evaluation/plan-money-reconcile-20261117.md)):

```
[chain4] ch07 总额=[820, 400]  ch08 总额=[820, 820]  冲突=1  二十件校服工钱总额=400
```

- **校服工钱总额 820 = 820** —— 原来自造的 840/880 不再出现;
- `400` 是 ch07 **新引入**的"另一批二十件校服",ch08 没提 —— **不是**与已定数冲突,
  而是工具分不出"新项 vs 冲突"(见 §5)。

## 注意

本目录里的 `ch07.txt`/`ch08.txt` 是**第 4 轮**的产物。前三轮的文本已被覆盖,
它们只作为"问题如何被逐轮暴露"的记录留在上面的表里。
