"""Deterministic per-chapter repetition evidence for the evaluator.

为什么需要它:项目文风契约(``narrative_quality_gates``)明写"重复描写要结合
语境判断;同一动作在新处境、不同回应或递进意义中可以准确重复,不为避重换成
失真的近义词",并且**不按词频清零**。所以章内重复**不能**做成硬闸 —— 那会正
面违反契约,还会把有意为之的递进重复一起判死。

真正的缺口是**评审看不见重复**:``chapter_evaluation`` 只看得到正文与前文,
而生成侧的"解码/续写重叠"会以句级模板循环的形式出现(实测 卷5-3「掌根裂痕
烫了#下」×33、卷3-52「他记得这些」×28、卷3-16「第#道线从腕侧起…」×12)。
段落级重复由 ``prose_sanitizer.collapse_duplicate_paragraphs`` 落库前折叠,
但**句级**循环它抓不到,评审又没被喂这个信号,于是整批进入 needs_review。

本模块只做"数数":把可数的东西数清楚交给模型,判断仍归模型。它是纯函数,
不碰数据库、不调模型,因此可离线测试。
"""
from __future__ import annotations

import re
from collections import Counter

# 数字(含中文数字)掩码成同一符号,这样"第一道/第二道/第十道"归为同一模板。
_NUMBER_RE = re.compile(r"[0-9零一二三四五六七八九十百千两]+")
# 句子切分:只按句末/分句标点与换行,不按顿号(顿号枚举单独统计)。
_CLAUSE_SPLIT_RE = re.compile(r"[。！？；!?;\n]+")
_ENUM_SPLIT_RE = re.compile(r"[、,，]")

MIN_CLAUSE_CHARS = 4   # 太短的句子(如"他停住")天然会重复,不计入
MIN_REPEAT = 4         # 同一模板出现 ≥ 该次数才作为证据上报
TOP_TEMPLATES = 3
MIN_ENUM_RUN = 6       # 顿号枚举:同模板连续 ≥ 该项数才上报


def _skeleton(clause: str) -> str:
    return _NUMBER_RE.sub("#", clause.strip())


def _clauses(text: str) -> list[str]:
    out: list[str] = []
    for chunk in _CLAUSE_SPLIT_RE.split(text or ""):
        chunk = chunk.strip()
        if len(chunk) >= MIN_CLAUSE_CHARS:
            out.append(chunk)
    return out


def _longest_enum_run(text: str) -> tuple[int, str]:
    """最长顿号枚举:同模板连续项数(如"第一道纹路、第二道纹路、…")。"""
    best, example = 0, ""
    for paragraph in re.split(r"\n+", text or ""):
        for sentence in _CLAUSE_SPLIT_RE.split(paragraph):
            items = [i.strip() for i in _ENUM_SPLIT_RE.split(sentence) if i.strip()]
            if len(items) < MIN_ENUM_RUN:
                continue
            run = 1
            for prev, cur in zip(items, items[1:]):
                run = run + 1 if _skeleton(cur) == _skeleton(prev) else 1
                if run > best:
                    best, example = run, _skeleton(cur)
    return best, example


def chapter_repetition_profile(text: str) -> dict:
    """Count章内同模板句与最长顿号枚举。纯计数,不下判断。"""
    clauses = _clauses(text)
    counts = Counter(_skeleton(c) for c in clauses)
    repeated = [(n, sk) for sk, n in counts.items() if n >= MIN_REPEAT]
    repeated.sort(key=lambda item: (-item[0], item[1]))
    enum_run, enum_example = _longest_enum_run(text)
    return {
        "clause_total": len(clauses),
        "max_repeat": repeated[0][0] if repeated else 0,
        "templates": repeated[:TOP_TEMPLATES],
        "enum_run": enum_run if enum_run >= MIN_ENUM_RUN else 0,
        "enum_example": enum_example,
    }


def render_repetition_block(profile: dict) -> str:
    """Render the evaluator evidence block; empty when nothing is worth reporting."""
    if not profile:
        return ""
    templates = profile.get("templates") or []
    enum_run = profile.get("enum_run") or 0
    if not templates and not enum_run:
        return ""
    lines = [
        "## 本章重复度统计(确定性计数,不是评分)",
        "以下数字由代码数出,不含判断。项目文风契约允许有叙事作用的准确重复"
        "(递进、强调、同一动作在新处境中重复);也允许为记忆丧失、机械流程等"
        "设计的有意复沓。请结合语境判断:是递进/强调/有意复沓,还是生成侧"
        "解码重叠造成的凑字数。后者应记为 issue 并给出可定位的引文。",
    ]
    if templates:
        lines.append(f"同一句式模板(数字已掩码为 #)在本章出现次数(仅列 ≥{MIN_REPEAT} 次):")
        for n, skeleton in templates:
            lines.append(f"  - {n} 次: {skeleton[:80]}")
    if enum_run:
        lines.append(
            f"最长顿号枚举: 连续 {enum_run} 项同模板"
            + (f"(如 {profile.get('enum_example', '')[:60]})" if profile.get("enum_example") else "")
        )
    return "\n".join(lines)
