"""PR-A-GEN-PIPELINE-FIX in-process verification.

Proves that the 3 bugs found by the end-to-end smoke test are actually
fixed at the wiring level, without depending on whether the LLM tier is
configured (the smoke test would otherwise hang on an unconfigured
'generation' model).

- Bug A: BatchGenerator calls ChapterGenerator.generate with the current
  ``(*, project_id, volume_id, chapter_idx, db, chapter_id, user_instruction)``
  signature. Verified by introspection + a stubbed generator that asserts
  it received the new kwargs.
- Bug B: After successful generation the resulting text is written back
  to ``chapters.content_text`` / ``word_count`` / ``status``. Verified by
  reading the row back from the DB.
- Bug C: ``PUT /api/projects/{p}/chapters/{c}`` rejects empty content on
  a non-empty chapter unless ``force=true`` is sent. Verified by direct
  HTTPX call against the live backend.

Run inside the backend container:

    docker exec ai-write-backend-1 python /app/scripts/_pr_a_verify.py
"""

from __future__ import annotations

import asyncio
import inspect
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import jwt

sys.path.insert(0, "/app")

from app.config import settings  # noqa: E402
from app.db.session import async_session_factory  # noqa: E402
from app.models.project import Chapter, Project, Volume  # noqa: E402
from app.services.batch_generator import BatchGenerator  # noqa: E402
from app.services.chapter_generator import ChapterGenerator  # noqa: E402


FIXED_TEXT = "【PR-A verify】 " + ("这是一段被插入的测试文本。" * 80)


