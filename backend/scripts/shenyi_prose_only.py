"""Prose-only re-run: reuse saved 神裔 outlines, generate prose for ch 1-3.

Uses the production fallback setting (ALLOW_SCENE_PLANNER_FALLBACK unset =
enabled) so a chapter whose planner drops a contract field falls back
gracefully instead of hard-failing. Reuses the outline JSON artifacts from
the full-flow run so we don't regenerate the 25-min volume outline.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

_ROOT_ENV = Path(__file__).resolve().parents[2] / ".env"
for _line in _ROOT_ENV.read_text().splitlines():
    _line = _line.strip()
    if _line and not _line.startswith("#") and "=" in _line:
        _k, _v = _line.split("=", 1)
        os.environ.setdefault(_k, _v)
os.environ["DATABASE_URL"] = "postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/aiwrite"
os.environ["DISABLE_AUTH"] = "1"
# Production behavior: planner fallback ENABLED (do not set to 0).

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PROJECT_ID = "1bb6e0dd-e2d3-4eef-af70-f4a018e93f67"
OUT_DIR = Path(__file__).resolve().parents[1] / "tmp" / "shenyi_flow"


def _log(msg: str) -> None:
    print(f"[prose] {msg}", flush=True)


def _build_pack(book, vol, chapter_outline):
    from app.services.context_pack import ContextPack
    return ContextPack(
        current_outline=chapter_outline,
        volume_outline=vol,
        book_outline_excerpt=json.dumps(
            {"title": book.get("title"), "main_plot": book.get("main_plot")},
            ensure_ascii=False)[:6000],
    )


async def main():
    from app.db.session import async_session_factory
    from app.services.scene_orchestrator import SceneOrchestrator

    book = json.loads((OUT_DIR / "book_outline.json").read_text())
    vol = json.loads((OUT_DIR / "volume_1_outline.json").read_text())
    orch = SceneOrchestrator()
    results = []
    async with async_session_factory() as db:
        for idx in (1, 2, 3):
            ch = json.loads((OUT_DIR / f"chapter_{idx}_outline.json").read_text())
            pack = _build_pack(book, vol, ch)
            _log(f"=== ch{idx}: plan_scenes (production fallback) ===")
            briefs = await orch.plan_scenes(
                pack=pack, db=db, project_id=PROJECT_ID, chapter_id=None,
                target_words=3000, n_scenes_hint=3,
            )
            titles = [b.title for b in briefs]
            used_fallback = all(t.startswith("场景 ") for t in titles)
            _log(f"  ch{idx}: {len(briefs)} scenes, titles={titles}, fallback={used_fallback}")

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
    (OUT_DIR / "prose_summary.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))
    _log("=== DONE ===")
    for r in results:
        _log(json.dumps(r, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
