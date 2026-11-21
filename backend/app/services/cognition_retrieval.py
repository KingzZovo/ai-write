"""Semantic material selection over current, source-verified cognition history.

Qdrant holds derived vectors and opaque receipts, not a second fact ledger.
Current point IDs are an explicit search allowlist. Old or late writes cannot
crowd out current points or overwrite another source's immutable vector.
"""
import asyncio
import logging
import math
import os
from copy import deepcopy
from dataclasses import dataclass, field, replace
from uuid import UUID, NAMESPACE_URL, uuid5

from qdrant_client.models import Distance, Filter, HasIdCondition, PointStruct, VectorParams

from app.services import lexical_cognition_rank as lexical_rank
from app.services.cognition_context import observation_key, render_history
from app.services.cognition_index_lock import cognition_index_lock
from app.services.context_errors import RequiredContextUnavailable
from app.services.embedding_space import current_embedding_space, split_embedding_text, validate_vector, EMBEDDING_BATCH_SIZE
from app.services.entity_sources import digest
from app.services.qdrant_store import cognition_collection

RETRIEVAL_SECONDS = 180
RETRIEVAL_LIMIT = 64
# 召回来源标记:向量(默认)与无嵌入的词法兜底。
RANKING_SOURCE_EMBEDDING = 'embedding'
RANKING_SOURCE_LEXICAL = 'lexical'
logger = logging.getLogger(__name__)


class EmbeddingUnavailable(Exception):
    """The embedding provider could not answer (transport, HTTP, timeout).

    Carries only the provider exception's type name; upstream text can hold
    endpoint or credential detail and must never travel further.
    """


async def load_candidate_context(project_id, *, before_chapter_id, chapter_outline, chapter_text,
                                 previous_text='', user_instruction='', sessions=None):
    """Each review/rewrite attempt gets material for its actual current draft."""
    from app.db.session import async_session_factory
    from app.services.character_cognition import load_prompt_context
    from app.config import settings as _settings
    sessions = sessions or async_session_factory
    try:
        async with sessions() as db:
            prepared = await load_prompt_context(db, project_id, before_chapter_id=before_chapter_id,
                chapter_outline=chapter_outline, chapter_text=chapter_text, previous_text=previous_text,
                # 与写作主路径同预算,否则"评审/改写"这条路会先撞线。
                max_chars=int(getattr(_settings, "COGNITION_HISTORY_MAX_CHARS", 3000) or 3000),
                defer_retrieval=True)
        return await resolve_prompt_context(prepared, sessions=sessions, extra_context=user_instruction)
    except RequiredContextUnavailable:
        raise
    except Exception as exc:
        raise RequiredContextUnavailable('character_cognition_context_unavailable') from exc


@dataclass(frozen=True)
class CognitionPrompt:
    project_id: str
    ledger: dict = field(repr=False)
    snapshot_hash: str
    context: str = field(repr=False)
    priority_names: tuple
    max_chars: int
    before_global_idx: int | None
    before_chapter_id: str | None

    def __str__(self):
        raise RequiredContextUnavailable('character_cognition_retrieval_pending')

    @classmethod
    def create(cls, project_id, ledger, context, priority_names, max_chars, *, before_global_idx, before_chapter_id):
        stamp = getattr(ledger, 'snapshot_hash', None)
        if not stamp:
            raise RequiredContextUnavailable('character_cognition_snapshot_unavailable')
        return cls(str(UUID(str(project_id))), deepcopy(ledger), stamp, context, tuple(priority_names), max_chars,
            before_global_idx, str(before_chapter_id) if before_chapter_id else None)


@dataclass(frozen=True)
class CognitionSelection:
    """已选材料与排序来源;后两个字段只供观测,不属于持久回执。"""

    text: str = field(repr=False)
    ranked_keys: tuple
    space_id: str | None
    # 排序来源('embedding' | 'lexical')与逐条分数(仅词法兜底给出实际分值)。
    # 持久回执仍是 space_id/ranked_keys/text 三项,校验端逐字不变。
    ranking_source: str = RANKING_SOURCE_EMBEDDING
    ranking_scores: tuple = field(default=(), repr=False)


