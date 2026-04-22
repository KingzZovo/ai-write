"""ConStory v1 — item missing checker (v1.0 chunk 10).

Detects when an item is narrated as destroyed / lost in the draft, yet
the same item is still used/invoked in later paragraphs of the same draft.

Lightweight: scans the draft text only (no Neo4j lookup), keeping the
check deterministic and cheap. Works by:
 1) Gather candidate item names from context.items (optional parameter).
 2) For each item, find destroy/lose verb positions, then check whether the
    item name reappears with a use verb AFTER that position in the draft.

Output shape matches critic_service issues:
    {severity, category, desc, location, source}
"""
from __future__ import annotations

import logging
import re
from typing import Any, Iterable

logger = logging.getLogger(__name__)

# 毁坏/丢失谓词
LOSS_VERBS = (
    "碎了", "碎于", "碎裂", "打碎", "摔碎", "炒碎",
    "摧毁", "爆炸", "炸毁", "烧毁", "焰烈烧毁", "化为灰烬",
    "丢失", "遗失", "丢了", "丢掉", "丢失了", "不见了",
    "被夺走", "被掉包", "被抢走", "被收缴", "被没收",
)

# 使用谓词：在丢失/毁坏后出现这些动词的视为问题
USE_VERBS = (
    "提起", "握住", "握紧", "插上", "骑着", "挂上", "带着",
    "拿起", "拿着", "握着", "佩戴", "戴着", "拔出", "揮舞",
    "匙动", "呼出", "激发",
)

# 回退/恢复关键词：若丢失后出现「找回/修复/重铸/新的」则不算冲突
RESTORE_KEYWORDS = (
    "找回", "拾回", "修复", "修好", "重铸", "新的", "另一把",
    "替代", "赎回", "退还",
)


def _find_all(hay: str, needle: str) -> list[int]:
    out: list[int] = []
    start = 0
    while True:
        i = hay.find(needle, start)
        if i < 0:
            break
        out.append(i)
        start = i + len(needle)
    return out


async def scan_item_missing(
    draft: str,
    *,
    project_id: str,
    chapter_idx: int | None = None,
    item_names: Iterable[str] | None = None,
    neo4j_driver: Any | None = None,  # accepted for interface uniformity, unused
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    if not draft or not draft.strip():
        return issues

    items = [n for n in (item_names or []) if n and isinstance(n, str)]
    if not items:
        # 无显式实体清单时，退化为纯模式：检测「碎了/丢失」后相同短语再被「提起/握住」
        # 代价很高，暂不启用
        return issues

    for item in items:
        if item not in draft:
            continue
        loss_positions: list[int] = []
        for v in LOSS_VERBS:
            # item 在 v 前后 8 字符窗口内
            for pos in _find_all(draft, v):
                window = draft[max(0, pos - 12): pos + 12]
                if item in window:
                    loss_positions.append(pos)
        if not loss_positions:
            continue
        first_loss = min(loss_positions)
        after = draft[first_loss:]
        if any(kw in after for kw in RESTORE_KEYWORDS):
            continue
        # 在丢失后的文本中找 use verb + item
        hit = False
        for v in USE_VERBS:
            for pos in _find_all(after, v):
                window = after[max(0, pos - 12): pos + 12]
                if item in window:
                    hit = True
                    break
            if hit:
                break
        if hit:
            issues.append(
                {
                    "severity": "hard",
                    "category": "consistency_item_missing",
                    "desc": f"物品「{item}」在本章节被描述为碎坏/丢失，但后文仍有角色提起/握住/拔出该物品的描写。",
                    "location": item,
                    "source": "consistency:item_missing",
                }
            )
    return issues
