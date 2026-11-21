"""Dated cognition excerpts with complete source quotes and explicit omissions.

Matching only recalls records under their original labels. It cannot establish
presence, resolve aliases, merge facts or infer anyone's knowledge.
"""
import logging
from collections import defaultdict, deque
from dataclasses import dataclass
from itertools import chain, islice
import re

from app.services.context_errors import RequiredContextUnavailable
from app.services.entity_sources import digest


logger = logging.getLogger(__name__)
READER = '__reader__'
HEADER = ('以下为逐章认知记录，节选不代表全部认知。较早的‘未知记录’只描述当时，'
          '后章明确获知同一信息后不能再据旧记录断言其仍未知。转述、怀疑和误解须保留限定，不能当作客观事实。')


@dataclass(frozen=True)
class Observation:
    chapter: int
    order: int
    name: str
    text: str
    key: str


def recalled_labels(text: str, labels) -> list[str]:
    """Longest exact label at each occurrence; no contained-name credit."""
    names = sorted({name for name in labels if name and name != READER}, key=lambda name: (-len(name), name))
    if not text or not names:
        return []
    pattern = '|'.join(re.escape(name) for name in names)
    return list(dict.fromkeys(match.group() for match in re.finditer(pattern, text)))


def _round_robin_remaining(grouped, focus):
    """Visit only active histories; skewed casts take O(events + labels)."""
    active = deque(islice(grouped[name], 1, None) for name in focus)
    while active:
        history = active.popleft()
        event = next(history, None)
        if event is not None:
            yield event
            active.append(history)


def observation_key(name, item):
    return digest([name, item])


def observations(ledger):
    """Shared exact identities and display text; no semantic fact merging."""
    events = []
    for name, entry in ledger.items():
        for item in entry.get('history', []):
            label = '读者' if name == READER else name
            state = {'learns': '本章认知记录', 'still_unknown': '当时未知记录',
                     'believes': '当时确信（可能误信）', 'suspects': '当时怀疑'}[item['kind']]
            line = f"- [CH-{item['chapter_idx']}] {label}·{state}：{item['fact']}"
            if item.get('source_mode') == 'author':
                line += '（作者校订）'
            if item.get('evidence'):
                line += '\n  依据原文：' + item['evidence']
            events.append(Observation(item['chapter_idx'], len(events), name, line, observation_key(name, item)))
    return events


def history_fits(ledger, max_chars):
    return observations_fit(observations(ledger), max_chars)


def observations_fit(events, max_chars):
    return not events or len(HEADER) + sum(len(event.text) + 1 for event in events) <= max_chars


def render_history(ledger: dict, max_chars: int, *, priority_names=(), context_text='', ranked_keys=()) -> str:
    return render_observations(observations(ledger), max_chars, priority_names=priority_names,
                               context_text=context_text, ranked_keys=ranked_keys)


def render_observations(events, max_chars: int, *, priority_names=(), context_text='', ranked_keys=()) -> str:
    """Keep latest required records; recall older material with its latest peer.

    Similarity selects material only. An old hit cannot by itself establish
    someone's present knowledge. Optional hits and their latest companion
    fit together or are both omitted, preserving whole facts and quotes.
    """
    if not events:
        return ''
    newest = sorted(events, key=lambda e: (e.chapter, e.order), reverse=True)
    grouped = defaultdict(list)
    for event in newest:
        grouped[event.name].append(event)
    focus = list(dict.fromkeys(
        recalled_labels(context_text, grouped) + [n for n in priority_names if n in grouped]))
    if focus and READER in grouped and READER not in focus:
        focus.append(READER)
    minimum = [grouped[name][0] for name in focus]
    costs = {e.order: len(e.text) + 1 for e in events}
    truncated = len(HEADER) + sum(costs.values()) > max_chars

    def footer(count):
        return f'（认知记录共{len(events)}条，本次节选{count}条；未列出不表示人物不知。）'

    # Reserve the widest possible count, including the separator newline.
    remaining = max_chars - len(HEADER) - (len(footer(len(events))) + 1 if truncated else 0)
    if focus and sum(costs[e.order] for e in minimum) > remaining:
        # 规则本身是对的:每个必选人物的**最新一条(含依据原文)**必须装下,
        # 否则退化成"只给旧记录"会让模型以为人物还不知道,写出矛盾情节
        # (见 tests/test_cognition_context_selection.py 的 overflow 用例)。
        # 2026-11-18 的长跑事故(连撞 10 章、整批中断)根因不是这条规则,而是
        # **预算太小**:上游把全书人物卡名+别名都作为 priority_names 传进来,
        # 1200 字符在 36 章以后必然装不下。预算已改为可配置
        # COGNITION_HISTORY_MAX_CHARS(默认 3000);这条日志用于在真实长跑里
        # 观察"还差多少",便于继续调预算而不是回退语义。
        logger.warning(
            'cognition_context_budget_exceeded people=%d required_chars=%d '
            'remaining=%d budget=%d',
            len(minimum), sum(costs[e.order] for e in minimum), remaining, max_chars,
        )
        raise RequiredContextUnavailable('character_cognition_context_budget_exceeded')
    if remaining < 0:
        return ''
    selected = list(minimum)
    remaining -= sum(costs[e.order] for e in selected)
    selected_orders = {e.order for e in selected}
    by_key = {e.key: e for e in events}

    def select_with_companions(event):
        nonlocal remaining
        if event.order in selected_orders:
            return
        companions = [grouped[event.name][0], event]
        if READER in grouped:
            companions.insert(0, grouped[READER][0])
        unit = {e.order: e for e in companions if e.order not in selected_orders}
        cost = sum(costs[order] for order in unit)
        if cost <= remaining:
            selected.extend(unit.values())
            selected_orders.update(unit)
            remaining -= cost

    for key in ranked_keys:
        event = by_key.get(key)
        if event is not None:
            select_with_companions(event)
    focused = set(focus)
    # Round-robin prevents one prolific participant from taking all slots.
    ordered = chain(_round_robin_remaining(grouped, focus), (e for e in newest if e.name not in focused))
    for event in ordered:
        select_with_companions(event)
    parts = [HEADER] + [e.text for e in sorted(selected, key=lambda e: (e.chapter, e.order))]
    if truncated:
        parts.append(footer(len(selected)))
    return '\n'.join(parts)
