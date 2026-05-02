"""Semantic chunker for reference-book decompile (v0.6).

Respects natural boundaries: dialogue completeness, paragraph breaks,
scene transition cues. Bounded by SEMANTIC_CHUNKER_MAX_TOKENS (default 800).
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_MAX_TOKENS = int(os.getenv("SEMANTIC_CHUNKER_MAX_TOKENS", "800"))
# Minimum content size for a slice to be emitted standalone. Anything below
# this threshold is merged into the *next* slice instead of being flushed,
# so chapter/volume/section headings, stray short dialogue lines, structural
# tags ("序章", "尾声", "目录", etc.) never become standalone slices that the
# beat-extraction LLM would otherwise hallucinate against.
_MIN_TOKENS = int(os.getenv("SEMANTIC_CHUNKER_MIN_TOKENS", "80"))
# rough chinese/english hybrid: 1 token ≈ 1 han char ≈ 0.75 word
_APPROX_CHARS_PER_TOKEN = 1

# Scene transition cue phrases (zh). Matching these starts a new slice.
_SCENE_CUES = [
    r"^此时",
    r"^与此同时",
    r"^另一边",
    r"^另一头",
    r"^数天后",
    r"^数月后",
    r"^回说",
    r"^话说",
    r"^却说",
    r"^翅日",
]
_SCENE_RE = re.compile("|".join(_SCENE_CUES))

_CHAPTER_RE = re.compile(r"^\s*(?:第[\u4e00-\u9fa5\d]+[章回节]|Chapter\s*\d+)", re.IGNORECASE)

# Volume / part / structural headings that are NOT "第X章/回/节" but still
# act as semantic boundaries we never want to be alone in a slice.
#  - 正传·XXX（上/中/下） / 前传·XXX / 番外·XXX
#  - 序章 / 楽子 / 尾声 / 后记 / 预告 / 目录 / 未完待续
_STRUCTURE_RE = re.compile(
    r"^\s*(?:"
    r"(?:正传|前传|番外|外传|后传)\s*[··・:：\-]?"  # zh "sequel/prequel/spinoff" prefixes
    r"|序章|序言|序曲|楽子|引子|开篇|尾声|结语|后记|预告|目录|未完待续"
    r"|上卷|中卷|下卷|第\s*[\u4e00-\u9fa5\d]+\s*卷|Volume\s*\d+|Part\s*\d+"
    r")",
    re.IGNORECASE,
)


@dataclass
class Slice:
    sequence_id: int
    slice_type: str  # chapter|scene|paragraph
    chapter_idx: int | None
    start_offset: int
    end_offset: int
    raw_text: str
    token_count: int


def _approx_tokens(text: str) -> int:
    # crude but stable; PG column is just for budgeting/display
    return max(1, len(text) // _APPROX_CHARS_PER_TOKEN)


def chunk(text: str, *, max_tokens: int | None = None) -> list[Slice]:
    """Split raw book text into semantic slices.

    Rules (in order):
    1. Chapter heading starts new slice.
    2. Scene cue line starts new slice.
    3. Otherwise merge paragraphs until max_tokens.
    4. Slices below _MIN_TOKENS are never flushed standalone; they roll
       into the next slice. Only the very last slice in the book may be
       short (we still emit it so we don't drop the tail).
    """
    cap = max_tokens or _MAX_TOKENS
    floor = _MIN_TOKENS
    slices: list[Slice] = []
    if not text:
        return slices

    lines = text.splitlines(keepends=True)
    buf: list[str] = []
    buf_tokens = 0
    buf_start = 0
    cursor = 0
    chapter_idx = 0
    slice_type = "paragraph"
    seq = 0

    def _flush(end_offset: int, stype: str, ch_idx: int | None, *, force: bool = False):
        """Emit accumulated buf as a Slice.

        If buf size is below `floor` and `force` is False, keep accumulating
        instead of emitting (the short fragment will roll into the next
        slice). Pass `force=True` only at end-of-text to flush the tail.
        """
        nonlocal buf, buf_tokens, buf_start, seq
        if not buf:
            return
        raw = "".join(buf).strip()
        if not raw:
            buf = []
            buf_tokens = 0
            buf_start = end_offset
            return
        if not force and buf_tokens < floor:
            # Hold the fragment in buf so it merges with the next slice;
            # do NOT reset buf/buf_tokens/buf_start.
            return
        slices.append(
            Slice(
                sequence_id=seq,
                slice_type=stype,
                chapter_idx=ch_idx,
                start_offset=buf_start,
                end_offset=end_offset,
                raw_text=raw,
                token_count=_approx_tokens(raw),
            )
        )
        seq += 1
        buf = []
        buf_tokens = 0
        buf_start = end_offset

    for line in lines:
        stripped = line.strip()
        line_tokens = _approx_tokens(line)

        if _CHAPTER_RE.match(stripped):
            _flush(cursor, slice_type, chapter_idx)
            chapter_idx += 1
            slice_type = "chapter"
            buf = [line]
            buf_tokens = line_tokens
            buf_start = cursor
            cursor += len(line)
            # chapter heading by itself is too short to be a slice
            slice_type = "scene"
            continue

        if _STRUCTURE_RE.match(stripped):
            # Volume/part/structural heading: flush whatever is pending
            # (subject to the floor), then attach this heading to the next
            # slice's buf so it never stands alone.
            _flush(cursor, slice_type, chapter_idx)
            slice_type = "scene"
            buf.append(line)
            buf_tokens += line_tokens
            if not buf and not slices:
                buf_start = cursor
            elif not buf:
                buf_start = cursor
            cursor += len(line)
            continue

        if _SCENE_RE.match(stripped) and buf_tokens > 0:
            _flush(cursor, slice_type or "scene", chapter_idx)
            slice_type = "scene"

        if buf_tokens + line_tokens > cap and buf_tokens > 0:
            _flush(cursor, slice_type or "paragraph", chapter_idx)
            slice_type = "paragraph"

        buf.append(line)
        buf_tokens += line_tokens
        cursor += len(line)

    _flush(cursor, slice_type or "paragraph", chapter_idx, force=True)
    return slices
