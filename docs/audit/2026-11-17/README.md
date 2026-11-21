# 全仓代码审计(2026-11-17)

目标:把 `backend/app` 下**全部 Python 文件**审计一遍,发现问题就修,修完继续,直到覆盖 100%。

## 做法

1. **确定性分区**:`backend/scripts/ops/audit_partition.py` 把 `backend/app` 的 385 个 `.py`
   切成 8 份(`partitions/p1.txt` … `p8.txt`),保证**互不重叠、并集=全部**,同一文件永远落在同一分区,
   重复执行结果一致。
   - 先按目录聚合;`services/` 太大,再按文件名前缀归主题(write / cognition / retrieval / runtime / domain / misc);
   - 任何组超过 48 个文件就切片,再按"文件数最少优先"贪心均衡。
2. **并行只读审计**:8 个子代理各审一份清单,只读(Read/Grep/Glob),输出
   `pN.md`(本目录)。每份必须列出**实际打开过的文件**。
3. **覆盖率核对**:`backend/scripts/ops/audit_coverage.py` 解析每份 `pN.md` 里
   `## 已读文件` 下的围栏块,与 `backend/app/**/*.py` 求并集,不足 100% 返回非零并打印缺失清单。

## 复跑

```bash
cd backend
.venv/bin/python -m scripts.ops.audit_partition --out ../docs/audit/2026-11-17   # 重新分区
.venv/bin/python -m scripts.ops.audit_coverage  --dir ../docs/audit/2026-11-17   # 核对覆盖
```

## 记录格式(每份 pN.md)

- 逐条 finding:标题 / 位置(`文件:行`)/ 事实 / 影响 / 建议改法,按 P0–P3 分档;
- 逐文件一句话结论表;
- `## 已读文件` + 围栏块(一行一个相对仓库根路径);
- `## 覆盖说明`(没读什么、因此不能下哪些结论);
- `## 该分区最该先修的 3 件`(各附最小验证命令)。

## 边界

- 审计阶段只读;修复另做,且必须带离线回归测试。
- 不联网、不调用模型(此刻另有一个 100 章真实长跑在跑)。
- 只使用隔离库 `aiwrite_eval`,不触碰用户既有数据。