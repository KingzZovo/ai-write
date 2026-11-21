"""回归:章正文已落库但状态不是 completed 时,认知台账不许静默丢事实。

落库路径先写正文、再降级状态(api/generate.py → ``draft``,
tasks/generation_tasks.py → ``needs_review``)。此前 `load_ledger` 枚举台账来源
时写死 ``Chapter.status == 'completed'``,于是那一章正文里**已经发生的事实**会从
之后所有章节的认知台账里消失,而且没有任何日志或指标说明少了一章。

现在的语义:
* 边界之前、有正文的章,不论 status 都参与台账;
* 非 completed 章缺记录或校验失败 → 跳过该章但可见(warning + 计数器),其余章照常;
* completed 章照旧严格,校验失败仍抛 RequiredContextUnavailable,不放宽;
* 完全没有正文的章既不纳入也不报错。

只用 SQLite 真实 SQL,不联网、不调用模型。
"""
from __future__ import annotations

import logging
from datetime import datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Uuid, create_engine, insert, update
from sqlalchemy.orm import Session

from app.models.cognition import ChapterCognition as Record, CognitionRevision
from app.models.project import Chapter, Project, Volume
from app.observability.metrics import COGNITION_LEDGER_DEGRADED_TOTAL
from app.services.character_cognition import load_ledger
from app.services.context_errors import RequiredContextUnavailable
from app.services.entity_sources import digest, resolve_entity_source

LOGGER_NAME = 'app.services.character_cognition'
REASONS = ('missing_records', 'invalid_evidence', 'source_unavailable', 'boundary_unavailable')


def counters():
    return {reason: COGNITION_LEDGER_DEGRADED_TOTAL.labels(reason=reason)._value.get()
            for reason in REASONS}


class _Db:
    """The async surface `load_ledger` uses, over one synchronous SQLite session."""

    def __init__(self, session):
        self._session = session

    async def execute(self, statement):
        # asyncpg accepts UUID strings; SQLite's UUID bind requires UUID objects.
        # Adapt only that representation, then execute the real SQL.
        compiled = statement.compile()
        parameters = {}
        for key, value in compiled.params.items():
            if isinstance(compiled.binds[key].type, Uuid):
                def convert(item):
                    return UUID(item) if isinstance(item, str) else item
                parameters[key] = ([convert(item) for item in value]
                                   if isinstance(value, (list, tuple)) else convert(value))
        return self._session.execute(statement, parameters)

    async def scalar(self, statement):
        return (await self.execute(statement)).scalar()


class _Book:
    def __init__(self, session, pid, vid):
        self.session, self.pid, self.vid = session, pid, vid
        self.db = _Db(session)

    def chapter(self, index, body, status):
        cid = uuid4()
        # 生产库由 a1001949 触发器维护 content_hash;台账现在用它判断"这章有落库正文"
        # (而不是 detoast content_text),所以测试要照生产的形态把它写上。
        import hashlib as _hashlib
        self.session.execute(insert(Chapter).values(id=cid, volume_id=self.vid, chapter_idx=index,
            global_idx=index, title=f'第{index}章', content_text=body, status=status,
            word_count=len(body.strip()), content_hash=_hashlib.sha256(body.encode()).hexdigest()))
        self.session.commit()
        return cid

    async def record(self, cid, changes, *, status='completed'):
        """Persist the delta extraction would have written for this saved body."""
        source = await resolve_entity_source(self.db, self.pid, 0, cid, allow_review_drafts=True)
        self.session.execute(insert(Record).values(id=uuid4(), project_id=self.pid,
            chapter_id=UUID(str(cid)), source_hash=source.source_hash, source_json=source.receipt,
            changes_json=changes, payload_hash=digest([source.source_hash, changes]),
            source_mode='auto', cycle_id=uuid4(), status=status, attempt_count=1,
            updated_at=datetime(2026, 1, 1, 12, 0, 0)))
        self.session.commit()
        return source


