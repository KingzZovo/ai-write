"""Offline tests for the deterministic foreshadow setup check.

No database, no model call: plan entries come from a stub and the chapter body
comes from a fake result row. The position/boundary objects are the real ones
built over a small fake book so the chapter-scoping logic is exercised.
"""
from __future__ import annotations

from collections import Counter
from uuid import UUID, uuid5

import pytest
from fastapi import HTTPException

from app.services import foreshadow_setup_check as module
from app.services.foreshadow_scope import ForeshadowBoundary, ForeshadowPositions
from app.services.foreshadow_setup_check import (
    ForeshadowSetupChapterUnavailable,
    check_chapter_foreshadow_setup,
    keyword_tokens,
)

_NS = UUID('8f2c1c1e-0000-4000-8000-0000000000aa')
_MISSING = object()


def chapter_id_at(volume_idx, chapter_idx):
    return str(uuid5(_NS, f'ch-{volume_idx}-{chapter_idx}'))


def project_id():
    return str(uuid5(_NS, 'project'))


def positions(volume_size):
    """Real ForeshadowPositions for a book with len(volume_size) volumes."""
    volumes, chapters = {}, {}
    volume_counts, local_counts, global_counts = Counter(), Counter(), Counter()
    local_ids, global_ids = {}, {}
    global_idx = 0
    for volume_idx, size in enumerate(volume_size, start=1):
        volume_key = str(uuid5(_NS, f'vol-{volume_idx}'))
        volumes[volume_key] = {'id': volume_key, 'volume_idx': volume_idx,
                               'title': f'第{volume_idx}卷'}
        volume_counts[volume_idx] += 1
        for local_idx in range(1, size + 1):
            global_idx += 1
            key = chapter_id_at(volume_idx, local_idx)
            chapters[key] = {'id': key, 'volume_id': volume_key, 'chapter_idx': local_idx,
                             'global_idx': global_idx, 'status': 'draft',
                             'title': f'第{local_idx}章'}
            local_counts[(volume_key, local_idx)] += 1
            global_counts[global_idx] += 1
            local_ids[(volume_key, local_idx)] = key
            global_ids[global_idx] = key
    return ForeshadowPositions(volumes, chapters, volume_counts, local_counts,
                               global_counts, local_ids, global_ids)


def plan(chapter, description, *, hint=None, typ='伏笔'):
    """A plan entry shaped exactly like read_foreshadow_plans output."""
    return {
        'id': str(uuid5(_NS, f"plan-{chapter}-{description}-{hint}")),
        'source_kind': 'chapter_outline',
        'source_id': str(uuid5(_NS, f'source-{chapter}')),
        'volume_id': str(uuid5(_NS, 'vol-1')),
        'volume_idx': 1,
        'chapter_id': chapter,
        'chapter_idx': 2,
        'planted_chapter': 2,
        'description': description,
        'type': typ,
        'hint': '' if hint is None else hint,
        'status': 'planned',
        'source_label': '第1卷第2章(全书位置2)',
        'is_confirmed': None,
    }


class FakeResult:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class FakeDB:
    """Answers exactly the one column query the module issues for the body."""

    def __init__(self, text):
        self.text = text
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return FakeResult(None if self.text is _MISSING else (self.text,))


def wire(monkeypatch, *, plans, position_index, text):
    """Replace the two DB-backed readers; keep the real report logic."""
    calls = []

    async def fake_positions(db, pid):
        calls.append(('positions', str(pid)))
        return position_index

    async def fake_plans(db, pid, *, positions=None, boundary=None):
        calls.append(('plans', boundary, positions))
        return {'plans': list(plans), 'unavailable_sources': 0}

    monkeypatch.setattr(module, 'load_foreshadow_positions', fake_positions)
    monkeypatch.setattr(module, 'read_foreshadow_plans', fake_plans)
    return FakeDB(text), calls


