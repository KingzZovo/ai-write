"""Replay saved CPA prose through the current gate without making model calls.

Run from backend with PYTHONPATH=. and the repository virtualenv.
"""
import asyncio
import hashlib
import json
from pathlib import Path

from app.services import chapter_quality_gate as gate

ROOT = Path(__file__).resolve().parents[3]


async def main():
    calls = 0

    async def forbidden_model_call(**kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError('Observed form must not request a model rewrite')

    gate.run_text_prompt = forbidden_model_call
    results = []
    for name in ('initial-chapter.txt', 'saved-chapter.txt'):
        source = ROOT / 'docs/evaluation/natural-scenes-20261007' / name
        original = source.read_text()
        result = await gate.apply_chapter_quality_gate(
            text=original, db=None, target_word_count=1800,
        )
        assert result.final_text == original
        assert result.rewrite_rounds == 0
        assert result.status == 'passed'
        results.append({
            'source': str(source.relative_to(ROOT)),
            'chars': len(original),
            'sha256': hashlib.sha256(original.encode()).hexdigest(),
            'unchanged': result.final_text == original,
            'status': result.status,
            'model_calls': calls,
            'metadata': result.to_safe_metadata(),
            'editorial_verdict': 'Known factual/POV problems remain. Not literary acceptance.',
        })
    destination = Path(__file__).with_name('replay.json')
    destination.write_text(json.dumps(results, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps([{'source': r['source'], 'chars': r['chars'], 'unchanged': r['unchanged'], 'model_calls': r['model_calls']} for r in results]))


if __name__ == '__main__':
    asyncio.run(main())