async def embed_texts(space, texts, *, input_type):
    """Classify provider reachability separately from data integrity.

    With degraded continuation off, the provider's own exception travels
    unchanged so every current caller keeps its behaviour byte for byte.
    When the author opted in, a transport/HTTP/timeout failure is re-raised as
    ``EmbeddingUnavailable``; ``EmbeddingSpace.embed`` reports configuration
    drift, malformed batches and malformed vectors as ``ValueError``, and those
    must keep blocking.
    """
    try:
        return await space.embed(texts, input_type=input_type)
    except ValueError:
        raise
    except Exception as exc:
        if not _degraded_continuation_enabled():
            raise
        # The provider's own message may carry endpoint or credential detail.
        raise EmbeddingUnavailable(type(exc).__name__) from exc


def _exception_leaves(exc):
    """Every leaf of a (possibly nested) task-group failure, in order."""
    if isinstance(exc, BaseExceptionGroup):
        return [leaf for child in exc.exceptions for leaf in _exception_leaves(child)]
    return [exc]


def _embedding_unavailable(exc):
    """True only when every recorded cause is a provider outage."""
    leaves = _exception_leaves(exc)
    return bool(leaves) and all(isinstance(leaf, EmbeddingUnavailable) for leaf in leaves)


def _degraded_continuation_enabled() -> bool:
    """嵌入服务不可用时是否允许继续写作(默认关闭 = 维持现有阻断行为)。

    开启后只影响"嵌入服务不可用"这一类软失败;快照变化、空间指纹变化、索引
    回执不符等真实损坏照旧抛错(见 ``resolve_prompt_selection`` 的核对顺序)。
    """
    try:
        from app.config import settings
        return bool(getattr(settings, 'ALLOW_COGNITION_DEGRADED_CONTINUATION', False))
    except Exception:  # noqa: BLE001
        return False


def _lexical_blend_weight() -> float:
    """可选混合重排权重;默认 0 = 关闭,排序结果与纯余弦逐字不变。

    取值顺序:环境变量 ``COGNITION_LEXICAL_BLEND``(运维临时覆盖)> 配置项
    ``settings.COGNITION_LEXICAL_BLEND``;都没有时为 0。非法值(非数字、NaN、
    超出 0..1)按关闭处理并留一条 warning,绝不让写作链因为一个拼错的配置而改序或中断。
    """
    raw = os.environ.get('COGNITION_LEXICAL_BLEND')
    if raw in (None, ''):
        try:
            from app.config import settings as _settings

            raw = str(getattr(_settings, 'COGNITION_LEXICAL_BLEND', 0.0))
        except Exception:  # noqa: BLE001 - 配置读不到就按关闭处理
            raw = '0'
    raw = raw if raw not in (None, '') else '0' 
    try:
        weight = float(raw)
    except (TypeError, ValueError):
        weight = 0.0
    if not math.isfinite(weight) or not 0.0 <= weight <= 1.0:
        logger.warning('cognition_lexical_blend_ignored value=%r;只接受 0..1', raw)
        return 0.0
    return weight


def _lexical_query(prompt) -> str:
    """当前上下文 + 优先人物名(查询时关键词扩展)。

    这里刻意不加 ``人物:`` 之类的标签:向量查询需要它区分字段,而词法打分里
    这类词出现在每一篇文档中,只会稀释 IDF。
    """
    names = '、'.join(name for name in prompt.priority_names if isinstance(name, str) and name)
    return f'{prompt.context}\n{names}' if names else prompt.context


def _lexical_event_texts(prompt):
    """每条当前观察一条文档(不切块),键空间与向量索引一致。"""
    keys, documents = [], []
    for name, row in prompt.ledger.items():
        for item in row.get('history', ()):
            fact = item.get('fact')
            if not isinstance(fact, str) or not fact.strip():
                continue
            keys.append(observation_key(name, item))
            documents.append(f"人物:{name}\n认知性质:{item.get('kind', '')}\n内容:{fact}"
                             f"\n原文:{item.get('evidence') or ''}")
    return keys, documents


