# 换 embedding 模型后索引要重建(2026-11-17)

## 1. 机制:向量按"空间"分集合,换模型就是换空间

`embedding_space.space_identity()` 把 **provider 类 / model / base_url / modality / encoding / truncate**
一起做指纹。集合名形如:

```
chapter_cognition__<project_id 去横线>__<space_id>__<dimension>
```

所以换模型(本轮 `nemotron-3.5-embed-1b` → `nemotron-3-embed-1b`)会落到**另一个集合**,
旧集合的点不会被复用。

## 2. 实测:换完模型,当前空间是空的

评估项目 `c20621d7-…`:

| 集合 | space | 点数(修复前) |
|---|---|---|
| `chapter_cognition` | `96fe84ec…`(旧模型) | 114 |
| `chapter_cognition` | `d3219c81…`(旧模型) | 0(已被维护任务清空) |
| **`chapter_cognition`** | **`a37fd218…`(当前模型)** | **0** |
| `chapter_chunks` | 当前模型 | **集合不存在** |

后果:`rank_cognition` 按当前集合名查询 → 0 点 → 空结果;
`resolve_prompt_selection` **不会因此报错**(只有嵌入调用失败或空间中途变化才抛),
所以是**静默**的:写手拿不到语义召回的旧材料,而链路看起来一切正常。

## 3. 根因:索引是懒建的,而维护任务只删不建

- 认知点的写入方是 `rank_cognition` 自己 —— 解析某一章上下文时顺手
  `ensure_collection` + `prepare_index`,把该章需要的条目 upsert 进去;
- `cognition_index_maintenance` 只做清理:把 `space_id != 当前` 的点列进 `victims` 删除
  ([cognition_index_maintenance.py:229](/root/ai-write/backend/app/services/cognition_index_maintenance.py:229));
- 于是**已经写完的书**在换模型之后,除非重新生成一遍,召回会长期为空。

## 4. 已修:补上两个回填入口

### 4.1 分块索引(产品自带)

`chapter_chunker.backfill_project_chunks(project_id)` —— 文档化的手动入口、可重复执行:

```
{"status": "ok", "chapters_chunked": 24, "chapters_skipped": 0}
```

`chapter_chunks` 由"不存在"变成 **60 个点**。

### 4.2 认知索引(本轮新增)

新增 [cognition_index_backfill.py](/root/ai-write/backend/app/services/cognition_index_backfill.py):
`backfill_project_cognition(project_id)`,与分块侧对齐——按写作时的同一路径逐章补齐:

```
load_prompt_context(db, pid, before_chapter_id=本章, defer_retrieval=True)  # 拿到 CognitionPrompt
   → rank_cognition(prompt, client, space)   # 内部 ensure_collection + prepare_index + 查询
```

- point id 由 `(project_id, space_id, key, part)` 决定 → **幂等**,重跑覆盖;
- 单章失败只记日志并继续,返回 `status: ok|partial|empty` 与失败明细;
- 不能在 DB 会话里做嵌入,所以 prompt 构造完就关会话(与写作时一致)。

实跑结果:

```
{"status": "ok", "chapters_indexed": 20, "chapters_skipped": 3, "chapters_failed": 0,
 "space_id": "a37fd218…"}
```

当前空间的认知点数 **0 → 142**;直接验召回:

```
ch18 召回键数=64   ch24 召回键数=64
```

**踩过的坑**:第一版用了 `load_candidate_context`,它返回的是**渲染后的文本**
(内部已经把检索跑掉了),不是 prompt;于是每章都在拿到字符串后崩掉 ——
日志显示 23 章失败,但点数其实已经涨了(内层路径写进去了)。必须走
`load_prompt_context(..., defer_retrieval=True)`。

## 5. 前后对照(装配量,不是质量)

同一章 ch18,召回修复前 / 后各跑一次真实生成(都 `status: ok`):

| 指标 | 修复前 | 修复后 |
|---|---|---|
| `estimated_tokens` | 6,883 | 7,106 |
| `cognition_boundaries` | 1,278 | 1,278 |
| `related_chapter_recall` | 218 | 233 |
| 块数 / 合计字符 | 24 / 10,176 | 24 / 10,573 |

**必须说清的边界**:这组数字只证明"索引回来了、召回有内容",
**不证明章节写得更好**。装配量几乎没变(认知块就是同一份台账、只是排序依据变了),
质量影响需要按内容对照或评审,本轮**没有量化**。

## 6. 建议

1. 换模型之后**先跑这两个回填**(分块 + 认知),再开始写作;
2. 更彻底一点:把"空间变化 → 自动回填"接进维护任务,而不是等人工触发;
3. 至少把"当前空间索引为空"显式化(日志/面板),而不是静默返回零命中 ——
   这与本仓库既有立场一致:降级可以,静默不行。

## 7. 验证

- 新增 4 项离线回归([test_cognition_index_backfill.py](/root/ai-write/backend/tests/test_cognition_index_backfill.py)):
  只从第 2 章起、单章失败不中断、无前序认知则跳过、空项目如实报告;
- 全量离线回归 **4794 passed / 762 skipped / 0 failed**(283.29s)。