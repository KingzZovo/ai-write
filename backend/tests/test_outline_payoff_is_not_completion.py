from types import SimpleNamespace as NS

import pytest
from sqlalchemy import select, update
from app.models.project import Foreshadow, Outline
from app.services.foreshadow_plans import read_foreshadow_plans
from tests.test_foreshadow_chronology import book, add_clue, volume_outline, set_chapter_plan


@pytest.mark.asyncio
@pytest.mark.parametrize('level', ['chapter', 'volume'])
@pytest.mark.parametrize('with_new_plan', [False, True])
async def test_outline_payoff_plan_never_changes_existing_prose_records(book, level, with_new_plan):
    r = book
    fid = add_clue(r, r.chapters[0], '旧问题', legacy=True)
    planted = ['信封角上的蓝色印记'] if with_new_plan else []
    if level == 'chapter':
        set_chapter_plan(r, r.chapters[1], planted)
    else:
        oid = volume_outline(r, planted)
        r.session.execute(update(Outline).where(Outline.id == oid).values(content_json={
            'volume_id': str(r.volumes[1]), 'volume_idx': 2,
            'foreshadows': {'planted': planted, 'resolved': ['旧问题']},
        }))
        r.session.commit()
    result = await read_foreshadow_plans(r.db, r.pid)
    assert len(result['plans']) == int(with_new_plan)
    assert r.session.execute(select(Foreshadow.id, Foreshadow.status)).all() == [(fid, 'planted')]
    assert all(str(query).lstrip().upper().startswith('SELECT') for query in r.queries)


def test_planned_foreshadows_are_not_counted_as_unresolved_prose_debt():
    from app.services.foreshadow_manager import compute_debt_score
    rows = [NS(status='planned', planted_chapter=1, description=f'计划{i}', type='伏笔') for i in range(100)]
    report = compute_debt_score(rows, 80)
    assert report['unresolved'] == 0 and report['score'] == 100
    assert not report['criticals'] and not report['warnings']


def test_planned_prompt_does_not_claim_actual_planting_or_maturity():
    from app.services.context_pack import CFPGTriplet
    from app.services.foreshadow_lifecycle import format_active_foreshadows_for_prompt
    triplet = CFPGTriplet(cause='计划', foreshadow='蓝色印记', payoff_goal='认出送信者', proximity=1, planned=True)
    text = triplet.to_prompt()
    assert '尚待正文落实' in text and '接近消解' not in text
    block = format_active_foreshadows_for_prompt([{
        'description': '蓝色印记', 'planted_chapter': None, 'status': 'planned', 'source_label': '卷级计划（未指定章节）', 'age': 77,
    }])
    assert '未指定章节' in block and '计划·尚待正文落实' in block
    assert '已埋 77' not in block and '以下伏笔已被前文埋下' not in block
