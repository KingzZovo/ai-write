from tests.support.volume_meta import valid_volume_meta
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from app.services.outline_generator import OutlineGenerator


def batch():
    return [{'chapter_idx':i,'title':f'过桥{i}','summary':'人物进入桥头，交信后离开。','key_events':['进入桥头','交出密信']} for i in range(1,3)]


@pytest.mark.parametrize('bad', ['extra','duplicate','reversed','missing_summary','empty_event','non_object','boolean_index'])
def test_batch_contract_rejects_loss_or_renumbering(bad):
    items=batch()
    if bad=='extra': items.append(copy.deepcopy(items[-1]))
    elif bad=='duplicate': items[1]['chapter_idx']=1
    elif bad=='reversed': items.reverse()
    elif bad=='missing_summary': del items[1]['summary']
    elif bad=='empty_event': items[1]['key_events']=['']
    elif bad=='non_object': items[1]='wrong'
    elif bad=='boolean_index': items[0]['chapter_idx']=True
    before=copy.deepcopy(items)
    with pytest.raises(ValueError): OutlineGenerator._validate_volume_batch(items,1,2)
    assert items==before


@pytest.mark.asyncio
async def test_invalid_batch_retries_without_silent_renumbering(monkeypatch):
    obj=OutlineGenerator()
    obj._load_chapters_per_volume_pref=AsyncMock(return_value=None)
    invalid=batch(); invalid[1]['chapter_idx']=1
    calls=AsyncMock(side_effect=[SimpleNamespace(text=json.dumps(valid_volume_meta(2))),
                               SimpleNamespace(text=json.dumps({'batch':invalid})),
                               SimpleNamespace(text=json.dumps({'batch':batch()}))])
    obj.router=SimpleNamespace(generate=calls)
    monkeypatch.setattr('app.services.title_quality_checker.check_and_rewrite_in_place',AsyncMock(return_value={}))
    result=await obj._generate_volume_outline_staged({},1)
    assert result['chapter_summaries']==batch() and calls.await_count==3
    assert '章号错误' in calls.await_args_list[2].kwargs['messages'][1]['content']


@pytest.mark.asyncio
async def test_failed_staged_volume_is_not_streamed_as_saveable_json():
    obj=OutlineGenerator()
    obj._generate_volume_outline_staged=AsyncMock(return_value={'_parse_error':True,'raw_text':'batch failed','chapter_summaries':batch()})
    with pytest.raises(ValueError,match='batch failed'):
        _=[text async for text in obj._generate_volume_outline_staged_text_stream({},1)]


@pytest.mark.asyncio
async def test_exhausted_batch_returns_failure_without_partial_normalization():
    obj=OutlineGenerator()
    obj._load_chapters_per_volume_pref=AsyncMock(return_value=None)
    wrong=batch(); wrong[1]['chapter_idx']=1
    call=AsyncMock(side_effect=[SimpleNamespace(text=json.dumps(valid_volume_meta(2)))]+
                  [SimpleNamespace(text=json.dumps({'batch':wrong})) for _ in range(3)])
    obj.router=SimpleNamespace(generate=call)
    result=await obj._generate_volume_outline_staged({},1)
    assert result['_parse_error'] is True and result['chapter_summaries']==[]
    assert call.await_count==4


@pytest.mark.asyncio
@pytest.mark.parametrize('count,pref', [(0,None),(-2,None),(True,None),(2.7,None),("2.7",None),(10,(3,4))])
async def test_invalid_or_unapproved_count_never_starts_chapter_batches(count,pref):
    obj=OutlineGenerator()
    obj._load_chapters_per_volume_pref=AsyncMock(return_value=pref)
    call=AsyncMock(return_value=SimpleNamespace(text=json.dumps(valid_volume_meta(count))))
    obj.router=SimpleNamespace(generate=call)
    result=await obj._generate_volume_outline_staged({},1)
    assert result['_parse_error'] is True and call.await_count==2