def _lexical_recall(prompt, *, limit=RETRIEVAL_LIMIT):
    """无嵌入召回:(有序键, 逐条词法分);没有可用文本时返回两个空元组。

    只读 ``prompt.ledger``,不碰嵌入服务与向量库;空查询、空台账、全零分
    自然得到空结果,由调用方回落"无排序台账"。
    """
    keys, documents = _lexical_event_texts(prompt)
    if not documents:
        return (), ()
    ranked = lexical_rank.rank(documents, _lexical_query(prompt), top_k=limit)
    return (tuple(keys[position] for position, _ in ranked),
            tuple((keys[position], value) for position, value in ranked))


def _lexical_blend(prompt, scores):
    """按 COGNITION_LEXICAL_BLEND 把词法分混进余弦分;权重 0 时原样返回。"""
    weight = _lexical_blend_weight()
    if weight == 0.0 or not scores:
        return scores
    keys, documents = _lexical_event_texts(prompt)
    if not documents:
        return scores
    lexical = {keys[position]: value
               for position, value in lexical_rank.rank(documents, _lexical_query(prompt))}
    return lexical_rank.blend_scores(scores, lexical, weight)


def iter_index_entries(prompt, space_id):
    for name, row in prompt.ledger.items():
        for item in row.get('history', []):
            key = observation_key(name, item)
            document = f"人物：{name}\n认知性质：{item['kind']}\n内容：{item['fact']}\n原文：{item['evidence']}"
            for index, part in enumerate(split_embedding_text(document)):
                content_hash = digest([prompt.project_id, space_id, key, index, part])
                point_id = str(uuid5(NAMESPACE_URL, content_hash))
                yield point_id, dict(key=key, text=part, payload=dict(version=1, project_id=prompt.project_id,
                    space_id=space_id, content_hash=content_hash, event_key=key))


def index_entries(prompt, space_id):
    return dict(iter_index_entries(prompt, space_id))


async def ensure_collection(client, name, dimension):
    if not await client.collection_exists(name):
        try:
            await client.create_collection(collection_name=name,
                vectors_config=VectorParams(size=dimension, distance=Distance.COSINE))
        except Exception:
            # A concurrent preparer may create the same immutable space.
            # Transport/auth failures are never converted into an empty index.
            if not await client.collection_exists(name):
                raise
    info = await client.get_collection(name)
    config = info.config.params.vectors
    if not isinstance(config, VectorParams) or config.size != dimension or config.distance != Distance.COSINE:
        raise ValueError('cognition_index_configuration_invalid')


async def prepare_index(client, name, entries, space, dimension):
    """Resume from complete point receipts; a cancelled batch never means done."""
    missing = []
    ids = list(entries)
    for start in range(0, len(ids), 256):
        batch = ids[start:start + 256]
        existing = await client.retrieve(collection_name=name, ids=batch, with_payload=True, with_vectors=False)
        found = {str(point.id): point.payload for point in existing}
        for point_id in batch:
            if found.get(point_id) != entries[point_id]['payload']:
                missing.append(point_id)
    batches = iter(missing[start:start + EMBEDDING_BATCH_SIZE]
                   for start in range(0, len(missing), EMBEDDING_BATCH_SIZE))

    async def write_batches():
        for batch in batches:
            vectors = await embed_texts(space, [entries[pid]['text'] for pid in batch], input_type='passage')
            if len(vectors) != len(batch):
                raise ValueError('embedding_response_count_invalid')
            points = [PointStruct(id=pid, vector=validate_vector(vector, dimension), payload=entries[pid]['payload'])
                      for pid, vector in zip(batch, vectors)]
            await client.upsert(collection_name=name, points=points, wait=True)

    async with asyncio.TaskGroup() as tasks:
        for _ in range(min(4, len(missing))):
            tasks.create_task(write_batches())


