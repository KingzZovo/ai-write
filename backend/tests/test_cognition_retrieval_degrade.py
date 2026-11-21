"""回归:嵌入服务不可用时,认知语义召回降级而不是阻断整章。

实测(2026-11-14 / 11-16,两次):上游 NVIDIA embeddings 持续 HTTP 500 时,
中段章(第 18 章起)直接 `character_cognition_retrieval_unavailable`,
整条写作链停在那里 —— 而语义召回本该只是"可选增强"。

默认行为不变(嵌入不可用即阻断)。打开 `ALLOW_COGNITION_DEGRADED_CONTINUATION`
后,只有"嵌入服务不可用"(transport / HTTP / timeout)才降级:不抛错,改为
返回不含语义排序、但仍带完整台账渲染文本的选择,并写 warning 日志。
真实损坏照旧抛错:快照变了(`decision_changed`)、空间中途变了
(`embedding_changed`)、索引回执/配置这类数据不一致(ValueError)、以及
索引后端(向量库)故障。

全部用例只用假 client / 假 space:不联网,不启动 Qdrant 或 PostgreSQL。
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from qdrant_client.models import Distance, VectorParams

from app.config import settings
from app.services import cognition_retrieval as retrieval
from app.services.cognition_context import render_history
from app.services.cognition_validation import CognitionLedger
from app.services.context_errors import RequiredContextUnavailable
from app.services.entity_sources import digest

LOGGER_NAME = 'app.services.cognition_retrieval'
# 上游原文里可能有端点/凭据细节,任何日志/异常里都不许出现。
UPSTREAM_TEXT = 'NVIDIA embeddings HTTP 500: upstream exploded'


def event(index, fact):
    return dict(chapter_idx=index, kind='learns', fact=fact, evidence=fact,
                source_hash=digest([index, fact]), source_chapter_id=str(uuid4()), source_mode='auto')


def make_ledger(*, count=40, salt=''):
    """长到必须走语义召回的历史台账(默认 40 条,单批装不下)。"""
    ledger = CognitionLedger({f'人物{i}': {'history': [event(i + 1, f'第{i}条消息')]} for i in range(count)})
    ledger.snapshot_hash = digest([salt, dict(ledger)])
    return ledger


def make_prompt(ledger, *, context='村里的水路后来怎样了?', max_chars=600):
    return retrieval.CognitionPrompt.create(uuid4(), ledger, context, (), max_chars,
        before_global_idx=20, before_chapter_id=None)


class OfflineSpace:
    """确定性的 2 维假编码器,不发任何网络请求。"""

    identity = digest('offline-degrade-space-v1')

    def __init__(self, *, fail_on=('query', 'passage'), error=None):
        self.fail_on = tuple(fail_on)
        self.error = error if error is not None else RuntimeError(UPSTREAM_TEXT)
        self.calls = []

    async def embed(self, texts, *, input_type):
        self.calls.append(input_type)
        if input_type in self.fail_on:
            raise self.error
        return [[1.0, 0.0] for _ in texts]


class FakeClient:
    """只实现 rank_cognition 真正会用的向量库表面;内存字典,不联网。"""

    def __init__(self):
        self.collections = {}
        self.calls = []

    async def collection_exists(self, name):
        self.calls.append('collection_exists')
        return name in self.collections

    async def create_collection(self, collection_name, vectors_config):
        self.calls.append('create_collection')
        self.collections[collection_name] = vectors_config

    async def get_collection(self, name):
        self.calls.append('get_collection')
        return SimpleNamespace(config=SimpleNamespace(params=SimpleNamespace(vectors=self.collections[name])))

    async def retrieve(self, *, collection_name, ids, with_payload=True, with_vectors=False):
        self.calls.append('retrieve')
        return []

    async def query_points_groups(self, **kwargs):
        self.calls.append('query_points_groups')
        raise AssertionError('批次向量都没写完,不该查询')

    async def close(self):
        raise AssertionError('别人借来的 client 不能被关闭')


class DeadIndexClient(FakeClient):
    """向量库本身不可用:不是"嵌入服务不可用",不能降级。"""

    async def collection_exists(self, name):
        raise RuntimeError('qdrant unreachable')


@asynccontextmanager
async def index_lock(project_id, *, wait=False):
    yield SimpleNamespace(check=AsyncMock())


def sessions_for(ledger, monkeypatch):
    @asynccontextmanager
    async def sessions():
        yield SimpleNamespace()

    monkeypatch.setattr('app.services.character_cognition.load_ledger', AsyncMock(return_value=ledger))
    return sessions


async def drive(entry, prompt, *, space, monkeypatch, current=None, spaces=None, client=None):
    """走真实入口(默认 resolve_prompt_selection),只把外部世界换成假件。"""
    loader = (AsyncMock(side_effect=spaces) if spaces is not None
              else AsyncMock(return_value=space))
    return await entry(prompt, client=client if client is not None else FakeClient(), space_loader=loader,
        sessions=sessions_for(current if current is not None else prompt.ledger, monkeypatch),
        lock_factory=index_lock)


@pytest.mark.asyncio
async def test_embedding_outage_still_blocks_by_default(monkeypatch):
    """开关关闭时逐字不变:嵌入不可用 → retrieval_unavailable。"""
    monkeypatch.setattr(settings, 'ALLOW_COGNITION_DEGRADED_CONTINUATION', False, raising=False)
    prompt = make_prompt(make_ledger())
    with pytest.raises(RequiredContextUnavailable, match='retrieval_unavailable') as error:
        await drive(retrieval.resolve_prompt_selection, prompt, space=OfflineSpace(), monkeypatch=monkeypatch)
    assert UPSTREAM_TEXT not in str(error.value)


@pytest.mark.asyncio
async def test_query_embedding_outage_degrades_when_enabled(monkeypatch, caplog):
    """纯"嵌入服务不可用":query 就失败,连向量库都没碰到。"""
    monkeypatch.setattr(settings, 'ALLOW_COGNITION_DEGRADED_CONTINUATION', True, raising=False)
    prompt, space, client = make_prompt(make_ledger()), OfflineSpace(fail_on=('query',)), FakeClient()
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        selection = await drive(retrieval.resolve_prompt_selection, prompt, space=space,
            monkeypatch=monkeypatch, client=client)
    assert space.calls == ['query'] and client.calls == []
    assert selection.ranked_keys == () and selection.space_id == space.identity
    assert selection.text and '逐章认知记录' in selection.text
    assert 'degraded' in caplog.text and '降级' in caplog.text and 'RuntimeError' in caplog.text
    assert UPSTREAM_TEXT not in caplog.text


@pytest.mark.asyncio
async def test_passage_outage_degrades_to_unranked_ledger(monkeypatch, caplog):
    """实测故障点:query 成功、passage 批量失败(NVIDIA 5xx / 超时)。"""
    monkeypatch.setattr(settings, 'ALLOW_COGNITION_DEGRADED_CONTINUATION', True, raising=False)
    ledger = make_ledger()
    prompt, space = make_prompt(ledger), OfflineSpace(fail_on=('passage',))
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        selection = await drive(retrieval.resolve_prompt_selection, prompt, space=space, monkeypatch=monkeypatch)
    assert space.calls[0] == 'query' and 'passage' in space.calls
    assert selection.ranked_keys == ()
    # 降级结果就是"无语义排序"的台账渲染:逐字可复现。
    assert selection.text == render_history(prompt.ledger, prompt.max_chars,
        priority_names=(), context_text=prompt.context)
    assert 'degraded' in caplog.text and '降级' in caplog.text
    assert UPSTREAM_TEXT not in caplog.text


@pytest.mark.asyncio
async def test_writing_entry_returns_ledger_text_when_degraded(monkeypatch):
    """写作入口(resolve_prompt_context)拿到的必须是可用的台账字符串。"""
    monkeypatch.setattr(settings, 'ALLOW_COGNITION_DEGRADED_CONTINUATION', True, raising=False)
    prompt = make_prompt(make_ledger())
    text = await drive(retrieval.resolve_prompt_context, prompt, space=OfflineSpace(fail_on=('passage',)),
        monkeypatch=monkeypatch)
    assert isinstance(text, str) and text and '逐章认知记录' in text


@pytest.mark.asyncio
async def test_decision_change_is_never_degraded(monkeypatch):
    """快照变了(作者改稿/章节重排)是真实损坏:即使嵌入不可用也不降级。"""
    monkeypatch.setattr(settings, 'ALLOW_COGNITION_DEGRADED_CONTINUATION', True, raising=False)
    prompt = make_prompt(make_ledger())
    with pytest.raises(RequiredContextUnavailable, match='decision_changed'):
        await drive(retrieval.resolve_prompt_selection, prompt, space=OfflineSpace(), monkeypatch=monkeypatch,
            current=make_ledger(salt='作者改稿'))


@pytest.mark.asyncio
async def test_decision_change_blocks_with_flag_off_too(monkeypatch):
    monkeypatch.setattr(settings, 'ALLOW_COGNITION_DEGRADED_CONTINUATION', False, raising=False)
    prompt = make_prompt(make_ledger())
    with pytest.raises(RequiredContextUnavailable, match='retrieval_unavailable'):
        await drive(retrieval.resolve_prompt_selection, prompt, space=OfflineSpace(), monkeypatch=monkeypatch,
            current=make_ledger(salt='作者改稿'))


@pytest.mark.asyncio
async def test_embedding_space_change_is_never_degraded(monkeypatch):
    """召回途中嵌入空间指纹变了:真实损坏,不降级。"""
    monkeypatch.setattr(settings, 'ALLOW_COGNITION_DEGRADED_CONTINUATION', True, raising=False)
    prompt = make_prompt(make_ledger())
    first, changed = OfflineSpace(fail_on=('query',)), OfflineSpace(fail_on=())
    changed.identity = digest('rotated-embedding-space')
    with pytest.raises(RequiredContextUnavailable, match='embedding_changed'):
        await drive(retrieval.resolve_prompt_selection, prompt, space=first, monkeypatch=monkeypatch,
            spaces=[first, changed])


@pytest.mark.asyncio
async def test_embedding_space_change_blocks_with_flag_off_too(monkeypatch):
    monkeypatch.setattr(settings, 'ALLOW_COGNITION_DEGRADED_CONTINUATION', False, raising=False)
    prompt = make_prompt(make_ledger())
    first, changed = OfflineSpace(fail_on=('query',)), OfflineSpace(fail_on=())
    changed.identity = digest('rotated-embedding-space')
    with pytest.raises(RequiredContextUnavailable, match='retrieval_unavailable'):
        await drive(retrieval.resolve_prompt_selection, prompt, space=first, monkeypatch=monkeypatch,
            spaces=[first, changed])


@pytest.mark.asyncio
async def test_configuration_error_is_not_degraded_even_when_enabled(monkeypatch):
    """配置漂移/回执不符这类 ValueError 是数据不一致,不是"服务不可用"。"""
    monkeypatch.setattr(settings, 'ALLOW_COGNITION_DEGRADED_CONTINUATION', True, raising=False)
    prompt = make_prompt(make_ledger())
    space = OfflineSpace(fail_on=('query',), error=ValueError('embedding_configuration_changed'))
    with pytest.raises(RequiredContextUnavailable, match='retrieval_unavailable'):
        await drive(retrieval.resolve_prompt_selection, prompt, space=space, monkeypatch=monkeypatch)


@pytest.mark.asyncio
async def test_vector_index_failure_is_not_degraded(monkeypatch):
    """向量库坏了是真实故障;只有"嵌入服务不可用"才降级。"""
    monkeypatch.setattr(settings, 'ALLOW_COGNITION_DEGRADED_CONTINUATION', True, raising=False)
    prompt = make_prompt(make_ledger())
    with pytest.raises(RequiredContextUnavailable, match='retrieval_unavailable'):
        await drive(retrieval.resolve_prompt_selection, prompt, space=OfflineSpace(fail_on=()),
            monkeypatch=monkeypatch, client=DeadIndexClient())


def test_task_group_outage_group_is_recognised_as_provider_failure():
    """同一个 TaskGroup 里并发批次全失败时是真 ExceptionGroup,不能漏判。"""
    all_outage = ExceptionGroup('all failed', [retrieval.EmbeddingUnavailable('RuntimeError'),
        ExceptionGroup('nested', [retrieval.EmbeddingUnavailable('ReadTimeout')])])
    assert retrieval._embedding_unavailable(all_outage)
    assert retrieval._embedding_unavailable(retrieval.EmbeddingUnavailable('RuntimeError'))
    # 混进任何数据不一致/未知失败,就不再算"服务不可用"。
    assert not retrieval._embedding_unavailable(ExceptionGroup('mixed', [
        retrieval.EmbeddingUnavailable('RuntimeError'), ValueError('embedding_response_count_invalid')]))
    assert not retrieval._embedding_unavailable(RuntimeError(UPSTREAM_TEXT))

@pytest.mark.asyncio
async def test_rank_cognition_keeps_the_provider_error_when_flag_is_off(monkeypatch):
    """开关关闭时连分类都不做:直接调用 rank_cognition 的旧调用方逐字不变。"""
    monkeypatch.setattr(settings, 'ALLOW_COGNITION_DEGRADED_CONTINUATION', False, raising=False)
    prompt = make_prompt(make_ledger())
    with pytest.raises(RuntimeError) as error:
        await retrieval.rank_cognition(prompt, client=FakeClient(), space=OfflineSpace())
    assert UPSTREAM_TEXT in str(error.value)


@pytest.mark.asyncio
async def test_rank_cognition_classifies_the_provider_error_when_flag_is_on(monkeypatch):
    monkeypatch.setattr(settings, 'ALLOW_COGNITION_DEGRADED_CONTINUATION', True, raising=False)
    prompt = make_prompt(make_ledger())
    with pytest.raises(retrieval.EmbeddingUnavailable) as error:
        await retrieval.rank_cognition(prompt, client=FakeClient(), space=OfflineSpace())
    assert UPSTREAM_TEXT not in str(error.value)


@pytest.mark.asyncio
async def test_degrade_increments_the_visibility_metric(monkeypatch):
    """降级可以不报错,但不能让运维"看不见":必须留下可采集的计数。

    (不加在 selection 回执里是有意的:回执是带哈希校验的版本化契约,
    加字段要同时改生产端与校验端并升版本,风险与收益不成比例。)
    """
    from app.observability.metrics import COGNITION_RETRIEVAL_DEGRADED_TOTAL

    monkeypatch.setattr(settings, 'ALLOW_COGNITION_DEGRADED_CONTINUATION', True, raising=False)
    counter = COGNITION_RETRIEVAL_DEGRADED_TOTAL.labels(reason='embedding_unavailable')
    before = counter._value.get()
    await drive(retrieval.resolve_prompt_context, make_prompt(make_ledger()),
                space=OfflineSpace(fail_on=('passage',)), monkeypatch=monkeypatch)
    assert counter._value.get() == before + 1
