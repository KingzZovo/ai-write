"""把大纲里**已经声明的时间推进**钉进写手输入。

问题(2026-11-17 实测,100 章长跑):8 章被判 `time_progress_missing` ——
大纲写的是"次日清晨""一周后",正文通篇没有一句"过了多久"的说法,读者接不上
时间线。场景合同本来有 `time_delta` 字段,但兜底简报只给它一句
"按本章大纲推导,不得跳过关键时间成本",正常路径也依赖写手自觉;而写手在约
一万字上下文里会忽略规划层的这类声明(和金额钉子遇到的是同一个问题)。

做法与 `numeric_pins` 同构:只提取大纲/简报里**已经写明**的时间说法,渲染成几条
短指令放进写手 user content。不发明时间、不改大纲、不做剧情判断。
"""

from __future__ import annotations

import re

# 时段词:只用来判断"这句话是不是在说某一刻",不判断好坏。
DAY_PARTS: tuple[str, ...] = (
    "清晨", "早晨", "一早", "早上", "上午", "正午", "晌午", "中午", "午后",
    "下午", "傍晚", "黄昏", "入夜", "夜晚", "夜里", "深夜", "凌晨", "天亮",
    "破晓", "半夜", "当夜", "当晚", "当天",
)

# 相对推进词:与评测探针 continuity_probe._TIME_PROGRESS_MARKERS 同口径,
# 保证"我们钉的"和"我们量的"是同一件事。
PROGRESS_MARKERS: tuple[str, ...] = (
    "次日", "第二天", "隔天", "隔了一", "过了", "天后", "日后", "周后", "月后",
    "头一天", "前一天", "昨天", "昨日", "前天",
)

# 兜底简报里的占位话术:它不是时间声明,不能当成"大纲说了什么"。
_PLACEHOLDERS: tuple[str, ...] = (
    "按本章大纲推导", "按大纲推导", "不得跳过关键时间成本", "无",
)

# 提取顺序即优先级:先抓"明确的相对推进",再抓"时段"。
_DAY_PART_SUFFIX = "(?:一早|清晨|早上|上午|中午|午后|下午|傍晚|夜里|深夜|天亮|的早上|的傍晚)?"
_PROGRESS_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:次日|翌日|第二天|隔天|隔了一(?:天|夜|宿)?|头一天|前一天)" + _DAY_PART_SUFFIX),
    re.compile(
        r"[一二两三四五六七八九十百半几0-9]{1,3}个?(?:天|日|周|星期|月|年)(?:之?后|以后)" + _DAY_PART_SUFFIX
    ),
    re.compile(r"(?:过了|又过)[一二两三四五六七八九十百半几0-9]{1,3}个?(?:天|日|周|星期|月|年)"),
    re.compile(r"[一二两三四五六七八九十半几]{1,2}(?:天|日|周)过去"),
)
_DAY_PART_PATTERN = re.compile("|".join(sorted(DAY_PARTS, key=len, reverse=True)))

MAX_CUE_CHARS = 16


def _clean(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip()


def _is_placeholder(text: str) -> bool:
    if not text:
        return True
    return any(mark in text for mark in _PLACEHOLDERS) and not declares_progress(text)


def declares_progress(declared: str) -> bool:
    """这句话要求的是"往后推一段"吗?(与评测探针同判定,避免两边各说一套)"""
    text = _clean(declared)
    if not text:
        return False
    return any(mark in text for mark in ("后", "次日", "第二天", "第三", "次周"))


def extract_time_cue(*sources: object) -> str:
    """从若干候选文本里取出**最短的**时间声明;没有就返回空串。

    只认"已经写明的说法"(次日/一周后/十天后傍晚…)。占位话术
    (``按本章大纲推导``)不算声明,否则兜底简报会让每一章都要求交代时间。
    """
    for source in sources:
        text = _clean(source)
        if not text or _is_placeholder(text):
            continue
        for pattern in _PROGRESS_PATTERNS:
            match = pattern.search(text)
            if match:
                return match.group(0)[:MAX_CUE_CHARS]
        # 没有相对推进:退而求其次,只取一个时段,并要求句子本身在说时间。
        match = _DAY_PART_PATTERN.search(text)
        if match and any(mark in text for mark in ("章", "场", "接", "时", "晨", "夜")):
            return match.group(0)[:MAX_CUE_CHARS]
    return ""


def time_pin_lines(declared: str, *, limit: int = 3) -> list[str]:
    """一条声明渲染成 1–2 行短指令;空声明返回空列表。"""
    cue = _clean(declared)
    if not cue or _is_placeholder(cue):
        return []
    lines = [f"时间推进:{cue}"]
    if declares_progress(cue):
        lines.append(
            "落地方式:必须用叙事让读者看出「过了多久」(如「第二天一早」「一夜过去」),"
            "不要另起一段解释时间,也不要把这一段改成当天连续发生。"
        )
    else:
        lines.append("落地方式:正文要写出这个时段,读者才能定位这一刻。")
    return lines[: max(1, int(limit))]


def time_pin_block(declared: str) -> str:
    """写手 user content 用的一段;没有声明时返回空串(调用方可直接拼接)。"""
    lines = time_pin_lines(declared)
    if not lines:
        return ""
    body = "\n".join(f"- {line}" for line in lines)
    return (
        "\n\n【本章时间推进(大纲已声明,正文必须体现)】\n"
        f"{body}\n"
        "这条要求只约束时间交代,不改变本场该写什么。"
    )