class _StubGenerator:
    """Stub that records call kwargs and returns canned text."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def generate(self, **kwargs):  # noqa: D401
        self.calls.append(kwargs)
        return FIXED_TEXT


async def _seed_project_and_chapter(db) -> tuple[str, str, str]:
    pid = uuid.uuid4()
    vid = uuid.uuid4()
    cid = uuid.uuid4()

    project = Project(
        id=pid,
        title="PR-A verify project",
        genre="现代都市",
        premise="verify",
        target_word_count=10000,
    )
    db.add(project)
    db.add(Volume(
        id=vid,
        project_id=pid,
        title="卷一",
        volume_idx=1,
        summary="verify",
        target_word_count=10000,
    ))
    db.add(Chapter(
        id=cid,
        volume_id=vid,
        title="第一章",
        chapter_idx=1,
        outline_json={"summary": "verify"},
        content_text="",
        word_count=0,
        status="draft",
        target_word_count=2000,
    ))
    await db.flush()
    await db.commit()
    return str(pid), str(vid), str(cid)


async def main() -> int:
    rc = 0
    print("==> Bug A: ChapterGenerator.generate signature check")
    sig = inspect.signature(ChapterGenerator().generate)
    params = list(sig.parameters)
    expected = ["project_id", "volume_id", "chapter_idx", "db", "chapter_id", "user_instruction"]
    missing = [p for p in expected if p not in params]
    print(f"   signature params={params}")
    if missing:
        print(f"   FAIL: missing params {missing}")
        rc = 1
    else:
        print("   OK")

    print("==> Bug A + B: BatchGenerator wiring + persistence")
    async with async_session_factory() as db:
        pid, vid, cid = await _seed_project_and_chapter(db)
        print(f"   seeded project={pid} volume={vid} chapter={cid}")

    async with async_session_factory() as db:
        bg = BatchGenerator()
        stub = _StubGenerator()
        bg.generator = stub  # type: ignore[assignment]

        # Also stub hooks so we don't depend on Neo4j / consistency state.
        class _OkHook:
            can_proceed = True
            errors: list[str] = []

        async def _pre(**kw):  # noqa: ARG001
            return _OkHook()

        async def _post(**kw):  # noqa: ARG001
            return None

        bg.hook_manager.run_pre_hooks = _pre  # type: ignore[assignment]
        bg.hook_manager.run_post_hooks = _post  # type: ignore[assignment]

        progress: list = []
        job = await bg.generate_batch(
            project_id=pid,
            chapter_configs=[{
                "chapter_id": cid,
                "volume_id": vid,
                "chapter_idx": 1,
                "outline": {"summary": "verify"},
            }],
            db=db,
            style_instruction="",
            on_progress=lambda j: progress.append(j.results[0].status),
        )

        print(f"   job.status={job.status} completed={job.completed_chapters}/{job.total_chapters}")
        print(f"   per-chapter: status={job.results[0].status} word_count={job.results[0].word_count} err={job.results[0].error!r}")
        print(f"   progress trace: {progress}")

        if not stub.calls:
            print("   FAIL: stub generator never called")
            rc = 1
        else:
            kw = stub.calls[0]
            wanted = {"project_id": pid, "volume_id": vid, "chapter_idx": 1, "chapter_id": cid}
            mismatches = {k: (kw.get(k), v) for k, v in wanted.items() if str(kw.get(k)) != str(v)}
            if mismatches:
                print(f"   FAIL: kwargs mismatch {mismatches}")
                rc = 1
            elif "db" not in kw:
                print("   FAIL: db not threaded through")
                rc = 1
            else:
                print("   OK: ChapterGenerator.generate called with new signature")

    # Re-open a fresh session so we see committed state.
    async with async_session_factory() as db:
        ch = await db.get(Chapter, uuid.UUID(cid))
        if ch is None:
            print("   FAIL: chapter row missing after batch")
            rc = 1
        else:
            print(f"   chapter row: status={ch.status} word_count={ch.word_count} content_len={len(ch.content_text or '')}")
            if (ch.content_text or "") != FIXED_TEXT:
                print("   FAIL: chapter.content_text was not persisted (Bug B not fixed)")
                rc = 1
            elif ch.status != "completed":
                print(f"   FAIL: chapter.status={ch.status} (expected completed)")
                rc = 1
            else:
                print("   OK: chapter content persisted to DB")

    print("==> Bug C: PUT chapter protect guard")
    token = jwt.encode(
        {"sub": "king", "exp": datetime.now(timezone.utc) + timedelta(hours=1)},
        settings.SECRET_KEY,
        algorithm="HS256",
    )
    base = os.environ.get("PR_A_BACKEND", "http://127.0.0.1:8000")
    async with httpx.AsyncClient(base_url=base, headers={"Authorization": f"Bearer {token}"}, timeout=10) as client:
        r = await client.put(
            f"/api/projects/{pid}/chapters/{cid}",
            json={"content_text": ""},
        )
        print(f"   PUT empty -> HTTP {r.status_code}")
        if r.status_code == 200:
            print("   FAIL: empty PUT accepted (Bug C not fixed)")
            rc = 1
        elif r.status_code == 400:
            try:
                body = r.json()
            except Exception:
                body = {}
            code = (body.get("detail") or {}).get("code") if isinstance(body.get("detail"), dict) else None
            print(f"   guard code={code}")
            if code == "chapter_protect_empty_content":
                print("   OK: empty content rejected with chapter_protect_empty_content")
            else:
                print("   FAIL: 400 returned but not the protect-empty-content code")
                rc = 1
        else:
            print(f"   FAIL: unexpected status {r.status_code}: {r.text[:200]}")
            rc = 1

        # Now force=true should succeed
        r = await client.put(
            f"/api/projects/{pid}/chapters/{cid}",
            json={"content_text": "", "force": True},
        )
        print(f"   PUT empty+force -> HTTP {r.status_code}")
        if r.status_code != 200:
            print("   FAIL: force=true did not override guard")
            rc = 1
        else:
            print("   OK: force=true overrides guard")

    # Cleanup
    async with async_session_factory() as db:
        proj = await db.get(Project, uuid.UUID(pid))
        if proj is not None:
            await db.delete(proj)
            await db.commit()
            print("   cleanup: project deleted")

    print("\nRESULT:", "PASS" if rc == 0 else "FAIL")
    return rc


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
