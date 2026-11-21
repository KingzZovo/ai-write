"""Reproducible real-model comparison of context assembly (never production DB).

Reads endpoint credentials from a private directory; writes fictional prompts,
responses and actual token/latency records. Does not assign quality scores.
"""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import types

CASES = [
    {'id':'mystery_bridge', 'genre':'悬疑', 'facts':'阿青左手已截肢，不能双手持物。守卫不知道密信内容。桥已断裂，不能徒步过桥。',
     'task':'阿青把密信交给守卫。展示断桥形成的阻力和解决办法；守卫不能提前说出信中秘密。',
     'checks':['肢体状态不矛盾','解决断桥阻力后完成交信','守卫知识有来源']},
    {'id':'fantasy_cost', 'genre':'奇幻', 'facts':'渡河术只能使用一次，施术必须烧毁唯一的铜铃。林叶不知道河底有人。铜铃烧毁后不能再次出现。',
     'task':'林叶用渡河术救出同伴。写清施术、付出代价与抵岸后状态；河底的人保持未被发现。',
     'checks':['渡河术仅一次','铜铃代价落到行动','未提前知道河底秘密']},
    {'id':'scifi_airlock', 'genre':'科幻', 'facts':'气闸只能容纳一人。氧气只够四分钟。站长已死亡，不能发实时指令。维修员不知道内舱密码。',
     'task':'维修员将伤员送入内舱。展示时间压力、气闸限制、获取权限的因果过程。',
     'checks':['气闸不同时通过两人','权限获取有依据','死者不发实时指令']},
]

async def main(args):
    from dotenv import dotenv_values
    private=Path(args.credentials_dir)
    config=dotenv_values(private/'server.env')
    os.environ['SECRET_KEY']=config.get('SECRET_KEY') or ''
    os.environ['DATABASE_URL']=args.database_url
    from sqlalchemy import update
    from sqlalchemy.engine import make_url
    url=make_url(args.database_url)
    if url.host not in {'localhost','127.0.0.1'} or url.database!='aiwrite_quality_eval':
        raise RuntimeError('Requires isolated local aiwrite_quality_eval database')
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    import app.main
    from app.models import Base
    from app.models.project import LLMEndpoint
    from app.models.prompt import PromptAsset
    from app.services import prompt_registry
    from app.services.context_pack import ContextPack
    endpoint=next(e for e in json.loads((private/'endpoint.json').read_text()) if e['tier']==args.tier)
    if args.model:
        endpoint['default_model']=args.model
    output=Path(args.output)
    if (output/'results.json').exists():
        raise RuntimeError('Refusing to overwrite an existing evaluation run')
    from app.utils.crypto import decrypt_api_key
    key=decrypt_api_key(endpoint['api_key'])
    if not key: raise RuntimeError('Endpoint credential could not be loaded')
    engine=create_async_engine(args.database_url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine,expire_on_commit=False)() as db:
        await db.execute(update(LLMEndpoint).values(enabled=0))
        await db.execute(update(PromptAsset).values(is_active=0))
        ep=LLMEndpoint(name='quality-eval-only',provider_type=endpoint['provider_type'],base_url=endpoint['base_url'],
                       api_key=endpoint['api_key'],default_model=endpoint['default_model'],tier='standard')
        db.add(ep); await db.flush()
        asset=PromptAsset(task_type='quality_context_eval',name='固定上下文对照',system_prompt='你是小说作者。遵守给定事实，写具体行动、因果过程和人物反应。只输出正文。',
                          endpoint_id=ep.id,model_name=ep.default_model,temperature=0,max_tokens=2400)
        db.add(asset); await db.commit()
    await engine.dispose()
    repo=Path(__file__).resolve().parents[2]
    baseline_source=subprocess.check_output(['git','show',f'{args.baseline}:backend/app/services/context_pack.py'],cwd=repo,text=True)
    baseline_module=types.ModuleType('_quality_baseline_context'); sys.modules[baseline_module.__name__]=baseline_module
    exec(compile(baseline_source,'historical_context_pack.py','exec'),baseline_module.__dict__)
    output=Path(args.output); output.mkdir(parents=True,exist_ok=True)
    (output/'cases.json').write_text(json.dumps(CASES,ensure_ascii=False,indent=2))
    records=[]
    try:
        for case in CASES:
            data={'current_outline':{'summary':case['task'],'key_events':['遇到阻力','付出代价完成交接']},
                  'authoritative_fact_contract':case['facts'], 'world_rules':[case['facts']],
                  'future_outlines':['远期计划参考。'*100], 'style_tic_mirror':'【文风参考】\n'+('动作具体，节奏舒缓。'*100)}
            for variant,cls in [('baseline',baseline_module.ContextPack),('candidate',ContextPack)]:
                prompt=cls(**data).to_system_prompt(token_budget=1400)
                user=f'题材：{case["genre"]}。根据上下文写下一场约500字，完成本章任务，不添加设定说明。'
                stem=f'{case["id"]}-{variant}'
                (output/f'{stem}.prompt.json').write_text(json.dumps({'system_context':prompt,'user':user},ensure_ascii=False,indent=2))
                start=time.monotonic()
                result=await asyncio.wait_for(prompt_registry.run_text_prompt('quality_context_eval',user,None,
                           messages=[{'role':'system','content':prompt},{'role':'user','content':user}]),timeout=240)
                (output/f'{stem}.txt').write_text(result.text)
                record={'case':case['id'],'variant':variant,'model':result.model,'baseline_commit':args.baseline,
                        'candidate_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=repo,text=True).strip(),
                        'prompt_sha256':hashlib.sha256((prompt+user).encode()).hexdigest(),
                        'temperature':0,'requested_max_tokens':2400,
                        'input_tokens':result.usage.input_tokens,'output_tokens':result.usage.output_tokens,
                        'elapsed_seconds':round(time.monotonic()-start,3),'output_chars':len(result.text)}
                records.append(record)
                (output/'results.json').write_text(json.dumps(records,ensure_ascii=False,indent=2))
                print(json.dumps(record,ensure_ascii=False),flush=True)
    finally:
        from app.services import prompt_cache
        await prompt_cache.flush_pending_counts()
        from app.db.session import engine as application_engine
        await application_engine.dispose()

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--credentials-dir',required=True)
    parser.add_argument('--database-url',required=True)
    parser.add_argument('--baseline',default='14bdc40')
    parser.add_argument('--tier',choices=['standard','flagship'],default='standard')
    parser.add_argument('--model')
    parser.add_argument('--output',required=True)
    asyncio.run(main(parser.parse_args()))
