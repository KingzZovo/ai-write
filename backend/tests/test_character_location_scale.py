"""Writer location assembly across five million characters; no services."""
import json
from time import perf_counter
from types import SimpleNamespace as NS
from uuid import UUID, uuid4

import pytest
from sqlalchemy import insert, update

from app.models.project import Chapter, CharacterLocation, Location
from app.services import entity_sources, entity_source_validation
from app.services.source_proof_cache import EvidenceProofCache
from tests.support.body_hashes import install_sqlite_body_hashes
from tests.test_character_location_context import cards
from tests.test_character_state_context import state_db


@pytest.mark.asyncio
async def test_five_million_character_writer_locations_reuse_only_exact_source_proofs(
        state_db, monkeypatch, record_testsuite_property):
    r = state_db
    install_sqlite_body_hashes(r.session.get_bind())
    monkeypatch.setattr(entity_source_validation, 'entity_evidence_proofs', EvidenceProofCache())
    prose = '陈川留在船舱核对借据，没有新的进账，也没有离开船舱。' * 210
    rows = [dict(id=uuid4(), volume_id=r.vid, chapter_idx=index, global_idx=index,
        title=f'第{index}章', content_text=prose, status='completed') for index in range(1, 1007)]
    r.session.execute(insert(Chapter), rows); r.session.commit()
    total = sum(len(row['content_text']) for row in rows)
    assert total >= 5_000_000
    sources = await entity_sources.resolve_entity_sources(r.db, r.pid, [row['id'] for row in rows])
    place_id = uuid4()
    r.session.execute(insert(Location).values(id=place_id, project_id=r.pid, name='船舱'))
    r.session.execute(insert(CharacterLocation), [dict(project_id=r.pid, character_id=r.character_id,
        location_id=place_id, chapter_start=source.global_idx, chapter_end=source.global_idx,
        source_chapter_id=UUID(source.chapter_id), source_hash=source.source_hash) for source in sources.values()])
    r.session.commit()
    reads = []
    execute = r.db.execute
    async def measured(statement):
        result = await execute(statement)
        if any(getattr(col, 'key', None) == 'content_text' for col in statement.selected_columns):
            items = result.all()
            reads.append(dict(rows=len(items), chars=sum(len(row.content_text or '') for row in items)))
            return NS(all=lambda: items)
        return result
    monkeypatch.setattr(r.db, 'execute', measured)
    async def render():
        return (await cards(r, monkeypatch, before=1007)).character_cards[0]
    start = perf_counter(); cold = await render(); cold_seconds = perf_counter() - start
    cold_reads = list(reads); reads.clear()
    assert len(cold_reads) == 6 and sum(row['chars'] for row in cold_reads) == total
    assert max(row['rows'] for row in cold_reads) <= 200
    assert cold.location == '船舱' and cold.location_source['global_idx'] == 1006
    start = perf_counter(); warm = await render(); warm_seconds = perf_counter() - start
    assert not reads and warm.to_prompt() == cold.to_prompt()
    last = rows[-1]['id']
    changed_prose = prose + '\n陈川没有离船，只是合上了最后一页借据。'
    r.session.execute(update(Chapter).where(Chapter.id == last).values(content_text=changed_prose)); r.session.commit()
    stale = await render()
    assert stale.location_source['global_idx'] == 1005 and not reads
    current = await entity_sources.resolve_entity_source(r.db, r.pid, 0, last)
    r.session.execute(update(CharacterLocation).where(CharacterLocation.source_chapter_id == last).values(source_hash=current.source_hash))
    r.session.commit(); reads.clear()
    changed = await render()
    assert reads == [dict(rows=1, chars=len(changed_prose))]
    assert changed.location_source['global_idx'] == 1006
    record_testsuite_property('writer_location_scale', json.dumps(dict(chapters=len(rows), prose_characters=total,
        cold_body_batches=len(cold_reads), warm_body_characters=0, invalidated_body_characters=0,
        new_proof_body_characters=len(changed_prose), cold_seconds=cold_seconds, warm_seconds=warm_seconds)))
