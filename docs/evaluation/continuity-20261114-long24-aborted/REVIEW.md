# 连续 24 章对照臂(第一次尝试,失败)

这一轮在 **ch07 停下**,保留下来的原因是失败模式本身有价值。

```
{"index": 6, ..., "cognition_error": "EntityExtractionError: 认知依据无法在正文逐字定位",
 "cognition": {"status": "skipped", "attempts": 3}}
{"index": 7, "status": "error", "error_type": "RequiredContextUnavailable",
 "error_message": "character_cognition_not_ready"}
{"index": 8, "status": "error", "error_type": "RequiredContextUnavailable",
 "error_message": "character_cognition_not_ready"}
{"chapters": 6, "violations": 11}   ← max_consecutive_errors=2 触发停机
```

## 链条

1. 第 6 章的认知提取连续 3 次报"认知依据无法在正文逐字定位"
   (`character_cognition.validate_changes` 要求 evidence 是正文的**精确子串`)。
2. 该章认知记录落成 `skipped`(不是 completed)。
3. 第 7 章开写前的上下文检查要求"上一章认知已 completed",于是抛
   `RequiredContextUnavailable: character_cognition_not_ready`。
4. 第 8 章同样被拦,连续 2 次错误 → 运行停止。

## 这暴露了两件事

**(a) 产品缺陷:一次依据定位失败会卡死整本书。**
写作本身没问题(第 6 章 `gate: passed`、1041 字),卡的是提取步骤;
而提取失败是"软"失败,后果却是"硬"阻断。

**(b) 定位规则比它要防的东西更严。**
`evidence in source` 是精确子串匹配。模型稳定的偏差是**把全角标点写成半角**
(引号、逗号),这既不是改写也不是拼接,却会被判死。

## 已做的两处修改

1. `chapter_entity_extraction.evidence_locatable()`:先按逐字判,不中再把
   **标点形式**一对一归一化后判一次。刻意不删字符、不忽略换行、不放行省略号与拼接——
   那些正是"改写/概括"的信号,放宽会把伪造依据一起放进来
   (回归见 `tests/test_cognition_evidence_locator.py`)。
   `cognition_reader` 的读侧校验同步改用同一个函数,否则写侧放宽后读侧会拒绝自己的记录。
2. 探针续跑分支新增 `resumed_extract`:正文已落库但实体/认知没做完时,
   **补跑提取而不是把正文当残稿重写**(原先会 `rename(.incomplete.txt)` 再重写一遍)。

改后重跑,见 `continuity-20261114-long24-pins/REVIEW.md`。
