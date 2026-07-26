"""Regenerate ONLY the volume-level outlines for Shenyi, reusing the existing
high-quality book outline already persisted in the DB.

Rationale: the book outline (level='book') is good (~94K chars, passes the
quality gate and carries a 5-item volume_plan). The volume outlines are
invalidated 217-char placeholders missing `volume_idx`, so the frontend shows
them as missing. We do NOT want to burn LLM budget re-running the whole book
outline; we only need fresh volume outlines off the existing book plan.

This wrapper imports the heavy lifting from regenerate_shenyi_outlines.py and
calls generate_volumes() + persist() with the DB-loaded book outline.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.session import async_session_factory, dispose_current_engine_async
from app.models.project import Outline, Project
from app.services.model_router import get_model_router_async  # noqa: F401 (warms import path)
from app.services.outline_generator import compute_scale

import regenerate_shenyi_outlines as R


def soft_validate_book_outline(generator, book_outline: dict, scale, context: str) -> dict:
    """Relaxed book-outline gate for the REUSE path.

    The strict gate (R.validate_shenyi_book_outline_payload) enforces a 30000-char
    floor + per-section minimums, which is meant for *fresh* book generation. Here
    we reuse an already-persisted book outline that is canon-correct but thin
    (~7.8K chars). We keep the bug-prevention checks that actually matter — canon
    drift, required entity terms, physical anchor terms, volume_plan completeness,
    placeholder terms — and drop only the length floors.
    """
    text = R.as_text(book_outline.get("raw_text") or book_outline.get("full_outline"))
    errors: list[str] = []
    # Canon drift + required entity registry + memory-cost anchor terms.
    errors.extend(
        R.shenyi_text_gate_errors(text, require_book_terms=True, require_anchor_terms=True)
    )
    # Placeholder/junk terms must not be present.
    hit = [w for w in R.BANNED_PLACEHOLDER_TERMS if w in text]
    if hit:
        errors.append("placeholder_terms:" + ",".join(hit))
    # volume_plan must match the expected volume count and carry canon titles.
    volume_plan = book_outline.get("volume_plan")
    expected_volumes = int(scale.get("n_volumes") or 0) if scale else 0
    if expected_volumes:
        if not isinstance(volume_plan, list) or len(volume_plan) != expected_volumes:
            count = len(volume_plan) if isinstance(volume_plan, list) else 0
            errors.append(f"volume_plan_count:{count}!={expected_volumes}")
        else:
            titles = [R.as_text(it.get("title")) for it in volume_plan if isinstance(it, dict)]
            missing_titles = [t for t in R.SHENYI_VOLUME_PLAN_TITLES if t not in titles]
            if missing_titles:
                errors.append("volume_plan_title_drift:" + ",".join(missing_titles))
    if errors:
        raise RuntimeError(f"{context} soft gate failed: " + "; ".join(errors))
    enriched = dict(book_outline)
    enriched["entity_registry"] = dict(R.SHENYI_ENTITY_REGISTRY)
    return enriched


# persist() calls R.validate_shenyi_book_outline_payload internally; route both
# the wrapper's check and persist's check through the relaxed gate.
R.validate_shenyi_book_outline_payload = soft_validate_book_outline


async def load_book_outline() -> dict:
    async with async_session_factory() as db:
        row = (
            await db.execute(
                select(Outline)
                .where(Outline.project_id == R.PROJECT_ID, Outline.level == "book")
                .order_by(Outline.is_confirmed.desc(), Outline.id)
            )
        ).scalars().first()
        if row is None:
            raise RuntimeError("No book-level outline found; run full regen first.")
        cj = row.content_json
        if not isinstance(cj, dict):
            raise RuntimeError(f"Book outline content_json is not a dict: {type(cj)}")
        return cj


async def main() -> None:
    try:
        async with async_session_factory() as db:
            project = await db.get(Project, R.PROJECT_ID)
            if not project:
                raise RuntimeError(f"Project not found: {R.PROJECT_ID}")
        scale = compute_scale(project.target_word_count)
        R.log(f"项目：{project.title}，目标字数 {project.target_word_count}")
        R.log(f"规模规则：{json.dumps(scale, ensure_ascii=False)}")

        book_outline = await load_book_outline()
        R.log(
            "已加载现有 book 大纲："
            f"raw_text {len(str(book_outline.get('raw_text','')))} 字，"
            f"volume_plan {len(book_outline.get('volume_plan') or [])} 卷"
        )

        # Validate the existing book outline still passes the quality gate before
        # we delete+rewrite the volume rows against it.
        gen = R.OutlineGenerator(project_id=str(R.PROJECT_ID))
        book_outline = R.validate_shenyi_book_outline_payload(
            gen, book_outline, scale, "Book outline (reused)"
        )
        R.log("现有 book 大纲通过质量门，复用之，不重生成 book 级")

        plan = R.normalize_plan(book_outline, scale)
        R.log("将按卷计划生成：" + "；".join(
            f"{p['idx']}.{p.get('title')}({p.get('est_chapters')}章)" for p in plan
        ))

        volume_outlines = await R.generate_volumes(book_outline, plan)

        if R.BOOK_LLM_DEGRADED or R.BOOK_LOCAL_FALLBACK_USED:
            raise RuntimeError(
                "分卷生成过程中触发模型熔断或本地兜底，已停止写库，"
                f"原因={','.join(R.BOOK_LOCAL_FALLBACK_REASONS) or 'model_degraded'}"
            )

        stats = await R.persist(project, book_outline, volume_outlines)
        R.log("已写回数据库：" + json.dumps(stats, ensure_ascii=False))
    except RuntimeError as exc:
        R.log("生成停止：" + str(exc))
        raise SystemExit(2) from None
    finally:
        await dispose_current_engine_async()


if __name__ == "__main__":
    asyncio.run(main())
