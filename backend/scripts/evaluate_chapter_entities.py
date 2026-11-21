"""Direct CPA-configured upstream probe; bypasses CPA proxy/routing.
No application DB, graph or config writes.

Uses the production extraction/repair/validation code with a transport adapter;
this tests the prompt contract, not the production Registry binding.
"""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import re
import time

async def main(args):
    from scripts.cpa_eval_config import select_direct_upstream
    selection = select_direct_upstream(args.cpa_config, args.upstream, args.model)
    from app.services.model_router import OpenAIProvider
    from app.services import prompt_registry
    from app.services.chapter_entity_extraction import extract_entity_changes, SYSTEM_PROMPT
    out = Path(args.output)
    if out.exists():
        raise ValueError('Refusing to overwrite evidence')
    source = Path(args.source).read_text(encoding='utf-8-sig')
    marks = list(re.finditer(r'^.*第[一二三四五六七八九十]+章.*$', source, re.M))
    cases = [('chapter4', source[marks[3].start():marks[4].start()]),
             ('chapters2to4', source[marks[1].start():marks[4].start()])]
    provider = OpenAIProvider(selection.api_key, selection.base_url)
    out.mkdir(parents=True)
    records = []
    original = prompt_registry.run_text_prompt
    try:
        for name, body in cases:
            (out / (name + '.source.txt')).write_text(body)
            calls = []
            async def transport(**kwargs):
                index = len(calls) + 1
                calls.append({'attempt': index, 'input_chars': len(kwargs['user_content'])})
                messages = [{'role': 'system', 'content': SYSTEM_PROMPT}, {'role': 'user', 'content': kwargs['user_content']}]
                (out / f'{name}-{index}.input.json').write_text(json.dumps(messages, ensure_ascii=False, indent=2))
                result = await provider.generate(messages, model=selection.model, temperature=.3,
                    max_tokens=kwargs['max_tokens'], task_type='chapter_entities', request_timeout=120, retry_attempts=1)
                (out / f'{name}-{index}.raw.txt').write_text(result.text)
                calls[-1].update(input_tokens=result.usage.input_tokens, output_tokens=result.usage.output_tokens)
                return result
            prompt_registry.run_text_prompt = transport
            record = {'case': name, 'upstream': args.upstream, 'model': args.model,
                      'source_chars': len(body), 'source_sha256': hashlib.sha256(body.encode()).hexdigest(),
                      'max_tokens': 4096, 'temperature': .3, 'calls': calls}
            record.update(selection.public_metadata())
            start = time.monotonic()
            try:
                result = await asyncio.wait_for(extract_entity_changes(project_id=None, chapter_text=body, db=None), 280)
                (out / (name + '.entities.json')).write_text(json.dumps(result, ensure_ascii=False, indent=2))
                record.update(status='schema_valid', counts={key: len(value) for key, value in result.items()})
            except Exception as exc:
                record.update(status='error', error_type=type(exc).__name__)
            record['seconds'] = round(time.monotonic() - start, 3)
            records.append(record)
            (out / 'results.json').write_text(json.dumps(records, ensure_ascii=False, indent=2))
            print(json.dumps(record), flush=True)
    finally:
        prompt_registry.run_text_prompt = original
        await provider.client.close()

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--cpa-config', required=True)
    parser.add_argument('--upstream', default='geminiweb')
    parser.add_argument('--model', default='gemini-3.1-pro')
    asyncio.run(main(parser.parse_args()))
