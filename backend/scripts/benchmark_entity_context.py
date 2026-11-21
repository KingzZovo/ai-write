"""Disposable PG/Neo benchmark of production source-verified state readers.

Synthetic bodies measure engineering cost only; they are not literary samples.
Requires explicitly selected localhost test services. Never uses application DBs.
"""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import random
import resource
import time
from uuid import uuid4

from neo4j import AsyncGraphDatabase
from sqlalchemy import event, insert, text, update, delete, select, func
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from app.models import Base
from app.models.project import Project, Volume, Chapter, Character, CharacterState
from app.services.entity_sources import digest
from app.services.entity_context import verified_character_states, verified_world_snapshot


async def benchmark(args):
    url = os.environ.get('PIPELINE_TEST_DATABASE_URL', '')
    parsed = make_url(url)
    if parsed.host not in ('localhost','127.0.0.1') or parsed.database != 'aiwrite_pipeline_test':
        raise ValueError('requires explicit localhost aiwrite_pipeline_test database')
    uri = os.environ.get('ENTITY_TEST_NEO4J_URI')
    if uri != 'bolt://127.0.0.1:57687':
        raise ValueError('requires explicit disposable localhost Neo4j on port 57687')
    if not 1 <= args.chapters <= 10000 or not 1 <= args.chars <= 20000 or not 1 <= args.characters <= args.chapters:
        raise ValueError('invalid benchmark dimensions')
    schema, pid, vid = 'entity_bench_' + uuid4().hex, uuid4(), uuid4()
    admin = create_async_engine(url)
    engine = create_async_engine(url, connect_args={'server_settings': {'search_path': schema}})
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    driver = AsyncGraphDatabase.driver(uri, auth=('neo4j','aiwrite_test_only'))
    sql_count = 0
    @event.listens_for(engine.sync_engine, 'before_cursor_execute')
    def count(*_):
        nonlocal sql_count
        sql_count += 1
    created = False
    result = {'chapters':args.chapters, 'body_chars':args.chapters*args.chars,
              'characters':args.characters, 'states_per_chapter':args.states_per_chapter, 'scenario':'synthetic_state_history', 'runs':[]}
    try:
        async with admin.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"')); created = True
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await driver.verify_connectivity()
        names = [f'基准人物{i}' for i in range(args.characters)]
        cids = [uuid4() for _ in names]
        async with sessions() as db:
            await db.execute(insert(Project), [dict(id=pid,title='仅性能测试')])
            await db.execute(insert(Volume), [dict(id=vid,project_id=pid,title='测试卷',volume_idx=1)])
            await db.execute(insert(Character), [dict(id=cid,project_id=pid,name=name) for cid,name in zip(cids,names)])
            await db.commit()
        rng = random.Random(20261006)
        alphabet = '天地玄黄宇宙洪荒日月盈昃辰宿列张寒来暑往秋收冬藏山水江湖风雨花木琴剑灯影人心故事长夜星河城池远路舟马刀书雪云火酒茶梦'
        chapter_rows, state_rows, graph_rows = [], [], []
        expected = {name:{} for name in names}
        histories = []
        for idx in range(1,args.chapters+1):
            body = ''.join(rng.choices(alphabet,k=args.chars))
            chid, title = uuid4(), f'第{idx}章'
            receipt = dict(chapter_id=str(chid),project_id=str(pid),volume_id=str(vid),title=title,
                chapter_idx=idx,global_idx=idx,volume_idx=1,status='completed',
                content_hash=hashlib.sha256(body.encode()).hexdigest())
            source_hash=digest(receipt)
            chapter_rows.append(dict(id=chid,volume_id=vid,chapter_idx=idx,global_idx=idx,title=title,
                                     content_text=body,status='completed',word_count=args.chars))
            for offset in range(args.states_per_chapter):
                actor=((idx-1)*args.states_per_chapter+offset)%args.characters
                key=f'字段{((idx-1)//args.characters)%5}'
                status={key:f'值{idx}'}
                expected[names[actor]].update(status)
                histories.append((idx,names[actor],key,f'值{idx}'))
                state_rows.append(dict(id=uuid4(),project_id=pid,character_id=cids[actor],chapter_start=idx,
                    status_json=status,source_chapter_id=chid,source_hash=source_hash,source_mode='chapter_delta'))
                graph_rows.append(dict(cid=str(chid),name=names[actor],idx=idx,hash=source_hash,status=json.dumps(status,ensure_ascii=False)))
            if len(chapter_rows)==200 or idx==args.chapters:
                async with sessions() as db:
                    await db.execute(insert(Chapter),chapter_rows);await db.execute(insert(CharacterState),state_rows);await db.commit()
                async with driver.session() as session:
                    await session.run('UNWIND $rows AS row MERGE (c:Character {project_id:$pid,name:row.name}) '
                        'CREATE (c)-[:HAS_STATE]->(s:CharacterState {project_id:$pid,source_chapter_id:row.cid,'
                        'chapter_start:row.idx,source_hash:row.hash,source_mode:"chapter_delta",status_json:row.status})',
                        pid=str(pid),rows=graph_rows)
                chapter_rows.clear();state_rows.clear();graph_rows.clear()
        async with engine.begin() as conn:
            await conn.execute(text('ANALYZE chapters'));await conn.execute(text('ANALYZE character_states'))
        for iteration in range(args.runs):
            for mode in ('postgres','neo4j'):
                before = sql_count; started=time.perf_counter()
                async with sessions() as db:
                    if mode=='postgres':
                        states=await verified_character_states(db,pid,args.chapters+1)
                        actual={name:state['status'] for name,state in states.items()}
                    else:
                        snapshot=await verified_world_snapshot(db,driver,pid,args.chapters+1)
                        actual={state.name:state.status for state in snapshot.characters}
                assert actual==expected, (mode,'incomplete or incorrect cumulative state')
                result['runs'].append(dict(mode=mode,iteration=iteration+1,seconds=round(time.perf_counter()-started,4),sql_queries=sql_count-before))
        if args.materialize:
            from app.db import session as session_module
            from app.tasks.entity_tasks import _materialize_entities_to_postgres
            original = session_module.async_session_factory
            session_module.async_session_factory = sessions
            async with sessions() as db:
                await db.execute(delete(CharacterState)); await db.commit()
            started = time.perf_counter()
            try:
                stats = await _materialize_entities_to_postgres(project_id=str(pid),chapter_idx=args.chapters,
                    caller='isolated_benchmark',strict=True,graph_driver=driver)
                elapsed = round(time.perf_counter()-started,4)
                async with sessions() as db:
                    count = await db.scalar(select(func.count()).select_from(CharacterState))
                    restored = await verified_character_states(db,pid,args.chapters+1)
                assert count == args.chapters*args.states_per_chapter
                assert {name:state['status'] for name,state in restored.items()} == expected
                result['materialize'] = {'status':'ok','seconds':elapsed,'materialized_rows':count,
                                         'state_writes':stats['cstates_created']}
                repeated=time.perf_counter()
                repeated_stats=await _materialize_entities_to_postgres(project_id=str(pid),chapter_idx=args.chapters,
                    caller='isolated_benchmark_repeat',strict=True,graph_driver=driver)
                result['materialize_repeat']={'seconds':round(time.perf_counter()-repeated,4),
                                              'state_writes':repeated_stats['cstates_created']}
                assert repeated_stats['cstates_created']==0
            except Exception as exc:
                result['materialize'] = {'status':'failed','seconds':round(time.perf_counter()-started,4),
                    'error':type(exc).__name__,'detail':str(exc).split('[SQL:')[0][:500]}
            finally:
                session_module.async_session_factory = original
        async with sessions() as db:
            await db.execute(update(Chapter).where(Chapter.id==chid).values(content_text='作者修改了最新章。'));await db.commit()
            after=await verified_character_states(db,pid,args.chapters+1)
            graph_after=await verified_world_snapshot(db,driver,pid,args.chapters+1)
        expected_after={name:{} for name in names}
        for idx,name,key,value in histories:
            if idx<args.chapters:expected_after[name][key]=value
        expected_after={name:state for name,state in expected_after.items() if state}
        assert {name:state['status'] for name,state in after.items()}==expected_after
        assert {state.name:state.status for state in graph_after.characters}==expected_after
        result['edit_invalidates_without_cleanup']=True
        result['max_rss_mib']=round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024,1)
    finally:
        try:
            async with driver.session() as session:
                await session.run('MATCH (n {project_id:$pid}) DETACH DELETE n',pid=str(pid))
        finally:
            await asyncio.gather(driver.close(),engine.dispose(),return_exceptions=True)
            try:
                if created:
                    async with admin.begin() as conn:await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            finally:
                await admin.dispose()
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--chapters',type=int,default=2000)
    parser.add_argument('--chars',type=int,default=2500)
    parser.add_argument('--characters',type=int,default=20)
    parser.add_argument('--runs',type=int,default=3)
    parser.add_argument('--states-per-chapter',type=int,default=1)
    parser.add_argument('--materialize',action='store_true')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if not 1 <= args.runs <= 10:parser.error('runs must be between 1 and 10')
    if not 1 <= args.states_per_chapter <= args.characters:parser.error('invalid states per chapter')
    result=asyncio.run(benchmark(args))
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(result,ensure_ascii=False))
