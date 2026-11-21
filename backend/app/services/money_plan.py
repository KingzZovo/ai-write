"""计划层金额收敛:计划自造的总额与大纲已定的数冲突时,由大纲接管。

实测(2026-11-17,真实两章链):加了 ``money_totals`` 字段之后,**写手**不再自己发明,
但**规划者**会发明 —— ch07 的计划写「工钱=八百四十元(三十套 × 每套二十八元)」,
而 ch08 大纲写的是"八百二"。两个阶段各自自洽,读者看到的仍是相邻两章对不上。

所以约束必须下在**计划**这一层:计划里与大纲冲突的金额条目丢掉,保留其余,
并明确告诉写手"以大纲已定的金额为准"。

设计边界(与 `numeric_check` 一致):
- **不抛错、不改 status**:金额对不上不该让整章写不出来;
- **大纲侧没有金额就不动**:没有权威值就无权改计划;
- 只做**条目级**收敛,不重写句子、不猜名目。
"""
from __future__ import annotations

import re

from app.services.numeric_ledger import parse_amount, planned_amounts_from_text

# 计划里的金额条目分隔:分号、逗号(半角/全角)、顿号、换行。
# 实测 ch08 写的是「工钱=820元(…), 铺面租金=800元(…), 结余现金=20元(…)」——
# 只按分号切会把三条并成一条,任一条与大纲不合就整条丢掉(820 也跟着没了)。
_ENTRY_SPLIT = re.compile(r"[;,、\n]+")
AUTHORITY_NOTE = "【以大纲已定的金额为准】"


def _entry_amounts(entry: str) -> set[float]:
    """取一条目里出现的所有数额。"""
    from app.services.numeric_pins import amount_spans

    values: set[float] = set()
    for match in amount_spans(entry):
        value = parse_amount(match.group(0))
        if value is not None:
            values.add(float(value))
    return values


def reconcile_money_totals(money_totals: str, pinned_text: str,
                           *, max_entries: int = 6) -> tuple[str, list[str]]:
    """返回 ``(收敛后的文本, 被丢弃的条目)``。

    - 条目里的数额全部出现在大纲钉子中 → 保留;
    - 与大纲冲突的条目 → 丢弃;
    - 有丢弃时追加一句"以大纲已定的金额为准",避免写手在多个数之间二选一;
    - 大纲侧没有任何金额 → 原样返回。
    """
    text = (money_totals or "").strip()
    if not text:
        return "", []
    pinned = planned_amounts_from_text(pinned_text or "")
    if not pinned:
        return text, []

    kept: list[str] = []
    dropped: list[str] = []
    for entry in (part.strip() for part in _ENTRY_SPLIT.split(text)):
        if not entry:
            continue
        amounts = _entry_amounts(entry)
        # 只有"与权威集合完全不相交"才算冲突:条目里只要出现了大纲写过的数,
        # 说明它引用的就是已定事实,不该整条丢掉(实测 ch08 的「工钱=820, 铺租=800」
        # 曾因按"非子集"判定被整串删掉)。
        if amounts and not (amounts & pinned):
            dropped.append(entry)
        else:
            kept.append(entry)
    if not dropped:
        return text, []

    body = "; ".join(kept[:max_entries])
    return (f"{body}\n{AUTHORITY_NOTE}" if body else AUTHORITY_NOTE), dropped