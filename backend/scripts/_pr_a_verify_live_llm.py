"""PR-A-GEN-PIPELINE-FIX end-to-end verification WITH a real LLM call.

Unlike _pr_a_verify.py (which stubs the generator for fast wiring/persistence
regression), this script runs the full BatchGenerator pipeline against the
configured 'generation' endpoint. It exists to prove that the same pipeline
that passes the stub test also produces real LLM output and writes it to
the chapters table.

Run inside the backend container:

    docker exec ai-write-backend-1 python /app/scripts/_pr_a_verify_live_llm.py
"""
from __future__ import annotations
import asyncio, sys, traceback, uuid
sys.path.insert(0, '/app')

from sqlalchemy import select
from app.db.session import async_session_factory
from app.models.project import Chapter, Project, Volume
from app.services.batch_generator import BatchGenerator


async def main() -> int:
    try:
        return await _main()
    except Exception as e:
        print('FATAL:', type(e).__name__, e, flush=True)
        traceback.print_exc()
        return 2


async def _main() -> int:
    async with async_session_factory() as db:
        # Seed minimal project/volume/chapter with an outline.
        pid = uuid.uuid4()
        project = Project(
            id=pid,
            title='PR-A live LLM verify',
            genre='现代',
            premise='主角是一名民间匠人，在青石小镇重新拾起祖传手艺。',
            settings_json={},
            target_word_count=200,
        )
        db.add(project)
        await db.flush()

        vol = Volume(
            id=uuid.uuid4(),
            project_id=pid,
            title='第一卷',
            volume_idx=1,
            summary='开局。',
            target_word_count=50000,
        )
        db.add(vol)
        await db.flush()

        ch = Chapter(
            id=uuid.uuid4(),
            volume_id=vol.id,
            title='第一章  返乡',
            chapter_idx=1,
            outline_json={
                'summary': '主角陈平安乘火车返回青石小镇，在雨中推开祖传镇医堂的木门。',
                'beats': ['站台上雨中下车', '在街上遇见旧邻居', '推开镇医堂的门'],
            },
            target_word_count=200,
            status='draft',
        )
        db.add(ch)
        await db.flush()
        await db.commit()

        print(f'seeded project={pid} volume={vol.id} chapter={ch.id}', flush=True)

        # Run BatchGenerator with REAL ChapterGenerator (no stub).
        bg = BatchGenerator()
        configs = [{'chapter_id': str(ch.id), 'volume_id': str(vol.id), 'chapter_idx': 1}]

        progress: list[str] = []
        def on_progress(payload):
            try:
                msg = getattr(payload, 'status', None) or getattr(payload, 'phase', None) or str(payload)[:80]
            except Exception:
                msg = str(payload)[:80]
            progress.append(str(msg))
            print(' progress:', msg, flush=True)

        print('calling bg.generate_batch...', flush=True)
        job = await bg.generate_batch(
            project_id=str(pid),
            chapter_configs=configs,
            db=db,
            style_instruction='请控制在200-400字，独立成段，不要完结。',
            on_progress=on_progress,
        )
        print('job.status:', job.status, ' completed:', job.completed_chapters, '/', job.total_chapters, flush=True)

    # Re-open session: read chapter back
    async with async_session_factory() as db:
        row = (await db.execute(select(Chapter).where(Chapter.id == ch.id))).scalar_one()
        text = row.content_text or ''
        print(f'chapter row: status={row.status} word_count={row.word_count} content_len={len(text)}')
        print('--- HEAD ---')
        print(text[:300])
        print('--- /HEAD ---')

        ok = (row.status == 'completed' and len(text) >= 50)

        # Cleanup
        from app.models.project import Chapter as Ch2, Volume as V2, Project as P2
        await db.execute(Ch2.__table__.delete().where(Ch2.id == ch.id))
        await db.execute(V2.__table__.delete().where(V2.id == vol.id))
        await db.execute(P2.__table__.delete().where(P2.id == pid))
        await db.commit()

    print('RESULT:', 'PASS' if ok else 'FAIL')
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
