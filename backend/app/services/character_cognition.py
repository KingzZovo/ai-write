"""角色认知账本（character cognition ledger）。

Adapted from QMAI character-cognition (MIT, github.com/Mochocyang/QMAI).

每个项目维护一份账本：每个角色两个字符串列表 ``knows`` / ``does_not_know``，
外加全局读者视角（``character_name='__reader__'`` 行，只用 ``knows``）。
「读者已知 - 角色未知」即信息差/悬念基础。三个接线点：

1. 章节定稿后 LLM 抽取本章认知变化并合并（:func:`extract_and_update`）；
2. 生成下一章时序列化注入 ContextPack（:func:`serialize_for_prompt`）；
3. 评审 prompt 注入账本，配合 narrative_contract 的 ``cognition_violation``
   违规标签（角色提前知道不该知道的信息——AI 长篇高频穿帮）。

ledger 内存形态：``{character_name: {"knows": [...], "does_not_know": [...]}}``。
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

READER = "__reader__"

EXTRACTION_PROMPT = """从本章正文中抽取认知变化，只输出 JSON 数组，每项形如：
{"character": "角色名或__reader__", "learns": "得知的信息（一句话）"} 或
{"character": "角色名", "still_unknown": "该角色仍不知道的关键信息（一句话）"}
只记录影响后续剧情的关键信息差，最多 10 条。没有则输出 []。"""

# Cap extraction input like chapter_summarizer: head + tail keeps the opening
# reveal and the chapter-end twist, which is where cognition changes cluster.
_MAX_INPUT_CHARS = 6000

# Per-character cap on each of knows / does_not_know. Without a bound the
# lists grow by up to 10 entries per chapter forever; with serialization
# budgeted at ~1200 chars an unbounded ledger only wastes DB rows and skews
# the entry-count importance ordering. Oldest facts (list head) are evicted
# first — the tail holds the most recent chapter's additions.
MAX_FACTS_PER_CHARACTER = 30


# ---------------------------------------------------------------------------
# Pure logic
# ---------------------------------------------------------------------------


def _copy_ledger(ledger: dict) -> dict:
    out: dict[str, dict[str, list[str]]] = {}
    for name, entry in (ledger or {}).items():
        entry = entry or {}
        out[str(name)] = {
            "knows": [str(x) for x in (entry.get("knows") or [])],
            "does_not_know": [str(x) for x in (entry.get("does_not_know") or [])],
        }
    return out


def apply_changes(ledger: dict, changes: list[dict]) -> dict:
    """Merge extracted cognition changes into a ledger; returns a new dict.

    - ``learns``: 加入 knows 并从 does_not_know 移除（去重）。
    - ``still_unknown``: 加入 does_not_know；若该信息已在 knows 中则忽略，
      绝不把已知信息降级回未知。读者行只用 knows，still_unknown 被忽略。

    Malformed entries are skipped silently — extraction output is LLM JSON
    and must never be able to crash the post-save pipeline.
    """
    out = _copy_ledger(ledger)
    for change in changes or []:
        if not isinstance(change, dict):
            continue
        name = str(change.get("character") or "").strip()
        if not name:
            continue
        learns = str(change.get("learns") or "").strip()
        still_unknown = str(change.get("still_unknown") or "").strip()
        if not learns and not still_unknown:
            continue
        entry = out.setdefault(name, {"knows": [], "does_not_know": []})
        if learns:
            if learns not in entry["knows"]:
                entry["knows"].append(learns)
            entry["does_not_know"] = [x for x in entry["does_not_know"] if x != learns]
        if still_unknown and name != READER:
            if still_unknown not in entry["knows"] and still_unknown not in entry["does_not_know"]:
                entry["does_not_know"].append(still_unknown)
    # Capacity bound: evict the oldest facts (list head) and keep the most
    # recent MAX_FACTS_PER_CHARACTER (list tail, where new entries land).
    for entry in out.values():
        if len(entry["knows"]) > MAX_FACTS_PER_CHARACTER:
            entry["knows"] = entry["knows"][-MAX_FACTS_PER_CHARACTER:]
        if len(entry["does_not_know"]) > MAX_FACTS_PER_CHARACTER:
            entry["does_not_know"] = entry["does_not_know"][-MAX_FACTS_PER_CHARACTER:]
    return out


def serialize_for_prompt(ledger: dict, max_chars: int = 1200) -> str:
    """Render the ledger as a compact prompt block.

    Reader line 『读者已知：…』 comes FIRST — the reader-knows/character-
    doesn't-know gap is the suspense backbone and must never be the first
    thing truncated away. Remaining per-character lines 『X知道：a；b』 /
    『X不知道：c』 follow, characters with more ledger entries first.

    Truncation never ends mid-sentence: when a line does not fit the
    remaining budget, its oldest facts (list head) are dropped item by item
    until the newest ones fit; if not even the newest single fact fits, the
    whole line is skipped and later (shorter) lines still get a chance.
    Empty ledger returns "".
    """
    ledger = _copy_ledger(ledger)
    reader_entry = ledger.pop(READER, None)

    def _entry_weight(item: tuple[str, dict]) -> int:
        _, e = item
        return len(e["knows"]) + len(e["does_not_know"])

    # (prefix, facts) candidates in priority order: reader first, then
    # characters by entry count descending.
    candidates: list[tuple[str, list[str]]] = []
    if reader_entry and reader_entry["knows"]:
        candidates.append(("读者已知：", reader_entry["knows"]))
    for name, entry in sorted(ledger.items(), key=_entry_weight, reverse=True):
        if entry["knows"]:
            candidates.append((f"{name}知道：", entry["knows"]))
        if entry["does_not_know"]:
            candidates.append((f"{name}不知道：", entry["does_not_know"]))

    kept: list[str] = []
    used = 0
    for prefix, facts in candidates:
        sep_cost = 1 if kept else 0  # newline separator
        budget = max_chars - used - sep_cost - len(prefix)
        # Drop oldest facts (head) until the newest ones fit the budget.
        while facts and len("；".join(facts)) > budget:
            facts = facts[1:]
        if not facts:
            continue  # line unfittable; later, shorter lines may still fit
        line = prefix + "；".join(facts)
        kept.append(line)
        used += sep_cost + len(line)
    return "\n".join(kept)


# ---------------------------------------------------------------------------
# DB access
# ---------------------------------------------------------------------------


async def load_ledger(db: AsyncSession, project_id: Any) -> dict:
    """Load the full cognition ledger for a project. Empty dict if none."""
    from app.models.project import CharacterCognition

    result = await db.execute(
        select(CharacterCognition).where(
            CharacterCognition.project_id == str(project_id)
        )
    )
    ledger: dict[str, dict[str, list[str]]] = {}
    for row in result.scalars().all():
        ledger[row.character_name] = {
            "knows": list(row.knows or []),
            "does_not_know": list(row.does_not_know or []),
        }
    return ledger


async def save_ledger(db: AsyncSession, project_id: Any, ledger: dict) -> None:
    """Upsert the ledger: one row per character, updated in place."""
    from app.models.project import CharacterCognition

    result = await db.execute(
        select(CharacterCognition).where(
            CharacterCognition.project_id == str(project_id)
        )
    )
    existing = {row.character_name: row for row in result.scalars().all()}
    for name, entry in _copy_ledger(ledger).items():
        row = existing.get(name)
        if row is not None:
            row.knows = entry["knows"]
            row.does_not_know = entry["does_not_know"]
        else:
            db.add(
                CharacterCognition(
                    id=uuid.uuid4(),
                    project_id=str(project_id),
                    character_name=name,
                    knows=entry["knows"],
                    does_not_know=entry["does_not_know"],
                )
            )
    await db.commit()


# ---------------------------------------------------------------------------
# LLM extraction
# ---------------------------------------------------------------------------


def _parse_changes(raw: str) -> list[dict]:
    """Parse the extraction LLM output into a change list ([] on failure)."""
    text = (raw or "").strip()
    if text.startswith("```"):
        first_newline = text.index("\n") if "\n" in text else len(text)
        text = text[first_newline + 1 :]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Salvage a bare JSON array embedded in prose.
        start, end = text.find("["), text.rfind("]")
        if start < 0 or end <= start:
            return []
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return []
    if not isinstance(data, list):
        return []
    return [c for c in data if isinstance(c, dict)][:10]


async def extract_and_update(
    db: AsyncSession, project_id: Any, chapter_text: str
) -> dict:
    """Extract this chapter's cognition changes via LLM and merge + persist.

    Reuses task_type='evaluation' routing. JSON parse failure leaves the
    ledger untouched. Any exception is logged with a warning before being
    re-raised — callers wrap this in try/except so chapter persistence is
    never blocked by ledger trouble.
    """
    try:
        ledger = await load_ledger(db, project_id)
        text = chapter_text or ""
        if not text.strip():
            return ledger
        if len(text) > _MAX_INPUT_CHARS:
            text = (
                text[: int(_MAX_INPUT_CHARS * 0.7)]
                + "\n\n…(中部省略)…\n\n"
                + text[-int(_MAX_INPUT_CHARS * 0.3) :]
            )

        user_parts = [f"【本章正文】\n{text}"]
        current = serialize_for_prompt(ledger, max_chars=1200)
        if current:
            user_parts.append(f"【当前认知账本（增量更新参考，不要重复已有条目）】\n{current}")
        from app.services.model_router import get_model_router_async

        router = await get_model_router_async()
        result = await router.generate_with_tier_fallback(
            task_type="evaluation",
            messages=[
                {"role": "system", "content": EXTRACTION_PROMPT},
                {"role": "user", "content": "\n\n".join(user_parts)},
            ],
            temperature=0.2,
            max_tokens=800,
            _log_meta={"caller": "character_cognition.extract_and_update"},
        )
        changes = _parse_changes(result.text)
        if not changes:
            logger.info(
                "Cognition extraction produced no changes (project_id=%s)", project_id
            )
            return ledger
        new_ledger = apply_changes(ledger, changes)
        await save_ledger(db, project_id, new_ledger)
        logger.info(
            "Cognition ledger updated: %d change(s), %d character row(s) (project_id=%s)",
            len(changes),
            len(new_ledger),
            project_id,
        )
        return new_ledger
    except Exception as exc:
        logger.warning(
            "Cognition extract_and_update failed (project_id=%s): %s", project_id, exc
        )
        raise