@pytest.fixture
def ledger_db():
    engine = create_engine('sqlite://')
    for model in (Project, Volume, Chapter):
        model.__table__.create(engine)
    Record.__table__.create(engine)
    CognitionRevision.__table__.create(engine)
    with Session(engine) as session:
        pid, vid = uuid4(), uuid4()
        session.execute(insert(Project).values(id=pid, title='认知台账状态回归'))
        session.execute(insert(Volume).values(id=vid, project_id=pid, volume_idx=1, title='第一卷'))
        session.commit()
        yield _Book(session, pid, vid)
    engine.dispose()


def facts(ledger, name='沈青'):
    return [(item['chapter_idx'], item['fact']) for item in ledger[name]['history']]


@pytest.mark.asyncio
async def test_review_draft_with_valid_record_keeps_its_facts(ledger_db):
    """status 落后于正文:该章事实必须出现在后续章的台账里(改动前会消失)。"""
    r = ledger_db
    before = counters()
    first = r.chapter(1, '父亲把信收在抽屉里。', 'completed')
    await r.record(first, [{'character': '沈青', 'learns': '父亲收了信',
                            'evidence': '父亲把信收在抽屉里。'}])
    middle = r.chapter(2, '沈青终于明白父亲已经收到信。', 'needs_review')
    await r.record(middle, [{'character': '沈青', 'learns': '父亲已经收到信',
                             'evidence': '父亲已经收到信。'}])
    ledger = await load_ledger(r.db, r.pid, before_global_idx=3)
    assert facts(ledger) == [(1, '父亲收了信'), (2, '父亲已经收到信')]
    assert next(item for item in ledger['沈青']['history']
                if item['chapter_idx'] == 2)['source_chapter_id'] == str(middle)
    # 快照仍覆盖同一批章,四张表口径一致(弧规划核对依赖这一点)。
    assert set(ledger.snapshot['positions']) == {str(first), str(middle)}
    for key in ('sources', 'decisions', 'proofs'):
        assert set(ledger.snapshot[key]) == set(ledger.snapshot['positions'])
    assert counters() == before


@pytest.mark.asyncio
async def test_review_draft_without_record_is_skipped_visibly(ledger_db, caplog):
    """没有可用记录:不抛错、跳过该章,但 warning 与计数器必须留下痕迹。"""
    r = ledger_db
    before = counters()
    first = r.chapter(1, '父亲把信收在抽屉里。', 'completed')
    await r.record(first, [{'character': '沈青', 'learns': '父亲收了信',
                            'evidence': '父亲把信收在抽屉里。'}])
    middle = r.chapter(2, '沈青终于明白父亲已经收到信。', 'needs_review')
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        ledger = await load_ledger(r.db, r.pid, before_global_idx=3)
    assert facts(ledger) == [(1, '父亲收了信')]
    after = counters()
    assert after['missing_records'] == before['missing_records'] + 1
    assert after['invalid_evidence'] == before['invalid_evidence']
    assert 'cognition_ledger_degraded' in caplog.text
    for fragment in (str(middle), 'global_idx=2', 'status=needs_review', 'missing_records'):
        assert fragment in caplog.text


@pytest.mark.asyncio
async def test_review_draft_with_broken_record_is_skipped_visibly(ledger_db, caplog):
    """记录在但校验不过(改动正文/回执不符):同样只跳过,并标 invalid_evidence。"""
    r = ledger_db
    before = counters()
    first = r.chapter(1, '父亲把信收在抽屉里。', 'completed')
    await r.record(first, [{'character': '沈青', 'learns': '父亲收了信',
                            'evidence': '父亲把信收在抽屉里。'}])
    middle = r.chapter(2, '沈青终于明白父亲已经收到信。', 'needs_review')
    await r.record(middle, [{'character': '沈青', 'learns': '父亲已经收到信',
                             'evidence': '父亲已经收到信。'}])
    r.session.execute(update(Record).where(Record.chapter_id == middle).values(payload_hash='0' * 64))
    r.session.commit()
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        ledger = await load_ledger(r.db, r.pid, before_global_idx=3)
    assert facts(ledger) == [(1, '父亲收了信')]
    assert counters()['invalid_evidence'] == before['invalid_evidence'] + 1
    assert 'invalid_evidence' in caplog.text and str(middle) in caplog.text


