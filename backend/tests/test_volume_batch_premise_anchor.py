"""Regression: staged volume batches must stay on-genre even when V1 meta fails.

Live failure (2026-06-29 全流程测试): the V1 meta call returned correct
on-genre content (静默带 近未来科幻: 江临/顾成/声呐站) but with malformed,
truncated JSON (unterminated string). `_parse_json` failed → `_fallback_volume_meta`
produced an empty/generic meta → the V2 batch prompt (`batch_ctx`) carried ONLY
the compacted meta and NO book premise → the model defaulted to generic 武侠
tropes (林凡/天玄宗/青翠山), persisting chapter summaries completely unrelated to
the project. Silent, plausible-looking wrong content.

The batch prompt must carry a compact book-premise anchor so batches stay
on-genre regardless of whether V1 meta parsing succeeded.
"""
from __future__ import annotations

import json

import pytest

from app.services.outline_generator import OutlineGenerator


class _FakeResult:
    def __init__(self, text: str) -> None:
        self.text = text
        self.usage = None
        self.model = "fake"


class _RecordingRouter:
    """Captures every generate() call's user-message content.

    V1 meta call returns malformed JSON (forces fallback meta). V2 batch
    calls return well-formed sci-fi summaries — but we assert the prompt the
    batch SAW carries the book premise.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._meta_done = False

    async def generate(self, *, task_type, messages, max_tokens=None, **kwargs):
        user_content = ""
        for m in messages:
            if m.get("role") == "user":
                user_content = m.get("content", "")
        self.calls.append(user_content)
        if not self._meta_done:
            # V1 meta: on-genre but truncated/unterminated JSON → parse fails.
            self._meta_done = True
            return _FakeResult('{"volume_idx": 1, "title": "深水无声", "core_conflict": "江临在静默带')
        # V2 batch: return exactly the requested count of well-formed
        # sci-fi chapter summaries (honor start/end from the prompt).
        import re as _re
        m = _re.search(r"count=(\d+)", user_content)
        count = int(m.group(1)) if m else 5
        sm = _re.search(r"start=(\d+)", user_content)
        start = int(sm.group(1)) if sm else 1
        batch = {
            "batch": [
                {
                    "chapter_idx": start + j,
                    "title": f"静默第{start + j}章",
                    "summary": "江临在地下管网追查声呐站。",
                    "key_events": ["江临下潜", "顾成追查", "静默带扩散"],
                }
                for j in range(count)
            ]
        }
        return _FakeResult(json.dumps(batch, ensure_ascii=False))


# Mirrors the live failure: a THIN book outline persisted as raw_text only,
# with NO structured volume_plan. So _fallback_volume_meta finds no plan_item
# and produces an empty meta — the exact condition that stripped the premise
# from the batch prompt and let the model hallucinate off-genre.
BOOK_OUTLINE = {
    "raw_text": (
        "书名：失声之网\n核心概念：一个失声的管道维修工江临用预言墨水与时间赛跑，"
        "安全调查员顾成追查城市噪音消失的深海陷阱。近未来科幻悬疑。"
    ),
}


@pytest.mark.asyncio
async def test_volume_batch_prompt_carries_book_premise_when_meta_fails():
    gen = OutlineGenerator(project_id=None)
    gen.router = _RecordingRouter()

    result = await gen._generate_volume_outline_staged(BOOK_OUTLINE, volume_idx=1)

    # The merged volume outline must materialize chapter summaries.
    summaries = result.get("chapter_summaries") or []
    assert len(summaries) >= 5, f"expected >=5 chapter summaries, got {len(summaries)}: {result}"

    # The batch prompt (calls[1+]) MUST carry the book premise so the model
    # cannot drift off-genre. Before the fix it carried only the empty meta.
    batch_prompts = gen.router.calls[1:]
    assert batch_prompts, "no batch call was made"
    for bp in batch_prompts:
        assert ("江临" in bp) or ("失声之网" in bp) or ("声呐站" in bp), (
            "batch prompt lost the book premise; model would hallucinate off-genre. "
            f"prompt head: {bp[:300]}"
        )