async def rank_cognition(prompt, *, client, space):
    """Every current observation is indexed; query chunks cover the full context."""
    query_parts = split_embedding_text(prompt.context + '\n人物：' + '、'.join(prompt.priority_names))
    if not query_parts:
        raise ValueError('cognition_query_empty')
    query_vectors = []
    for start in range(0, len(query_parts), EMBEDDING_BATCH_SIZE):
        query_vectors.extend(await embed_texts(space, query_parts[start:start + EMBEDDING_BATCH_SIZE], input_type='query'))
    dimension = len(query_vectors[0])
    query_vectors = [validate_vector(v, dimension) for v in query_vectors]
    entries = index_entries(prompt, space.identity)
    if not entries:
        return []
    name = cognition_collection(prompt.project_id, space.identity, dimension)
    await ensure_collection(client, name, dimension)
    await prepare_index(client, name, entries, space, dimension)
    scores = {}
    allowed = Filter(must=[HasIdCondition(has_id=list(entries))])
    expected_groups = min(RETRIEVAL_LIMIT, len({entry['key'] for entry in entries.values()}))
    for vector in query_vectors:
        # Group on the event, not the slice: a very long quote must not
        # occupy all recall slots with adjacent chunks of the same event.
        result = await client.query_points_groups(collection_name=name, query=vector,
            query_filter=allowed, group_by='event_key', group_size=1,
            limit=RETRIEVAL_LIMIT, with_payload=True, with_vectors=False)
        if not result.groups:
            raise ValueError('cognition_search_empty')
        if len(result.groups) != expected_groups or len({group.id for group in result.groups}) != expected_groups:
            raise ValueError('cognition_search_incomplete')
        for group in result.groups:
            if len(group.hits) != 1:
                raise ValueError('cognition_search_group_invalid')
            hit = group.hits[0]
            entry = entries.get(str(hit.id))
            if entry is None or hit.payload != entry['payload'] or group.id != entry['key']:
                raise ValueError('cognition_search_receipt_invalid')
            score = float(hit.score)
            if not math.isfinite(score):
                raise ValueError('cognition_search_score_invalid')
            scores[entry['key']] = max(scores.get(entry['key'], float('-inf')), score)
    scores = _lexical_blend(prompt, scores)
    return [key for key, _ in sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:RETRIEVAL_LIMIT]]


async def resolve_prompt_context(value, *, sessions=None, client=None, space_loader=None, db=None,
                                 extra_context='', lock_factory=None):
    """Keep the string interface used by writing, revision and review callers."""
    if isinstance(value, str):
        return value
    selection = await resolve_prompt_selection(value, sessions=sessions, client=client, space_loader=space_loader,
        db=db, extra_context=extra_context, lock_factory=lock_factory)
    return selection.text


@dataclass(frozen=True)
class _Recall:
    """本轮有序键与来源标记;只用于观测,不进入持久回执。"""

    keys: tuple = ()
    source: str = RANKING_SOURCE_EMBEDDING
    scores: tuple = ()


async def _ranked_or_degraded(prompt, *, client, space):
    """Semantic ranking is an optional enhancement over the current ledger.

    Only a provider outage may fall back, and only when the author opted in.
    降级不再是"无排序":先用不依赖任何外部服务的词法证据打分(字符二元组 +
    IDF + 长词命中)对同一批当前观察排序;拿不到候选文本(或词法兜底自身出错)
    才回到原来的无排序台账。索引回执不符照旧阻断,作者决定/嵌入空间的变化由
    调用方随后重验。
    """
    try:
        return _Recall(tuple(await rank_cognition(prompt, client=client, space=space)))
    except Exception as exc:
        if not (_degraded_continuation_enabled() and _embedding_unavailable(exc)):
            raise
        keys, scores = (), ()
        try:
            keys, scores = _lexical_recall(prompt)
        except Exception as fallback:  # noqa: BLE001 —— 兜底自身失败也只能回到无排序
            logger.warning('cognition_retrieval_degraded project=%s before_chapter=%s lexical_failed=%s',
                           prompt.project_id, prompt.before_chapter_id, type(fallback).__name__)
            keys, scores = (), ()
        logger.warning('cognition_retrieval_degraded project=%s before_chapter=%s causes=%s;'
                       '嵌入服务不可用,本次降级继续:%s',
                       prompt.project_id, prompt.before_chapter_id,
                       ','.join(sorted({str(leaf) for leaf in _exception_leaves(exc)})),
                       f'按词法证据排序{len(keys)}条(无嵌入)' if keys
                       else '无可用候选文本,按完整认知台账不排序继续')
        try:
            from app.observability.metrics import COGNITION_RETRIEVAL_DEGRADED_TOTAL

            COGNITION_RETRIEVAL_DEGRADED_TOTAL.labels(reason='embedding_unavailable').inc()
        except Exception:  # noqa: BLE001  —— 指标不可用绝不能影响写作
            pass
        if not keys:
            return _Recall()
        return _Recall(tuple(keys), RANKING_SOURCE_LEXICAL, tuple(scores))