@pytest.mark.asyncio
async def test_planned_setup_present_in_prose_is_grounded(monkeypatch):
    target = chapter_id_at(1, 2)
    prose = '夜里他把信封收进抽屉，寄信的存根还压在字典下面，父亲的名字清楚可见。'
    db, calls = wire(monkeypatch, position_index=positions([3, 3]),
                     plans=[plan(target, '寄信的存根'),
                            plan(chapter_id_at(1, 1), '另一个无关计划')],
                     text=prose)

    report = await check_chapter_foreshadow_setup(
        db, project_id=project_id(), chapter_id=target)

    # Only this chapter's plan is judged, not the other chapter's entry.
    assert report.planned == 1 and report.grounded == 1 and report.missing == []
    item = report.items[0]
    assert item.keywords == ('寄信', '存根')
    assert item.grounded is True
    assert '存根' in item.evidence
    assert '信封' in item.evidence  # real context, not just the bare keyword
    assert len(item.evidence) <= 120
    # The plan read was bounded to this chapter's outline position.
    assert calls[1][1] == ForeshadowBoundary(volume_idx=1, chapter_idx=2, global_idx=2)


@pytest.mark.asyncio
async def test_absent_setup_is_missing(monkeypatch):
    target = chapter_id_at(2, 1)
    db, _ = wire(monkeypatch, position_index=positions([3, 3]),
                 plans=[plan(target, '青铜钥匙'), plan(target, '父亲的旧信')],
                 text='青铜钥匙挂在门边，他推门走了出去。')

    report = await check_chapter_foreshadow_setup(
        db, project_id=project_id(), chapter_id=target)

    assert (report.planned, report.grounded) == (2, 1)
    assert [item.title for item in report.missing] == ['父亲的旧信']
    assert report.missing[0].grounded is False and report.missing[0].evidence is None


@pytest.mark.asyncio
async def test_plan_without_keywords_is_missing(monkeypatch):
    target = chapter_id_at(1, 1)
    # Punctuation-only and single-character descriptions yield no keyword.
    db, _ = wire(monkeypatch, position_index=positions([2]),
                 plans=[plan(target, '——', typ=''), plan(target, '一 二 三', typ='')],
                 text='正文里本来就有很多字，一 二 三 都在里面。')

    report = await check_chapter_foreshadow_setup(
        db, project_id=project_id(), chapter_id=target)

    assert report.planned == 2 and report.grounded == 0
    for item in report.items:
        assert item.keywords == ()
        assert item.grounded is False and item.evidence is None


@pytest.mark.asyncio
async def test_empty_prose_marks_everything_missing(monkeypatch):
    target = chapter_id_at(1, 1)
    db, _ = wire(monkeypatch, position_index=positions([2]),
                 plans=[plan(target, '青铜钥匙'), plan(target, '父亲的旧信')], text='')

    report = await check_chapter_foreshadow_setup(
        db, project_id=project_id(), chapter_id=target)

    assert (report.planned, report.grounded) == (2, 0)
    assert len(report.missing) == 2
    assert all(item.evidence is None for item in report.missing)


@pytest.mark.asyncio
async def test_no_plans_reports_zero(monkeypatch):
    target = chapter_id_at(1, 2)
    db, _ = wire(monkeypatch, position_index=positions([2]), plans=[],
                 text='这一章什么都没安排。')

    report = await check_chapter_foreshadow_setup(
        db, project_id=project_id(), chapter_id=target)

    assert (report.planned, report.grounded, report.missing, report.items) == (0, 0, [], [])
    assert report.chapter_id == target


@pytest.mark.asyncio
async def test_keywords_normalize_fullwidth_space_and_punctuation(monkeypatch):
    target = chapter_id_at(1, 1)
    description = '密码' + '\uff0c' + '\u3000' + '青铜-钥匙' + '\uff01' + '月亮月亮'
    assert keyword_tokens(description, '{"resolve_conditions": "回应条件"}', '伏笔') == (
        '密码', '青铜', '钥匙', '月亮月亮', '回应条件')
    # The schema-default category is not a keyword; an explicit type is.
    assert keyword_tokens('存根', None, '道具') == ('存根', '道具')

    # Full-width prose still matches the folded keywords; indices stay aligned
    # so the evidence comes back verbatim from the original text.
    prose = '桌上有一把青铜制的钥匙' + '\u3002'
    db, _ = wire(monkeypatch, position_index=positions([2]),
                 plans=[plan(target, '钥匙', typ='')], text=prose)
    report = await check_chapter_foreshadow_setup(
        db, project_id=project_id(), chapter_id=target)
    assert report.items[0].grounded is True
    assert report.items[0].evidence == prose