@pytest.mark.asyncio
async def test_damaged_completed_record_still_blocks(ledger_db):
    """completed 章的严格性不放宽:同一批里出现 review draft 也照旧抛错。"""
    r = ledger_db
    first = r.chapter(1, '父亲把信收在抽屉里。', 'completed')
    await r.record(first, [{'character': '沈青', 'learns': '父亲收了信',
                            'evidence': '父亲把信收在抽屉里。'}])
    damaged = r.chapter(2, '沈青终于明白父亲已经收到信。', 'completed')
    await r.record(damaged, [{'character': '沈青', 'learns': '父亲已经收到信',
                              'evidence': '父亲已经收到信。'}])
    r.session.execute(update(Record).where(Record.chapter_id == damaged).values(payload_hash='0' * 64))
    r.session.commit()
    draft = r.chapter(3, '沈青没有再提这件事。', 'needs_review')
    await r.record(draft, [{'character': '沈青', 'learns': '沈青不再提信',
                            'evidence': '沈青没有再提这件事。'}])
    with pytest.raises(RequiredContextUnavailable):
        await load_ledger(r.db, r.pid, before_global_idx=4)


@pytest.mark.asyncio
async def test_chapter_without_prose_is_neither_included_nor_reported(ledger_db, caplog):
    """完全没有正文的章:不纳入台账,也不报错、不计数(与改动前一致)。"""
    r = ledger_db
    before = counters()
    first = r.chapter(1, '父亲把信收在抽屉里。', 'completed')
    await r.record(first, [{'character': '沈青', 'learns': '父亲收了信',
                            'evidence': '父亲把信收在抽屉里。'}])
    empty = r.chapter(2, '', 'needs_review')
    blank = r.chapter(3, '   \n  ', 'draft')
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        ledger = await load_ledger(r.db, r.pid, before_global_idx=4)
    assert facts(ledger) == [(1, '父亲收了信')]
    assert set(ledger.snapshot['positions']) == {str(first)}
    assert str(empty) not in ledger.snapshot['positions']
    assert str(blank) not in ledger.snapshot['positions']
    assert counters() == before
    assert 'cognition_ledger_degraded' not in caplog.text


@pytest.mark.asyncio
async def test_all_completed_path_is_unchanged(ledger_db, caplog):
    """全是 completed 的热路径:事实、快照形状、日志与计数器都与改动前一致。"""
    r = ledger_db
    before = counters()
    first = r.chapter(1, '父亲把信收在抽屉里。', 'completed')
    await r.record(first, [{'character': '沈青', 'learns': '父亲收了信',
                            'evidence': '父亲把信收在抽屉里。'}])
    second = r.chapter(2, '沈青终于明白父亲已经收到信。', 'completed')
    await r.record(second, [{'character': '沈青', 'learns': '父亲已经收到信',
                             'evidence': '父亲已经收到信。'}])
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        ledger = await load_ledger(r.db, r.pid, before_global_idx=3)
    assert facts(ledger) == [(1, '父亲收了信'), (2, '父亲已经收到信')]
    assert ledger.snapshot['boundary'] == 3
    assert ledger.snapshot['positions'] == {str(first): 1, str(second): 2}
    for key in ('sources', 'decisions', 'proofs'):
        assert set(ledger.snapshot[key]) == set(ledger.snapshot['positions'])
    assert counters() == before
    assert 'cognition_ledger_degraded' not in caplog.text