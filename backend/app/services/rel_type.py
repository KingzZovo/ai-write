"""Relationship type canonicalization.

Neo4j is the source of truth for extracted entities.

We still store relationship rows in Postgres for fast reads and for
downstream behavior/OOC checks. To keep that surface stable across
extractions, we canonicalize verbose/variant `rel_type` strings into a
compact token set.

This module is the single source of truth for canonicalization rules.
"""

from __future__ import annotations


def canonicalize_rel_type(raw: str | None) -> str:
    """Canonicalize an extracted relationship type into a stable token.

    Rules:
    - strip trailing explanations in parentheses / fullwidth parentheses
    - keep only the first token for slash combos ("A/B" -> "A")
    - map known keyword families into compact canonical labels
    - cap to 50 chars (DB field bound)
    """

    raw = (raw or "").strip()
    if not raw:
        return "other"

    rel_type = raw
    if "（" in rel_type:
        rel_type = rel_type.split("（", 1)[0].strip()
    if "(" in rel_type:
        rel_type = rel_type.split("(", 1)[0].strip()
    if "/" in rel_type:
        rel_type = rel_type.split("/", 1)[0].strip()

    # Keyword canonicalization should use the *original* raw string so that
    # explanatory suffixes still influence the mapping.
    if any(k in raw for k in ["敌对", "仇敌", "死敌"]):
        rel_type = "敌对"
    elif any(k in raw for k in ["对立", "不信任", "对手"]):
        rel_type = "对立"
    # Regulatory / enforcement actions are treated as 监管.
    elif any(
        k in raw
        for k in [
            "监管",
            "押解",
            "押送",
            "看押",
            "管辖",
            "盘查",
            "监控",
            "审查",
            "取证",
            "查档",
            "查档对照",
        ]
    ):
        rel_type = "监管"
    elif any(k in raw for k in ["审讯", "逼问"]):
        rel_type = "审讯"
    elif any(k in raw for k in ["师生", "师徒"]):
        rel_type = "师生"
    elif any(k in raw for k in ["上下级", "上位", "下属"]):
        rel_type = "上下级"
    elif any(k in raw for k in ["同舍", "同寝"]):
        rel_type = "同舍"
    elif any(k in raw for k in ["同伴", "同学", "同行", "协作"]):
        rel_type = "同伴"
    # Search / missing-person narratives are treated as 失联.
    elif any(k in raw for k in ["失联", "寻找"]):
        rel_type = "失联"

    rel_type = (rel_type or "other")[:50]
    return rel_type

