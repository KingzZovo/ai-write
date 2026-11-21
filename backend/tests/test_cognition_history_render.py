"""Paraphrased later discoveries must not become contradictory current claims."""
import pytest
from sqlalchemy import select
from app.models.project import Chapter
from app.services.character_cognition import load_ledger,serialize_for_prompt
from tests.test_cognition_sources import cog_db,extract
from tests.test_pipeline_control import runtime_db

@pytest.mark.asyncio
async def test_paraphrased_discovery_keeps_chapter_context(cog_db,monkeypatch):
    r=cog_db
    await extract(r,monkeypatch,[{'character':'沈青','still_unknown':'父亲已经收到信。','evidence':'沈青不知道父亲已经收到信。'},
                               {'character':'__reader__','learns':'沈青还不知道父亲已经收信。','evidence':'沈青不知道父亲已经收到信。'}])
    async with r.sessions() as db:
        first=await db.get(Chapter,r.chapter)
        second=Chapter(volume_id=first.volume_id,chapter_idx=2,global_idx=2,title='得知',status='completed',
                       content_text='父亲拿出信封。沈青这才知道信早已送到。')
        db.add(second);await db.commit();cid=second.id
    await extract(r,monkeypatch,[{'character':'沈青','learns':'那封信早已送到父亲手里。','evidence':'沈青这才知道信早已送到。'}],cid)
    async with r.sessions() as db:
        ledger=await load_ledger(db,r.project,before_global_idx=3)
    rendered=serialize_for_prompt(ledger)
    assert '[CH-1] 沈青·当时未知记录：父亲已经收到信。' in rendered
    assert '[CH-2] 沈青·本章认知记录：那封信早已送到父亲手里。' in rendered
    assert '[CH-1] 读者·本章认知记录：沈青还不知道父亲已经收信。' in rendered
    assert '沈青不知道：' not in rendered and '读者已知：' not in rendered


def test_budget_prefers_later_observations_without_cutting_facts():
    facts=[{'chapter_idx':i,'kind':'learns','fact':f'第{i}章得到的完整消息。'} for i in range(1,41)]
    rendered=serialize_for_prompt({'沈青':{'history':facts}},max_chars=250)
    assert len(rendered)<=250 and '[CH-40]' in rendered and '[CH-1]' not in rendered
    assert '第40章得到的完整消息。' in rendered
    assert '认知记录共40条，本次节选' in rendered
    assert '节选不代表全部认知' in rendered


def test_large_fact_is_not_sliced_or_claimed_unknown():
    ledger={'沈青':{'history':[{'chapter_idx':2,'kind':'learns','fact':'长消息'*200},
                              {'chapter_idx':1,'kind':'learns','fact':'完整短消息。'}]}}
    rendered=serialize_for_prompt(ledger,200)
    # The older short entry cannot stand alone while its newer companion
    # is too large. Neither observation nor its source is sliced to fit.
    assert '长消息' not in rendered and '完整短消息。' not in rendered
    assert '本次节选0条' in rendered
    assert serialize_for_prompt(ledger,10)==''


def test_unknown_record_can_preserve_model_qualification_without_double_negation():
    ledger={'沈青':{'history':[{'chapter_idx':1,'kind':'still_unknown',
                              'fact':'她不知道父亲已经收到通知。'}]}}
    rendered=serialize_for_prompt(ledger)
    assert '沈青·当时未知记录：她不知道父亲已经收到通知。' in rendered
    assert '尚未知：她不知道' not in rendered
