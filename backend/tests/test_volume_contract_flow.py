from tests.support.volume_meta import valid_volume_meta
import json
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from app.services.outline_generator import OutlineGenerator


@pytest.mark.asyncio
async def test_all_volume_obligations_and_prior_plan_reach_later_batches(monkeypatch):
    meta=valid_volume_meta(11)
    meta.update({'volume_idx':1,'chapter_count':11,'title':'渡河',
          'core_conflict':'开局背景'*200+'最后必须救出人质',
          'turning_points':['转折'+str(i) for i in range(8)],
          'volume_start_state':{'location':'北岸'},'volume_end_state':{'resource':'密信烧毁'},
          'volume_information_map':{'守卫':{'unknown':['桥下密道']}},
          'foreshadows':{'planted':[], 'resolved':['第七章兑现铜铃承诺']},
          'foreshadow_progression':['埋下铜铃','发现裂痕','兑现承诺']})
    book={'title':'渡河记','raw_text':'前提'*700+'唯一结局：人质获救',
          'characters':[{'name':'阿青','boundary':'不杀俘虏'}]}
    captured=[]
    async def model(task_type,messages,**kwargs):
        if task_type=='outline_volume_meta': return SimpleNamespace(text=json.dumps(meta))
        prompt=messages[1]['content']; captured.append(prompt)
        start=int(re.search(r'start=(\d+)',prompt).group(1)); end=int(re.search(r'end=(\d+)',prompt).group(1))
        items=[{'chapter_idx':i,'title':f'桥头{i}','summary':'完整动作'*40+f'第{i}章交接状态',
                'key_events':[f'第{i}章独有事件','交出密信'],'handoff_to_next':f'第{i}章未完动作'} for i in range(start,end+1)]
        return SimpleNamespace(text=json.dumps({'batch':items}))
    obj=OutlineGenerator(); obj.router=SimpleNamespace(generate=model)
    obj._load_chapters_per_volume_pref=AsyncMock(return_value=None)
    monkeypatch.setattr('app.services.title_quality_checker.check_and_rewrite_in_place',AsyncMock(return_value={}))
    result=await obj._generate_volume_outline_staged(book,1)
    assert len(result['chapter_summaries'])==11 and len(captured)==3
    for prompt in captured:
        assert all(marker in prompt for marker in ('最后必须救出人质','转折7','桥下密道','密信烧毁','铜铃承诺','唯一结局：人质获救','不杀俘虏'))
    assert '第1章独有事件' in captured[-1]  # outside last-two window
    assert '第10章交接状态' in captured[-1] and '第10章未完动作' in captured[-1]
    assert '不是已发生事实' in captured[-1]
