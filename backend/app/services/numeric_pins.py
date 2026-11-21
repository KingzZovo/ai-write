"""把大纲里已经定下的金额整理成一小段提示,交给写作阶段。

长篇最容易出的错不是算错,是**忘了自己前面或后面写过多少**:第 7 章让角色
"算出这批活能挣多少",模型自己写了个 880;第 8 章大纲钉的是 820;读者一眼就看出来。

这里只做一件事:从大纲文本里挑出**带金额的短片段**,按章标注,交给写手当硬约束。
不判断谁对谁错,不改大纲,也不猜数——只把已经写明的那些摆到写手面前。

刻意只取金额前后各 ``window`` 个字,不整句抄:这一块是给写手的"数字约束",
不是"剧透下一章要发生什么"。
"""
from __future__ import annotations

import re

_DIGITS = "一二两三四五六七八九十百千万零"
# 带元/块后缀的写法一定算钱;裸数字要再排掉量词和时间。
_AMOUNT_RE = re.compile(rf"(\d+(?:\.\d+)?|[{_DIGITS}]{{2,}})\s*(元|块钱|块)?")
_NON_MONEY_UNITS = (
    "点", "分", "章", "月", "年", "天", "人", "套", "件", "岁", "米", "斤", "里",
    "层", "次", "回", "张", "条", "只", "把", "位", "名", "个", "页", "封", "台",
    "辆", "步", "声", "句", "碗", "顿",
)
_SENTENCE_MARKS = "。;!?!\n"

DEFAULT_WINDOW = 14
DEFAULT_LIMIT = 12


def amount_spans(text: str) -> list[re.Match]:
    """返回文本里所有像金额的位置。

    裸阿拉伯数字不当金额:「[VOL-1/CH-8]」这类编号会被误当成 1 和 8。
    没有元/块后缀时,只认两个汉字以上的中文数字(口语的"八百二""一千二")。
    """
    spans: list[re.Match] = []
    body = text or ""
    for match in _AMOUNT_RE.finditer(body):
        token, suffix = match.group(1), match.group(2)
        if not suffix:
            if token.isdigit() or len(token) < 2:
                continue
            if body[match.end(): match.end() + 1] in _NON_MONEY_UNITS:
                continue
        spans.append(match)
    return spans


def _fragment(text: str, start: int, end: int, window: int) -> str:
    low = start
    while low > 0 and start - low < window and text[low - 1] not in _SENTENCE_MARKS:
        low -= 1
    high = end
    while high < len(text) and high - end < window and text[high] not in _SENTENCE_MARKS:
        high += 1
    return text[low:high].strip(" ,、, ")


def pin_fragments(text: str, *, window: int = DEFAULT_WINDOW, limit: int = 3) -> list[str]:
    """摘出金额附近的短片段(不跨句),按出现顺序去重。"""
    fragments: list[str] = []
    body = text or ""
    for match in amount_spans(body):
        fragment = _fragment(body, match.start(), match.end(), window)
        if fragment and fragment not in fragments:
            fragments.append(fragment)
        if len(fragments) >= limit:
            break
    return fragments


def pin_block(blocks, *, window: int = DEFAULT_WINDOW, limit: int = DEFAULT_LIMIT) -> str:
    """``blocks`` 是 ``[(章节标签, 大纲文本), ...]``,返回可直接放进提示的若干行。

    同一段文字只列一次;超出 ``limit`` 行就停:这是给写手的约束清单,
    不是把大纲再抄一遍。
    """
    lines: list[str] = []
    seen: set[str] = set()
    for label, text in blocks:
        for fragment in pin_fragments(text or "", window=window):
            if fragment in seen:
                continue
            seen.add(fragment)
            lines.append(f"[{label}] {fragment}" if label else fragment)
            if len(lines) >= limit:
                return "\n".join(lines)
    return "\n".join(lines)


def outline_pin_blocks(volume_outline, future_outline_lines, *, extra=None) -> list[tuple[str, str]]:
    """把"卷纲分章梗概 + 后续走向行"整理成 ``[(标签, 文本)]``。

    写作阶段注入与写后核对必须走同一份口径,所以块构造也放在这里,
    不让第二处再拼一遍。

    ``extra`` 是优先级最高的前置块(实测用当前章计划里已定的金额):
    ``pin_block`` 按顺序取且有条数上限,所以本章自己定的数要排在前面。
    """
    blocks: list[tuple[str, str]] = list(extra or [])
    volume_outline = volume_outline if isinstance(volume_outline, dict) else {}
    for entry in volume_outline.get("chapter_summaries") or []:
        if not isinstance(entry, dict):
            continue
        label = f"CH-{entry.get('chapter_idx')}" if entry.get("chapter_idx") else "本卷"
        events = entry.get("key_events")
        if isinstance(events, list):
            events = " ".join(str(item) for item in events)
        else:
            events = str(events or "")
        blocks.append((label, f"{entry.get('title', '')} {entry.get('summary', '')} {events}"))
    for line in future_outline_lines or []:
        _prefix, _sep, body = (line or "").partition("》")
        blocks.append((chapter_label(line), (body or line).lstrip(": ")))
    return blocks


def chapter_outline_line(*, volume_idx, chapter_idx, chapter_id,
                         title: str = "", summary: str = "") -> str:
    """后章大纲在提示里的行格式。写后核对要按同一格式还原,故集中在这里。"""
    return (f"[VOL-{volume_idx}/CH-{chapter_idx}; id={chapter_id}]"
            f"《{title or ''}》: {summary}")


def chapter_label(raw: str) -> str:
    """从 ``[VOL-1/CH-8; id=...]《标题》: 摘要`` 这类行里取出 ``CH-8``。"""
    match = re.search(r"CH-(\d+)", raw or "")
    return f"CH-{match.group(1)}" if match else ""
