"""Create a separately reviewable polish candidate from the complete source."""
import asyncio
import hashlib
from dataclasses import dataclass

from app.services.provider_response_guard import require_normal_finish

POLISH_INPUT_MAX_CHARS = 24000
from app.services.polish_prompt import POLISH_SYSTEM


@dataclass
class PolishCandidate:
    text: str
    status: str
    source_hash: str
    reason: str | None = None

    def metadata(self):
        return dict(status=self.status, source_hash=self.source_hash,
                    candidate_hash=hashlib.sha256(self.text.encode()).hexdigest() if self.text else None,
                    reason=self.reason)


async def create_polish_candidate(text, *, instruction='', llm_call, project_id=None, chapter_id=None):
    digest = hashlib.sha256(text.encode()).hexdigest()
    prompt = f'【作者要求】\n{instruction}\n\n【完整原稿】\n{text}'
    if not text.strip():
        return PolishCandidate('', 'skipped', digest, 'empty_source')
    if len(prompt) > POLISH_INPUT_MAX_CHARS:
        return PolishCandidate('', 'skipped', digest, 'source_exceeds_context_budget')
    async with asyncio.timeout(300):
        result = await llm_call(
            task_type='polishing',
            _log_meta={'project_id': project_id, 'chapter_id': chapter_id},
            messages=[{'role': 'system', 'content': POLISH_SYSTEM}, {'role': 'user', 'content': prompt}],
            max_tokens=16384,
        )
    require_normal_finish(getattr(result, 'finish_reason', None))
    candidate = result.text
    if not isinstance(candidate, str) or not candidate.strip():
        raise ValueError('polish_empty_response')
    if candidate == text:
        return PolishCandidate('', 'unchanged', digest)
    return PolishCandidate(candidate, 'needs_review', digest)


def polish_provenance(task):
    """Compare saved hashes with task texts, not with the live chapter/outline."""
    params = task.params_json if isinstance(task.params_json, dict) else {}
    metadata = params.get('polish_candidate')
    metadata = metadata if isinstance(metadata, dict) else {}

    def matches(key, value):
        expected = metadata.get(key)
        if not isinstance(expected, str) or len(expected) != 64 or any(c not in '0123456789abcdef' for c in expected):
            return 'unknown'
        if not isinstance(value, str):
            return 'unknown'
        return 'unchanged' if hashlib.sha256(value.encode()).hexdigest() == expected else 'changed'

    state = metadata.get('status')
    return {
        'status': state if isinstance(state, str) and state in {'needs_review', 'unchanged', 'skipped', 'failed'} else 'unknown',
        'task_source_status': matches('source_hash', task.result_text),
        'candidate_status': matches('candidate_hash', task.polished_text),
        'scope': 'task_texts_only',
    }
