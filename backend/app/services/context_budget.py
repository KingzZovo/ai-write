"""Budget complete context blocks; never silently cut mandatory contracts.

Counts are conservative character estimates, not provider-token measurements.
Traces contain source names/counts/hashes, never novel text.
"""
from dataclasses import dataclass
import hashlib


class ContextBudgetExceeded(ValueError):
    """Mandatory context cannot fit; callers must not generate from fragments."""


@dataclass(frozen=True)
class ContextBlock:
    source: str
    text: str
    layer: str
    required: bool = False
    priority: int = 0
    keep_tail: bool = False


class ContextLayer:
    def __init__(self, layer: str):
        self.layer = layer
        self.blocks: list[ContextBlock] = []

    def add(self, source, text, *, required=False, priority=0, keep_tail=False):
        if text:
            self.blocks.append(ContextBlock(source, text, self.layer, required, priority, keep_tail))


HEADINGS = {'roster': '', 'proximity': '=== 叙事上下文 ===\n',
            'facts': '=== 事实约束 ===\n', 'reference': '=== 细节参考 ===\n',
            'instructions': '=== 创作指令 ===\n'}
WEIGHTS = {'proximity': .40, 'facts': .33, 'reference': .20, 'instructions': .07}


def allocate_context(blocks: list[ContextBlock], char_budget: int) -> tuple[str, list[dict]]:
    if char_budget <= 0:
        raise ValueError('上下文预算必须大于零')
    trace = [{ 'source': b.source, 'layer': b.layer, 'required': b.required,
               'input_chars': len(b.text), 'included_chars': 0, 'status': 'omitted_budget',
               'sha256': hashlib.sha256(b.text.encode()).hexdigest() } for b in blocks]
    # Reserve separators and every possible layer heading before allocating.
    overhead = sum(len(h) for h in HEADINGS.values()) + 2 * (len(blocks) + len(HEADINGS))
    available = max(0, char_budget - overhead)
    mandatory = sum(len(b.text) for b in blocks if b.required)
    if mandatory > available:
        sources = ', '.join(b.source for b in blocks if b.required)
        raise ContextBudgetExceeded(
            f'上下文预算不足：必要事实与本章任务需要至少 {mandatory + overhead} 字符，'
            f'当前预算 {char_budget}；请增加上下文预算或缩小必要上下文范围（{sources}）'
        )
    selected = {}
    for i, b in enumerate(blocks):
        if b.required:
            selected[i] = b.text
    remaining = available - mandatory
    # Soft slots remain weighted so optional retrieval cannot crowd out recent
    # context. Mandatory facts can borrow across all slots before this step.
    for layer, weight in WEIGHTS.items():
        allowance = int(remaining * weight)
        candidates = sorted((i for i, b in enumerate(blocks) if b.layer == layer and not b.required),
                            key=lambda i: (-blocks[i].priority, -i))
        for i in candidates:
            b = blocks[i]
            if len(b.text) <= allowance:
                selected[i] = b.text
            elif b.keep_tail:
                heading, _, body = b.text.partition('\n')
                prefix = heading + '\n[仅保留最近内容；更早内容因预算省略]\n'
                room = allowance - len(prefix)
                if room > 0:
                    selected[i] = prefix + body[-room:]
            if i in selected:
                allowance -= len(selected[i])
    sections = []
    for layer, heading in HEADINGS.items():
        texts = [selected[i] for i, b in enumerate(blocks) if b.layer == layer and i in selected]
        if texts:
            sections.append(heading + '\n\n'.join(texts))
    for i, text in selected.items():
        trace[i]['included_chars'] = len(text)
        trace[i]['status'] = 'included' if text == blocks[i].text else 'tail_only'
    return '\n\n'.join(sections), trace
