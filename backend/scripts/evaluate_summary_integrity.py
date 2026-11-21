"""Direct CPA-configured upstream summary A/B; bypasses CPA proxy/routing.

No application DB/config writes or model judge."""
import argparse
import ast
import asyncio
import hashlib
import json
from pathlib import Path
import re
import subprocess
import time


async def main(args):
    from scripts.cpa_eval_config import select_direct_upstream
    selection = select_direct_upstream(args.cpa_config, args.upstream, args.model)
    from app.services.model_router import OpenAIProvider
    from app.services.prompt_registry import BUILTIN_PROMPTS
    from app.services.chapter_summarizer import _USER_TEMPLATE, _clean_summary_output
    out = Path(args.output)
    if out.exists():
        raise ValueError('Refusing to overwrite evidence')
    candidate_template = Path(args.candidate_prompt).read_text() if args.candidate_prompt else _USER_TEMPLATE
    source = Path(args.source).read_text(encoding='utf-8-sig')
    marks = list(re.finditer(r'^.*第[一二三四五六七八九十]+章.*$', source, re.M))
    cases = [('chapter4', source[marks[3].start():marks[4].start()]),
             ('chapters2to4', source[marks[1].start():marks[4].start()])]
    provider = OpenAIProvider(selection.api_key, selection.base_url)
    system = next(p['system_prompt'] for p in BUILTIN_PROMPTS if p['task_type'] == 'summary')
    root = Path(__file__).resolve().parents[2]
    baseline_code = subprocess.check_output(['git', 'show', args.baseline_ref + ':backend/app/services/chapter_summarizer.py'], cwd=root, text=True)
    tree = ast.parse(baseline_code)
    old_function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_clean_summary_output')
    old_template = ast.literal_eval(next(n.value for n in tree.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == '_USER_TEMPLATE' for t in n.targets)))
    namespace = {}
    exec(compile(ast.Module(body=[old_function], type_ignores=[]), '<baseline-cleaner>', 'exec'), namespace)
    out.mkdir(parents=True)
    records = []
    try:
        for index, (name, body) in enumerate(cases):
            (out / (name + '.source.txt')).write_text(body)
            variants = ['baseline', 'candidate'] if index == 0 else ['candidate', 'baseline']
            if args.candidate_only:
                variants = ['candidate']
            for variant in variants:
                baseline_body = body if len(body) <= 6000 else body[:4200] + '\n\n…(中部省略)…\n\n' + body[-1800:]
                user = old_template.format(title=name, content=baseline_body) if variant == 'baseline' else candidate_template.format(title=name, content=body)
                stem = name + '-' + variant
                (out / (stem + '.input.json')).write_text(json.dumps({'system': system, 'user': user}, ensure_ascii=False, indent=2))
                record = dict(case=name, variant=variant, model=args.model, upstream=args.upstream,
                              source_chars=len(body), input_chars=len(user), source_sha256=hashlib.sha256(body.encode()).hexdigest(),
                              max_tokens=2048, temperature=0.3, baseline_ref=args.baseline_ref)
                record.update(selection.public_metadata())
                start = time.monotonic()
                try:
                    result = await asyncio.wait_for(provider.generate(
                        [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}],
                        model=selection.model, temperature=0.3, max_tokens=2048, task_type='summary',
                        request_timeout=120, retry_attempts=1,
                    ), timeout=180)
                    (out / (stem + '.raw.txt')).write_text(result.text)
                    cleaned = namespace['_clean_summary_output'](result.text) if variant == 'baseline' else _clean_summary_output(result.text)
                    if variant == 'baseline' and len(cleaned) > 220:
                        cleaned = cleaned[:220].rstrip() + '…'
                    (out / (stem + '.summary.txt')).write_text(cleaned)
                    record.update(status='returned' if cleaned else 'invalid', summary_chars=len(cleaned),
                                  input_tokens=result.usage.input_tokens, output_tokens=result.usage.output_tokens)
                except Exception as exc:
                    record.update(status='error', error_type=type(exc).__name__)
                record['seconds'] = round(time.monotonic() - start, 3)
                records.append(record)
                (out / 'results.json').write_text(json.dumps(records, ensure_ascii=False, indent=2))
                print(json.dumps(record), flush=True)
    finally:
        await provider.client.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True)
    parser.add_argument('--candidate-prompt')
    parser.add_argument('--output', required=True)
    parser.add_argument('--cpa-config', required=True)
    parser.add_argument('--upstream', default='geminiweb')
    parser.add_argument('--model', default='gemini-3.1-pro')
    parser.add_argument('--baseline-ref', default='8549076')
    parser.add_argument('--candidate-only', action='store_true')
    asyncio.run(main(parser.parse_args()))