def test_keyword_extraction_bounds():
    # Duplicates collapse in first-seen order; the cap is 8.
    assert keyword_tokens('月亮' + '\uff0c' + '月亮' + '\uff0c' + '青铜' + '\uff0c' + '月亮') == (
        '月亮', '青铜')
    long_description = '\uff0c'.join(f'词{index}' for index in range(12))
    keywords = keyword_tokens(long_description)
    assert len(keywords) == 8
    assert keywords == tuple(f'词{index}' for index in range(8))
    # Whitespace/punctuation never survive; sub-2-length pieces are dropped.
    assert keyword_tokens('  ab  a  、b ') == ('ab',)
    assert keyword_tokens(None, None, None) == ()


@pytest.mark.asyncio
async def test_unknown_chapter_is_unavailable(monkeypatch):
    db, _ = wire(monkeypatch, position_index=positions([2]), plans=[], text=_MISSING)
    with pytest.raises(ForeshadowSetupChapterUnavailable):
        await check_chapter_foreshadow_setup(
            db, project_id=project_id(), chapter_id=chapter_id_at(9, 9))

    with pytest.raises(ForeshadowSetupChapterUnavailable):
        await check_chapter_foreshadow_setup(
            db, project_id=project_id(), chapter_id='not-a-uuid')


def test_item_and_report_shapes_are_immutable_read_only_views():
    report = module.build_report('chapter', [plan('chapter', '存根')], '一枚存根')
    assert isinstance(report.items[0].keywords, tuple)
    with pytest.raises(Exception):
        report.items[0].grounded = False


@pytest.mark.asyncio
async def test_api_returns_documented_shape(monkeypatch):
    from app.api import quality

    target = chapter_id_at(1, 2)

    async def fake_check(db, *, project_id, chapter_id):
        assert chapter_id == target
        return module.build_report(chapter_id, [
            plan(target, '寄信的存根'), plan(target, '父亲的旧信')],
            '寄信的存根压在抽屉里。')

    monkeypatch.setattr(module, 'check_chapter_foreshadow_setup', fake_check)
    payload = await quality.get_chapter_foreshadow_setup(
        project_id=project_id(), chapter_id=target, db=object())

    assert payload['chapter_id'] == target
    assert (payload['planned'], payload['grounded']) == (2, 1)
    assert list(payload) == ['chapter_id', 'planned', 'grounded', 'missing', 'items']
    assert payload['missing'] == [{
        'plan_key': payload['items'][1]['plan_key'],
        'title': '父亲的旧信',
        'keywords': ['父亲', '旧信'],
    }]
    assert set(payload['items'][0]) == {'plan_key', 'title', 'keywords', 'grounded', 'evidence'}
    assert payload['items'][0]['grounded'] is True
    assert '存根' in payload['items'][0]['evidence']


@pytest.mark.asyncio
async def test_api_maps_unavailable_chapter_to_404(monkeypatch):
    from app.api import quality

    async def fake_check(db, *, project_id, chapter_id):
        raise ForeshadowSetupChapterUnavailable('foreshadow_setup_chapter_unavailable')

    monkeypatch.setattr(module, 'check_chapter_foreshadow_setup', fake_check)
    with pytest.raises(HTTPException) as excinfo:
        await quality.get_chapter_foreshadow_setup(
            project_id=project_id(), chapter_id=chapter_id_at(1, 1), db=object())
    assert excinfo.value.status_code == 404


def test_route_is_registered():
    from app.api import quality

    paths = {getattr(route, 'path', '') for route in quality.router.routes}
    assert '/api/quality/projects/{project_id}/chapters/{chapter_id}/foreshadow-setup' in paths