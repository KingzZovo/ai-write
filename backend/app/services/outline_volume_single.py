"""Validated delivery for the explicit single-call volume-outline mode."""
import asyncio
from contextlib import aclosing

from app.services.outline_contract import validate_volume_meta


async def generate_single_volume(*, router, messages, parse_json, validate_batch,
                                 volume_idx, chapter_range, timeout, stream=False):
    messages = list(messages)
    for attempt in range(2):
        raw = None
        try:
            async with asyncio.timeout(timeout):
                if stream:
                    chunks = []
                    async with aclosing(router.generate_stream(task_type='outline_volume', messages=messages)) as response:
                        async for chunk in response:
                            chunks.append(chunk)
                    raw = ''.join(chunks)
                else:
                    response = await router.generate(task_type='outline_volume', messages=messages)
                    raw = response.text
            result = parse_json(raw)
            validate_volume_meta(result, volume_idx, chapter_range)
            validate_batch(result.get('chapter_summaries'), 1, result['chapter_count'])
            return result
        except ValueError as exc:
            if raw is None or attempt == 1:
                return {'_parse_error': True, 'raw_text': str(exc)}
            messages = messages + [
                {'role': 'assistant', 'content': raw},
                {'role': 'user', 'content': f'卷纲校验失败：{exc}。保留有效剧情和项目限制，返回完整修复后的卷纲 JSON，章节数量与章号必须准确。'},
            ]
        except Exception as exc:
            return {'_parse_error': True, 'raw_text': str(exc)}