async def resolve_prompt_selection(value, *, sessions=None, client=None, space_loader=None, db=None,
                                   extra_context='', lock_factory=None):
    """Resolve outside transactions; expose exact ranks for durable stage inputs."""
    if not isinstance(value, CognitionPrompt):
        raise RequiredContextUnavailable('character_cognition_snapshot_unavailable')
    if db is not None and db.in_transaction():
        # This is a borrowed session. Only its owner may commit/roll back
        # pending writes; never discard them to make a network call possible.
        raise RequiredContextUnavailable('character_cognition_transaction_open')
    if extra_context:
        value = replace(value, context=value.context + '\n' + extra_context)
    from app.db.session import async_session_factory
    from app.services.character_cognition import load_ledger
    sessions = sessions or async_session_factory
    space_loader = space_loader or current_embedding_space
    lock_factory = lock_factory or cognition_index_lock
    owns_client = client is None
    space = None
    try:
        async with asyncio.timeout(RETRIEVAL_SECONDS):
            async with lock_factory(value.project_id, wait=True) as lease:
                await lease.check()
                space = await space_loader()
                if client is None:
                    from qdrant_client import AsyncQdrantClient
                    from app.config import settings
                    client = AsyncQdrantClient(host=settings.QDRANT_HOST, port=settings.QDRANT_PORT,
                                              timeout=30, check_compatibility=False)
                recall = await _ranked_or_degraded(value, client=client, space=space)
                await lease.check()
                latest_space = await space_loader()
                try:
                    if latest_space.identity != space.identity:
                        raise RequiredContextUnavailable('character_cognition_embedding_changed')
                finally:
                    if latest_space is not space and hasattr(latest_space, 'aclose'):
                        await latest_space.aclose()
                async with sessions() as db:
                    current = await load_ledger(db, value.project_id, before_global_idx=value.before_global_idx,
                        before_chapter_id=value.before_chapter_id)
                    if getattr(current, 'snapshot_hash', None) != value.snapshot_hash:
                        raise RequiredContextUnavailable('character_cognition_decision_changed')
                await lease.check()
                # A degraded selection keeps the loaded space identity: downstream
                # receipts read `space_id is None` as "everything fitted, no ranking
                # was needed", and an empty rank list is already a valid selection.
                # 词法兜底只改排序来源与分值,回执三字段(space_id/ranked_keys/text)不变。
                return CognitionSelection(render_history(value.ledger, value.max_chars,
                    priority_names=value.priority_names, context_text=value.context, ranked_keys=recall.keys),
                    tuple(recall.keys), space.identity, recall.source, recall.scores)
    except RequiredContextUnavailable:
        raise
    except Exception as exc:
        raise RequiredContextUnavailable('character_cognition_retrieval_unavailable') from exc
    finally:
        for resource, method in ((client if owns_client else None, 'close'), (space, 'aclose')):
            if resource is not None and hasattr(resource, method):
                try:
                    async with asyncio.timeout(5):
                        await getattr(resource, method)()
                except Exception as exc:
                    # Cleanup must not replace cancellation or the actionable
                    # failure, and must not leak provider exception contents.
                    logger.warning('Cognition resource close failed (%s)', type(exc).__name__)
