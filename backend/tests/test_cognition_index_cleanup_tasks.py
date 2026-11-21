"""Task dispatch only: no broker, runtime database, or private harness access."""
from unittest.mock import Mock
from uuid import uuid4

import pytest

from app.tasks import entity_tasks
from app.services.entity_sources import digest
from app.services.qdrant_store import cognition_collection


def fake_async_result(monkeypatch, result):
    def run(coroutine):
        coroutine.close()
        return dict(result)
    monkeypatch.setattr('app.tasks._run_async_safe', run)


def test_periodic_discovery_dispatches_each_book_and_reports_failed_delivery(monkeypatch):
    pids = [str(uuid4()) for _ in range(3)]
    groups = {pid: [cognition_collection(pid, digest('encoder'), 2)] for pid in pids}
    fake_async_result(monkeypatch, {'examined': 3, 'projects': groups})
    dispatch = Mock(side_effect=[object(), RuntimeError('broker unavailable'), object()])
    monkeypatch.setattr(entity_tasks.cleanup_project_cognition_indexes, 'apply_async', dispatch)
    result = entity_tasks.cleanup_cognition_indexes.run()
    assert result['dispatched'] == 2 and result['dispatch_failures'] == 1
    assert [call.kwargs['args'][0] for call in dispatch.call_args_list] == pids
    assert [call.kwargs['args'][2] for call in dispatch.call_args_list] == list(groups.values())
    assert all(call.kwargs['expires'] == 3600 for call in dispatch.call_args_list)


@pytest.mark.parametrize('status', ['partial', 'complete', 'busy', 'inactive'])
def test_only_partial_batch_dispatches_its_cursor_after_resources_close(monkeypatch, status):
    pid = str(uuid4())
    cursor = {'collection': cognition_collection(pid, digest('encoder'), 2), 'offset': str(uuid4())}
    collections = [cursor['collection']]
    result = {'status': status, 'cursor': cursor if status in {'partial', 'busy'} else None}
    completed = False
    def run(coroutine):
        nonlocal completed
        coroutine.close()
        completed = True
        return result
    monkeypatch.setattr('app.tasks._run_async_safe', run)
    def dispatch(**kwargs):
        assert completed
        assert kwargs == {'args': [pid, cursor, collections], 'expires': 3600}
    send = Mock(side_effect=dispatch)
    monkeypatch.setattr(entity_tasks.cleanup_project_cognition_indexes, 'apply_async', send)
    assert entity_tasks.cleanup_project_cognition_indexes.run(pid, collection_names=collections) == result
    assert send.call_count == int(status == 'partial')


def test_continuation_delivery_failure_is_not_reported_as_completed(monkeypatch):
    fake_async_result(monkeypatch, {'status': 'partial', 'cursor': {'pending': True}})
    monkeypatch.setattr(entity_tasks.cleanup_project_cognition_indexes, 'apply_async',
        Mock(side_effect=RuntimeError('broker unavailable')))
    with pytest.raises(RuntimeError):
        entity_tasks.cleanup_project_cognition_indexes.run(str(uuid4()))
