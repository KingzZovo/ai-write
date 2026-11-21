"""Replay fictional quality probes through the production chapter judge.

Only writes to the local aiwrite_quality_eval database. Credentials stay outside
artifacts; responses, inputs and usage are retained without claiming human review.
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
    from app.services import chapter_evaluator, prompt_registry, prompt_cache
    from app.services.chapter_acceptance import evaluation_passes
    if args.system_prompt_file:
        chapter_evaluator.EVALUATION_SYSTEM_PROMPT = Path(args.system_prompt_file).read_text()
    endpoint = next(e for e in json.loads((private / 'endpoint.json').read_text()) if e['tier'] == 'flagship')
    model = args.model or endpoint['default_model']
    engine = create_async_engine(args.database_url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    builtin = next(p for p in prompt_registry.BUILTIN_PROMPTS if p['task_type'] == 'chapter_evaluation')
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        await db.execute(update(LLMEndpoint).values(enabled=0))
        await db.execute(update(PromptAsset).values(is_active=0))
        ep = LLMEndpoint(name='judge-eval-only', provider_type=endpoint['provider_type'], base_url=endpoint['base_url'],
                         api_key=endpoint['api_key'], default_model=model, tier='flagship')
        db.add(ep)
        await db.flush()
        db.add(PromptAsset(task_type='chapter_evaluation', name='虚构样本章节验收',
                           system_prompt=builtin['system_prompt'], output_schema=builtin['output_schema'],
                           endpoint_id=ep.id, model_name=model, model_tier='flagship', temperature=.3, max_tokens=2400))
        await db.commit()
    await engine.dispose()
    output.mkdir(parents=True)
    system_text = builtin['system_prompt'] + "\n\n" + chapter_evaluator.EVALUATION_SYSTEM_PROMPT
    (output / 'system_prompt.txt').write_text(system_text)
    prompt_hash = hashlib.sha256(system_text.encode()).hexdigest()
    repo = Path(__file__).resolve().parents[2]
    source = repo / 'docs/evaluation/context-pressure-20261006'
    cases = json.loads((source / 'cases.json').read_text())
    probes = []
    for case in cases:
        probes.append({**case, 'text': (source / f'{case["id"]}-candidate.txt').read_text(),
                       'source': f'context-pressure-20261006/{case["id"]}-candidate.txt'})
    # Positive control isolates the explicit delivery/bridge/amputation constraints;
    # it is not a claim that this short prose is a publishable full chapter.
    probes.append({
        'id': 'mystery_delivery_control', 'facts': cases[0]['facts'], 'task': cases[0]['task'],
        'source': 'handwritten fictional positive control',
        'text': '阿青在断桥前停住。对岸的守卫提起灯，照着缺了半幅桥面的河口。阿青把信夹在右臂与胸口之间，从腰间抽出绳子。空着的左袖被风掀了起来。\n'
                '“接住，别拉。”他用右手把系着石块的绳头抛过去。第一回落进河里。他收回湿绳，退后两步，第二回才越过缺口，砸在守卫脚边。\n'
                '守卫把绳头缠在桥柱上。阿青将油纸信袋的提环套上绳子，抬高手中这一端。信袋滑过断口，卡在绳结处。他抖了两下，袋子终于滑到了对岸。\n'
                '守卫解下信袋，隔河扬了扬。“给谁的？”\n“收信人的名字写在背面。别拆。”\n'
                '守卫翻过信袋，看清名字，把它收进怀里。阿青松开磨破掌心的绳子，退进桥头的暗处。',
    })
    records = []
    original = chapter_evaluator.run_text_prompt
    try:
        for probe in probes:
            captured = {}
            async def capture(*a, **kw):
                result = await original(*a, **kw)
                captured.update(text=result.text, model=result.model,
                                input_tokens=result.usage.input_tokens, output_tokens=result.usage.output_tokens)
                return result
            chapter_evaluator.run_text_prompt = capture
            inputs = {'chapter_text': probe['text'], 'chapter_outline': {'summary': probe['task']},
                      'previous_summary': probe['facts']}
            (output / f'{probe["id"]}.input.json').write_text(json.dumps(inputs, ensure_ascii=False, indent=2))
            start = time.monotonic()
            try:
                result = await asyncio.wait_for(chapter_evaluator.ChapterEvaluator().evaluate(**inputs), timeout=240)
                verdict = result.to_dict()
                accepted = evaluation_passes(result)
            except asyncio.TimeoutError:
                verdict = {'parse_failed': True, 'reason': 'evaluation_timeout'}
                accepted = False
            (output / f'{probe["id"]}.raw.txt').write_text(captured.get('text', ''))
            (output / f'{probe["id"]}.verdict.json').write_text(json.dumps(verdict, ensure_ascii=False, indent=2))
            record = {'case': probe['id'], 'source': probe['source'], 'model': captured.get('model', model),
                      'commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repo, text=True).strip(),
                      'text_sha256': hashlib.sha256(probe['text'].encode()).hexdigest(),
                      'system_prompt_sha256': prompt_hash,
                      'accepted': accepted, 'parse_failed': verdict.get('parse_failed', False),
                      'overall': verdict.get('overall'), 'elapsed_seconds': round(time.monotonic() - start, 3),
                      'input_tokens': captured.get('input_tokens'), 'output_tokens': captured.get('output_tokens'),
                      'requested_temperature': .3, 'requested_max_tokens': 2400}
            records.append(record)
            (output / 'results.json').write_text(json.dumps(records, ensure_ascii=False, indent=2))
            print(json.dumps(record), flush=True)
    finally:
        chapter_evaluator.run_text_prompt = original
        await prompt_cache.flush_pending_counts()
        from app.db.session import engine as application_engine
        await application_engine.dispose()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--credentials-dir', required=True)
    parser.add_argument('--database-url', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--model')
    parser.add_argument('--system-prompt-file', help='Experimental evaluator system prompt; does not change production defaults')
    asyncio.run(main(parser.parse_args()))
