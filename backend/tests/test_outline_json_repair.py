"""Regression: truncated/unclosed batch JSON must be recovered, not discarded.

Live failure (2026-06-29 全流程测试, README「relay 批次级 JSON 健壮性」): when a
staged volume batch call returns truncated JSON (relay cuts the stream, leaving an
unterminated string or unclosed array/object), `_parse_json` ran only a
hand-rolled regex repair then `json.loads` — which still fails on an unclosed
structure — and returned `{"_parse_error": True}`. A single failed batch then
voided the ENTIRE volume outline (0 chapters materialized). The larger the volume
(more batches), the more likely one batch trips this.

The project already depends on `json-repair` and uses it as an L1 fallback in
`prompt_registry._try_parse_structured`. `_parse_json` must do the same: after
plain `json.loads` fails, try `json_repair.loads` so a truncated-but-recoverable
batch yields usable items instead of nuking the volume.
"""
from __future__ import annotations

import pytest

from app.services.outline_generator import OutlineGenerator


@pytest.fixture
def gen():
    return OutlineGenerator(project_id=None)


def test_well_formed_json_still_parses(gen):
    # Baseline: valid JSON must keep parsing exactly as before.
    parsed = gen._parse_json('{"batch": [{"chapter_idx": 1, "title": "开端"}]}')
    assert parsed.get("_parse_error") is not True
    assert parsed["batch"][0]["chapter_idx"] == 1


def test_recovers_truncated_unterminated_string(gen):
    # Relay cut the stream mid-string: the last summary value is never closed.
    # Plain json.loads fails; json_repair recovers the completed leading items.
    truncated = (
        '{"batch": ['
        '{"chapter_idx": 1, "title": "深水", "summary": "江临下潜追查声呐站。"}, '
        '{"chapter_idx": 2, "title": "静默", "summary": "顾成在管网里发现'
    )
    parsed = gen._parse_json(truncated)
    assert parsed.get("_parse_error") is not True, f"repair gave up: {parsed}"
    batch = parsed.get("batch")
    assert isinstance(batch, list) and len(batch) >= 1
    assert batch[0]["chapter_idx"] == 1
    assert batch[0]["title"] == "深水"


def test_recovers_unclosed_array_and_object(gen):
    # Trailing brackets never emitted — array and object left open.
    truncated = (
        '{"batch": ['
        '{"chapter_idx": 1, "title": "一", "summary": "第一章。"}, '
        '{"chapter_idx": 2, "title": "二", "summary": "第二章。"}'
    )
    parsed = gen._parse_json(truncated)
    assert parsed.get("_parse_error") is not True, f"repair gave up: {parsed}"
    batch = parsed.get("batch")
    assert isinstance(batch, list) and len(batch) == 2
    assert [b["chapter_idx"] for b in batch] == [1, 2]


def test_markdown_fenced_truncated_json_recovers(gen):
    # Fenced (```json) AND truncated — the fence strip must happen before repair.
    truncated = (
        "```json\n"
        '{"batch": [{"chapter_idx": 1, "title": "起", "summary": "开篇。"}, '
        '{"chapter_idx": 2, "title": "承'
    )
    parsed = gen._parse_json(truncated)
    assert parsed.get("_parse_error") is not True, f"repair gave up: {parsed}"
    batch = parsed.get("batch")
    assert isinstance(batch, list) and len(batch) >= 1
    assert batch[0]["title"] == "起"


def test_unrecoverable_garbage_still_flags_parse_error(gen):
    # Genuinely non-JSON prose must NOT be fabricated into a fake dict — the
    # caller relies on _parse_error to fall back / retry rather than persist junk.
    parsed = gen._parse_json("这完全不是 JSON，只是模型返回了一段道歉话术。")
    assert parsed.get("_parse_error") is True


class _MetaOnlyRouter:
    """V1 meta returns truncated JSON that json_repair now recovers into a
    PARTIAL meta (has title/core_conflict but NO chapter_count). V2 batches
    return well-formed summaries honoring start/end.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._meta_done = False

    async def generate(self, *, task_type, messages, max_tokens=None, **kwargs):
        import json as _json
        import re as _re
        user_content = ""
        for m in messages:
            if m.get("role") == "user":
                user_content = m.get("content", "")
        self.calls.append(user_content)
        if not self._meta_done:
            self._meta_done = True
            # On-genre but truncated: json_repair recovers {volume_idx, title,
            # core_conflict} — crucially WITHOUT a chapter_count field.
            return _FakeResult(
                '{"volume_idx": 1, "title": "深水无声", "core_conflict": "江临在静默带追查声呐站'
            )
        count_m = _re.search(r"count=(\d+)", user_content)
        count = int(count_m.group(1)) if count_m else 5
        start_m = _re.search(r"start=(\d+)", user_content)
        start = int(start_m.group(1)) if start_m else 1
        batch = {"batch": [
            {"chapter_idx": start + j, "title": f"静默第{start + j}章",
             "summary": "江临下潜追查声呐站。", "key_events": ["下潜", "追查"]}
            for j in range(count)
        ]}
        return _FakeResult(_json.dumps(batch, ensure_ascii=False))


class _FakeResult:
    def __init__(self, text: str) -> None:
        self.text = text
        self.usage = None
        self.model = "fake"


# Thin book outline with a volume_plan so _fallback_volume_meta can supply a
# chapter_count when the recovered meta lacks one.
_BOOK_OUTLINE_WITH_PLAN = {
    "raw_text": "近未来科幻悬疑：江临追查城市噪音消失的深海陷阱。",
    "volume_plan": [{"idx": 1, "title": "深水无声", "est_chapters": 6}],
}


@pytest.mark.asyncio
async def test_partial_recovered_meta_backfills_chapter_count(gen):
    # REGRESSION: json_repair now recovers a partial meta (no chapter_count).
    # int(None) must NOT abort V2 with 0 summaries — the count must be backfilled
    # from the fallback so chapters still materialize.
    gen.router = _MetaOnlyRouter()
    result = await gen._generate_volume_outline_staged(
        _BOOK_OUTLINE_WITH_PLAN, volume_idx=1
    )
    summaries = result.get("chapter_summaries") or []
    assert len(summaries) >= 1, f"partial meta aborted V2 with 0 chapters: {result}"
