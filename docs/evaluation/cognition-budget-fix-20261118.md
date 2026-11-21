# 认知上下文预算:一次会让长篇整批中断的缺陷(2026-11-18)

## 一、事故现象

用最终方案(gemini-3.1-pro,目标 2000 字)跑 100 章验证时,跑到 **第 36 章起连续 10 章**失败:

```
{"index": 36, "status": "error", "error_type": "RequiredContextUnavailable",
 "error_message": "character_cognition_context_budget_exceeded"}
...
{"index": 45, "status": "error", ... 同上 ...}
```

探针的 `--max-consecutive-errors 10` 到第 45 章触发,整批退出,只落了 **35 章**。
注意这 10 章不是"每章都真的超预算",而是第 36 章一失败 → 台账不再增长 → 之后每章都撞同一堵墙。

## 二、根因

`app/services/cognition_context.py: render_observations()` 要求:

> 每个**必选人物**的**最新一条记录(含依据原文)**都必须装进 `max_chars` 预算,否则抛
> `RequiredContextUnavailable('character_cognition_context_budget_exceeded')`。

而必选人物来自上游 `context_pack`:

```python
priority_names=[name for card in pack.character_cards for name in (card.name, *card.aliases)]
```

即**全书人物卡的名字与别名**。预算默认只有 **1200 字符**(`serialize_for_prompt(..., max_chars=1200)`),
篇幅一长、人物一多、依据原文一长,必然撑爆。

## 三、为什么不能"装不下就裁掉几个人"

这条 fail-closed 是**有意的**,`tests/test_cognition_context_selection.py` 里有专门用例:

```python
def test_recalled_latest_evidence_overflow_is_explicit_instead_of_older_unknown():
    ledger = {"陈川": {"history": [event(1, "信是否送到。", kind="still_unknown"),
                                   event(2, "父亲已经收信。", "完整获知过程。" * 100)]}}
    with pytest.raises(RequiredContextUnavailable, match="budget"):
        serialize_for_prompt(ledger, max_chars=200, priority_names=["陈川"])
```

理由:若预算不够就退化成"只给旧的未知道记录",模型会认为人物**还不知道**,
写出"忽然知道了/忽然忘了"的矛盾情节 —— 对长篇小说是硬伤。

第一版修法(把 `priority_names` 里未被本章提到的人降级为可选 + 按重要性裁剪必选集)
被这 4 个既有用例否掉了,已回退。**正确的修法是提高预算,保留语义。**

## 四、修法

| 项 | 内容 |
|---|---|
| 新增配置 | `COGNITION_HISTORY_MAX_CHARS: int = 3000`(原默认 1200,提高 2.5 倍) |
| 写作主路径 | `context_pack` 调用 `load_prompt_context(..., max_chars=settings.COGNITION_HISTORY_MAX_CHARS)` |
| 评审/改写路径 | `cognition_retrieval.load_candidate_context` 同预算(否则那条路先撞线) |
| 可观测性 | 超预算时 `logger.warning('cognition_context_budget_exceeded people=%d required_chars=%d remaining=%d budget=%d')`,下次就知道该把预算调到多大 |
| 语义 | **不变**:装不下仍然显式失败 |

单测 `tests/test_cognition_context_budget.py`(7 项):
默认预算 ≥3000;20 人物带引文的记录在 3000 下**全部装下**;同样输入在 1200 下**确实失败**
(证明是容量问题);单条超大仍 fail-closed;失败前有日志;空输入返回空;HEADER 远小于预算。

## 五、受影响的已产出数据

- `longrun-100-gemini-2000-final`:第 36~48 章是用**回退前那版**代码跑出来的,
  已删除并从第 36 章重跑,保证整批同源(也保证与 dsv4 侧的对比是同代码版本)。
- 第 1~35 章不受影响(它们是在事故之前、用正确语义生成的)。

## 六、验证

| 项 | 结果 |
|---|---|
| `tests/test_cognition_context_budget.py` | 7 passed |
| 认知相关既有用例(`selection` / `history_render` / `character_state_context`) | 46 passed, 1 skipped |
| 后端全目录离线 | 见本批最终一轮 |
| 真实长跑 | 已从第 36 章重启,观察是否还出现 `budget_exceeded` |

## 七、遗留

`COGNITION_HISTORY_MAX_CHARS=3000` 是按"1200 在 36 章处撑爆、要留足余量"估的。
若真实长跑仍出现该错误,日志里的 `required_chars` 会给出确切数字,按它调即可
(该值只影响这一段上下文的长度,不改变任何验收口径)。
