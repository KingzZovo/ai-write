"""Full-flow verification harness for the 神裔 project (READ-ONLY to DB).

Exercises the FIXED code path in-process via the host .venv:
  1. book outline (staged)        -> save JSON artifact
  2. volume 1 outline             -> save JSON artifact
  3. chapter outlines for ch 1-3  -> save JSON artifacts
  4. prose for ch 1-3 via SceneOrchestrator.orchestrate_chapter_stream
     (this is the path that was falling back 6/6 before the fix)

It does NOT write to the DB. Outline objects are generated in memory and
dumped to /root/ai-write/backend/tmp/shenyi_flow/. Prose uses the orchestrator,
which only READS the pack/outline rows. We assert scene-plan fallback is NOT
used by checking the emitted scene titles are model-authored, not "场景 N".
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

# Load root .env BEFORE importing app.config so SECRET_KEY matches the live
# container (else Fernet decrypts the stored API key wrong -> relay 401).
_ROOT_ENV = Path(__file__).resolve().parents[2] / ".env"
for _line in _ROOT_ENV.read_text().splitlines():
    _line = _line.strip()
    if _line and not _line.startswith("#") and "=" in _line:
        _k, _v = _line.split("=", 1)
        os.environ.setdefault(_k, _v)
os.environ["DATABASE_URL"] = "postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/aiwrite"
os.environ["DISABLE_AUTH"] = "1"
# Force strict planner: if the fix works, fallback must NOT be needed.
os.environ["ALLOW_SCENE_PLANNER_FALLBACK"] = "0"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PROJECT_ID = "1bb6e0dd-e2d3-4eef-af70-f4a018e93f67"
OUT_DIR = Path(__file__).resolve().parents[1] / "tmp" / "shenyi_flow"
OUT_DIR.mkdir(parents=True, exist_ok=True)


def _log(msg: str) -> None:
    print(f"[flow] {msg}", flush=True)


async def _gen_outlines(db):
    """Stages 1-3: book -> volume 1 -> chapter outlines 1..3 (in memory)."""
    from sqlalchemy import select
    from app.models.project import Project
    from app.services.model_router import get_model_router_async
    from app.services.outline_generator import OutlineGenerator, compute_scale
    from scripts.regenerate_shenyi_outlines import SHENYI_CANONICAL_FACTS_PROMPT

    project = (await db.execute(select(Project).where(Project.id == PROJECT_ID))).scalar_one()
    scale = compute_scale(project.target_word_count)
    _log(f"项目 {project.title} 目标 {project.target_word_count} 字；scale={scale}")

    gen = OutlineGenerator(project_id=PROJECT_ID)
    gen.router = await get_model_router_async()

    user_input = (
        f"书名：{project.title}\n类型：{project.genre or '未指定'}\n"
        f"创意/前提：{project.premise or ''}\n"
        f"规模：目标字数 {scale.get('target_word_count')}；卷数 {scale.get('n_volumes')}；"
        f"每卷约 {scale.get('chapters_per_volume')} 章{SHENYI_CANONICAL_FACTS_PROMPT}"
    )

    _log("=== Stage 1: 全书大纲 (staged) ===")
    book = await gen.generate_book_outline(user_input, staged=True, scale=scale)
    (OUT_DIR / "book_outline.json").write_text(json.dumps(book, ensure_ascii=False, indent=2))
    _log(f"book keys: {list(book.keys())[:12]}")

    _log("=== Stage 2: 第1卷大纲 ===")
    vol = await gen.generate_volume_outline(book, volume_idx=1)
    (OUT_DIR / "volume_1_outline.json").write_text(json.dumps(vol, ensure_ascii=False, indent=2))
    _log(f"vol keys: {list(vol.keys())[:12]}")

    _log("=== Stage 3: 第1卷 第1-3章大纲 ===")
    chapters = []
    prev_summary = ""
    for idx in (1, 2, 3):
        ch = await gen.generate_chapter_outline(book, vol, chapter_idx=idx,
                                                previous_chapter_summary=prev_summary)
        (OUT_DIR / f"chapter_{idx}_outline.json").write_text(
            json.dumps(ch, ensure_ascii=False, indent=2))
        prev_summary = (ch.get("summary") or ch.get("brief") or "")[:400]
        chapters.append(ch)
        _log(f"  ch{idx} keys: {list(ch.keys())[:10]}")
    return book, vol, chapters


def _build_pack(book, vol, chapter_outline):
    """Synthesize a ContextPack from in-memory outlines (no DB writes)."""
    from app.services.context_pack import ContextPack
    return ContextPack(
        current_outline=chapter_outline,
        volume_outline=vol,
        book_outline_excerpt=json.dumps(
            {"title": book.get("title"), "main_plot": book.get("main_plot"),
             "world": book.get("world") or book.get("world_setting"),
             "characters": book.get("characters")},
            ensure_ascii=False)[:6000],
    )


async def _gen_prose(db, book, vol, chapters):
    """Stage 4: plan scenes + write prose for ch 1-3 via the FIXED path."""
    from app.services.scene_orchestrator import SceneOrchestrator
    orch = SceneOrchestrator()
    results = []
    for ch in chapters:
        idx = ch.get("chapter_idx") or ch.get("idx") or (chapters.index(ch) + 1)
        pack = _build_pack(book, vol, ch)
        target = 3000
        _log(f"=== Stage 4.{idx}: plan_scenes (strict, fallback disabled) ===")
        briefs = await orch.plan_scenes(
            pack=pack, db=db, project_id=PROJECT_ID, chapter_id=None,
            target_words=target, n_scenes_hint=3,
        )
        titles = [b.title for b in briefs]
        used_fallback = all(t.startswith("场景 ") for t in titles)
        _log(f"  ch{idx}: {len(briefs)} scenes, titles={titles}, fallback={used_fallback}")

        _log(f"=== Stage 4.{idx}: write prose ({len(briefs)} scenes) ===")
        parts, prior = [], ""
        for b in briefs:
            buf = ""
            async for chunk in orch.write_scene_stream(
                scene=b, pack=pack, prior_scenes_summary=prior, db=db,
                project_id=PROJECT_ID, chapter_id=None,
            ):
                buf += chunk
            parts.append(buf)
            prior = (prior + "\n" + buf[-500:])[-1500:]
        prose = "\n\n".join(parts)
        (OUT_DIR / f"chapter_{idx}_prose.txt").write_text(prose)
        _log(f"  ch{idx}: {len(prose)} chars written")
        results.append({"idx": idx, "scenes": len(briefs), "chars": len(prose),
                        "used_fallback": used_fallback, "titles": titles})
    return results


async def main():
    from app.db.session import async_session_factory
    async with async_session_factory() as db:
        book, vol, chapters = await _gen_outlines(db)
        results = await _gen_prose(db, book, vol, chapters)
    (OUT_DIR / "summary.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))
    _log("=== DONE ===")
    for r in results:
        _log(json.dumps(r, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
