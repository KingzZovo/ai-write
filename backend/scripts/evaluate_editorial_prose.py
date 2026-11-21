"""Isolated editorial prose A/B probes, not a production pipeline or model judge.

Only uses local aiwrite_quality_eval. Run serially with other evaluation tools.
Full inputs/outputs are retained for editorial reading, without automatic scoring.
"""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

async def main(args):
    from sqlalchemy.engine import make_url
    url = make_url(args.database_url)
    if url.host not in {'localhost', '127.0.0.1'} or url.database != 'aiwrite_quality_eval':
        raise RuntimeError('Requires isolated local aiwrite_quality_eval database')
    output = Path(args.output)
    if output.exists():
        raise RuntimeError('Refusing to overwrite an existing evaluation directory')
    candidate = Path(args.candidate_prompt).read_text()
    cases = json.loads(Path(args.cases).read_text())
    if (not candidate.strip() or not isinstance(cases, list) or not cases
            or any(not isinstance(c, dict) or not isinstance(c.get('id'), str)
                   or not isinstance(c.get('brief'), str) or not c['brief'].strip() for c in cases)):
        raise ValueError('Each case requires an id and brief')
    if bool(args.cpa_config) != bool(args.cpa_upstream):
        raise ValueError('CPA config and upstream must be specified together')
    from scripts.cpa_eval_config import select_direct_upstream
    selection = (select_direct_upstream(args.cpa_config, args.cpa_upstream, args.model)
                 if args.cpa_config else None)
    baseline_prompt = Path(args.baseline_prompt).read_text() if args.baseline_prompt else None
    plan_prompt = Path(args.candidate_plan_prompt).read_text() if args.candidate_plan_prompt else None
    from dotenv import dotenv_values
    private = Path(args.credentials_dir)
    config = dotenv_values(private / 'server.env')
    os.environ['SECRET_KEY'] = config.get('SECRET_KEY') or ''
    os.environ['DATABASE_URL'] = args.database_url
    from sqlalchemy import update
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    import app.main
    from app.models import Base
    from app.models.project import LLMEndpoint
    from app.models.prompt import PromptAsset
    from app.services import prompt_registry, prompt_cache
    endpoint = next(e for e in json.loads((private / 'endpoint.json').read_text()) if e['tier'] == 'flagship')
    if selection:
        from app.utils.crypto import encrypt_api_key
        endpoint = {**endpoint, 'base_url': selection.base_url,
                    'provider_type': 'openai_compatible',
                    'api_key': encrypt_api_key(selection.api_key)}
    model = selection.model if selection else (args.model or endpoint['default_model'])
    engine = create_async_engine(args.database_url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        await db.execute(update(LLMEndpoint).values(enabled=0))
        await db.execute(update(PromptAsset).values(is_active=0))
        ep = LLMEndpoint(name='editorial-prose-eval-only', provider_type=endpoint['provider_type'], base_url=endpoint['base_url'],
                         api_key=endpoint['api_key'], default_model=model, tier='flagship')
        db.add(ep)
        await db.flush()
        db.add(PromptAsset(task_type='editorial_prose_probe', name='隔离中文正文对照',
                           system_prompt='你是中文小说作者。只输出小说正文。',
                           endpoint_id=ep.id, model_name=model, model_tier='flagship', temperature=.3, max_tokens=3200))
        if plan_prompt:
            db.add(PromptAsset(task_type='editorial_scene_plan_probe', name='隔离场景规划',
                               system_prompt=plan_prompt, endpoint_id=ep.id, model_name=model,
                               model_tier='flagship', temperature=.3, max_tokens=1600))
        await db.commit()
    await engine.dispose()

    from app.services.narrative_contract import contract_block
    output.mkdir(parents=True)
    records = []
    repo = Path(__file__).resolve().parents[2]
    try:
        for index, case in enumerate(cases):
            variants = [('baseline', baseline_prompt or contract_block(include_writer=True)), ('candidate', candidate)]
            if args.variants != 'both':
                variants = [item for item in variants if item[0] == args.variants]
            if index % 2:
                variants.reverse()
            for variant, contract in variants:
                user = case['brief'] + '\n\n' + contract
                stem = f'{index:02d}-{variant}'
                (output / f'{stem}.input.json').write_text(json.dumps(
                    {'case': case['id'], 'system': '你是中文小说作者。只输出小说正文。', 'user': user}, ensure_ascii=False, indent=2))
                start = time.monotonic()
                record = {'case': case['id'], 'variant': variant, 'requested_model': model,
                          'commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repo, text=True).strip(),
                          'user_sha256': hashlib.sha256(user.encode()).hexdigest(),
                          'requested_temperature': .3, 'requested_max_tokens': 3200,
                          'upstream': args.cpa_upstream or 'configured_endpoint',
                          'planning_enabled': bool(plan_prompt and variant == 'candidate')}
                record.update(selection.public_metadata() if selection else {'transport': 'configured_endpoint'})
                try:
                    if plan_prompt and variant == 'candidate':
                        plan_start = time.monotonic()
                        planned = await asyncio.wait_for(prompt_registry.run_text_prompt(
                            'editorial_scene_plan_probe', case['brief'], None), timeout=180)
                        (output / f'{stem}.plan.txt').write_text(planned.text)
                        (output / f'{stem}.plan-input.json').write_text(json.dumps(
                            {'system': plan_prompt, 'user': case['brief']}, ensure_ascii=False, indent=2))
                        record['planning'] = {'elapsed_seconds': round(time.monotonic() - plan_start, 3),
                                              'input_tokens': planned.usage.input_tokens,
                                              'output_tokens': planned.usage.output_tokens,
                                              'model': planned.model}
                        raw = planned.text.strip()
                        if raw.startswith('```'):
                            raw = raw.split('\n', 1)[1].rsplit('```', 1)[0]
                        plan = json.loads(raw)
                        if not isinstance(plan, dict) or any(not plan.get(k) for k in
                                ('fixed_facts', 'character_turn', 'beats', 'ending')):
                            raise ValueError('Scene plan lacks required fields')
                        user += '\n\n【场景规划草案，若与原始设定冲突以原始设定为准；只转化为正文，不复述字段】\n' + json.dumps(plan, ensure_ascii=False)
                        (output / f'{stem}.input.json').write_text(json.dumps(
                            {'case': case['id'], 'system': '你是中文小说作者。只输出小说正文。', 'user': user}, ensure_ascii=False, indent=2))
                        record['user_sha256'] = hashlib.sha256(user.encode()).hexdigest()
                    result = await asyncio.wait_for(prompt_registry.run_text_prompt(
                        'editorial_prose_probe', user, None), timeout=180)
                    (output / f'{stem}.txt').write_text(result.text)
                    record.update(status='returned' if result.text.strip() else 'empty', model=result.model,
                                  input_tokens=result.usage.input_tokens, output_tokens=result.usage.output_tokens,
                                  text_sha256=hashlib.sha256(result.text.encode()).hexdigest(), chars=len(result.text))
                except Exception as exc:
                    # Exception strings can contain provider URLs; never publish them.
                    record.update(status='error', error_type=type(exc).__name__)
                record['elapsed_seconds'] = round(time.monotonic() - start, 3)
                records.append(record)
                (output / 'results.json').write_text(json.dumps(records, ensure_ascii=False, indent=2))
                print(json.dumps(record), flush=True)
    finally:
        await prompt_cache.flush_pending_counts()
        from app.db.session import engine as application_engine
        await application_engine.dispose()

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--credentials-dir', required=True)
    parser.add_argument('--database-url', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--model', default='grok-chat-fast')
    parser.add_argument('--variants', choices=['both','baseline','candidate'], default='both')
    parser.add_argument('--candidate-prompt', required=True)
    parser.add_argument('--cases', required=True)
    parser.add_argument('--baseline-prompt')
    parser.add_argument('--candidate-plan-prompt')
    parser.add_argument('--cpa-config', help='Read-only local CPA config; only isolated evaluation routing changes')
    parser.add_argument('--cpa-upstream', help='Direct-connect to one enabled CPA-configured upstream; bypasses CPA proxy/routing')
    asyncio.run(main(parser.parse_args()))
