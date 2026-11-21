import hashlib
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import pytest
from app.services.polish_candidate import PolishCandidate, polish_provenance


def task():
    source = '原稿。\n'
    candidate = PolishCandidate('候选。\n', 'needs_review', hashlib.sha256(source.encode()).hexdigest())
    return NS(result_text=source, polished_text=candidate.text, params_json={'polish_candidate': candidate.metadata()})


def test_both_texts_must_match_and_source_scope_is_explicit():
    value = task()
    assert polish_provenance(value) == dict(status='needs_review', task_source_status='unchanged', candidate_status='unchanged', scope='task_texts_only')
    value.result_text = '原稿。'
    assert polish_provenance(value)['task_source_status'] == 'changed'
    value.polished_text = '另一候选。'
    assert polish_provenance(value)['candidate_status'] == 'changed'


@pytest.mark.parametrize('params', [None, [], {}, {'polish_candidate': []}, {'polish_candidate': {'source_hash':'bad','candidate_hash':42,'status':[]}}])
def test_legacy_or_malformed_metadata_is_unknown(params):
    value = task()
    value.params_json = params
    result = polish_provenance(value)
    assert result['task_source_status'] == result['candidate_status'] == 'unknown'


@pytest.mark.asyncio
async def test_detail_endpoint_exposes_provenance_without_model_call():
    from app.api.generate import get_async_generation
    value = task()
    value.id = 't'; value.project_id = 'p'; value.task_type = 'outline_book'; value.status = 'completed'
    value.char_count = 4; value.progress_text = ''; value.error_message = None; value.created_at = None
    value.cancel_requested = False
    result = await get_async_generation('t', db=NS(get=AsyncMock(return_value=value)))
    assert result['polish_provenance']['candidate_status'] == 'unchanged'
    assert result['result_text'] == value.result_text and result['polished_text'] == value.polished_text
