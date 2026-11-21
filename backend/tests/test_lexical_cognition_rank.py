"""SAG2 式词法证据召回:无嵌入、无 LLM 的兜底与可选混合重排。

全部用例纯离线:不联网,不启动 Qdrant 或 PostgreSQL,只用内存假件与标准库。
覆盖:归一化/二元组基本断言、中文相关性、人名与泛化词的 IDF 差异、
确定性(同输入两次逐元素相等)、空输入不抛异常、以及 ``COGNITION_LEXICAL_BLEND``
权重为 0 时与纯余弦排序完全等价。
"""
from __future__ import annotations

import logging
from collections import Counter
from contextlib import asynccontextmanager
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from qdrant_client.models import Distance, VectorParams

from app.config import settings
from app.services import cognition_retrieval as retrieval
from app.services import lexical_cognition_rank as lexical
from app.services.cognition_context import observation_key, render_history
from app.services.cognition_validation import CognitionLedger
from app.services.entity_sources import digest


def event(index, fact, *, kind='learns', evidence=None):
    return dict(chapter_idx=index, kind=kind, fact=fact, evidence=evidence or fact,
                source_hash=digest([index, fact]), source_chapter_id=str(uuid4()), source_mode='auto')


def make_ledger(rows):
    ledger = CognitionLedger({name: {'history': [item]} for name, item in rows.items()})
    ledger.snapshot_hash = digest(dict(ledger))
    return ledger


# ---------------------------------------------------------------- 归一化与 n 元组


def test_normalize_folds_width_case_and_drops_punctuation():
    assert lexical.normalize('ＡＢＣ，abc！ １２３') == 'abcabc123'
    assert lexical.normalize('校服, 工钱。') == '校服工钱'
    assert lexical.normalize(None) == '' and lexical.normalize(42) == ''


def test_char_ngrams_use_cjk_bigrams_and_ascii_whole_words():
    assert lexical.char_ngrams('工钱八百') == ['工钱', '钱八', '八百']
    # 连续 ASCII:整词 + 小写二元组(输入已 casefold)。
    assert lexical.char_ngrams('AliPay') == ['alipay', 'al', 'li', 'ip', 'pa', 'ay']
    # 单字符片段保留自身,不能被丢掉。
    assert lexical.char_ngrams('井') == ['井']
    assert lexical.char_ngrams('') == []


def test_index_counts_match_the_public_ngram_view():
    """建索引复用一次规范化,但 tf 必须与 char_ngrams 逐项一致。"""
    doc = 'AliPay 2024 年校服工钱:八百,井口。'
    index = lexical.build_index([doc])
    assert index.term_freqs[0] == Counter(lexical.char_ngrams(doc))
    assert index.lengths[0] == sum(index.term_freqs[0].values())
    assert index.documents[0] == lexical.normalize(doc)


def test_long_phrase_hits_are_punctuation_insensitive():
    # 归一化先去标点,所以跨标点的连续片段仍算同一片段(文档侧同样归一化)。
    assert lexical.long_phrases('工钱,八百') == ['工钱八百']
    assert lexical.long_phrases('工钱八百块') == ['工钱八百块']


# ------------------------------------------------------------------- 中文相关性


def test_chinese_query_puts_the_matching_document_first():
    docs = ['村里的水路后来没人提了。', '校服已经洗好放在柜子里。', '工钱八百还没结清,校服也旧了。']
    ranked = lexical.rank(docs, '校服工钱')
    assert [position for position, _value in ranked][0] == 2
    # 分数降序,而且确实压过只命中一个词的文档。
    assert ranked[0][1] > ranked[1][1]


def test_exact_long_phrase_beats_bigram_only_match():
    docs = ['工钱八百要等到月底。', '工钱已经给过,八百是另一笔。']
    ranked = lexical.rank(docs, '工钱八百')
    assert [position for position, _value in ranked] == [0, 1]


def test_specific_name_beats_generic_words():
    """人名(低文档频率)必须压过每篇都出现的泛化词。"""
    docs = [f'第{index}条消息:天亮了。' for index in range(20)]
    docs.append('周芸把信压在碗下面,天亮了。')
    ranked = lexical.rank(docs, '周芸消息')
    assert [position for position, _value in ranked][0] == 20


def test_idf_rewards_the_rare_term():
    index = lexical.build_index(['周芸来了。', '别人走了。', '别人来了。'])
    assert index.idf('周芸') > index.idf('来了')


# --------------------------------------------------------------------- 确定性


def test_rank_is_deterministic_for_identical_input():
    docs = ['工钱八百', '水路塌了', '校服补过两次', '工钱八百还没结']
    first = lexical.rank(docs, '工钱 校服')
    second = lexical.rank(docs, '工钱 校服')
    assert first == second and [position for position, _v in first] == [2, 0, 3]
    # 单文档临时索引与共享索引的 IDF 分量不同,但同一输入必须给出同一结果。
    assert lexical.score('工钱', docs[0]) == lexical.score('工钱', docs[0])
    index = lexical.build_index(docs)
    assert (lexical.score('工钱', docs[0], index=index, position=0)
            == lexical.score('工钱', docs[0], index=index, position=0))


# ----------------------------------------------------------------------- 空输入


def test_empty_query_and_empty_documents_do_not_raise():
    assert lexical.rank([], '工钱') == []
    assert lexical.rank(['工钱八百'], '') == []
    assert lexical.rank(['工钱八百'], '，，。') == []
    assert lexical.rank([''], '工钱') == []
    assert lexical.score('', '工钱八百') == 0.0
    assert lexical.score('工钱', '') == 0.0
    assert len(lexical.build_index([])) == 0 and lexical.build_index([]).average_length == 0.0


def test_rank_respects_top_k_and_rejects_invalid_values():
    docs = ['工钱八百', '工钱一万', '工钱三百']
    assert len(lexical.rank(docs, '工钱', top_k=2)) == 2
    assert lexical.rank(docs, '工钱', top_k=0) == []
    with pytest.raises(ValueError):
        lexical.rank(docs, '工钱', top_k=-1)
    with pytest.raises(TypeError):
        lexical.build_index(['工钱', 3])


# -------------------------------------------------------- 混合重排的等价性(w=0)

COSINE = {'a': 0.9, 'b': 0.2, 'c': 0.5}
LEXICAL = {'a': 0.0, 'b': 4.0, 'c': 1.0}


def test_blend_weight_zero_returns_cosine_scores_untouched():
    assert lexical.blend_scores(COSINE, LEXICAL, 0.0) == COSINE
    assert lexical.blend_scores(COSINE, LEXICAL, 0) == COSINE
    # 顺序与纯余弦排序一致。
    pure = [key for key, _ in sorted(COSINE.items(), key=lambda item: (-item[1], item[0]))]
    blended = [key for key, _ in sorted(lexical.blend_scores(COSINE, LEXICAL, 0.0).items(),
                                       key=lambda item: (-item[1], item[0]))]
    assert blended == pure == ['a', 'c', 'b']


def test_blend_weight_one_is_the_lexical_order():
    blended = lexical.blend_scores(COSINE, LEXICAL, 1.0)
    assert sorted(blended, key=lambda key: -blended[key]) == ['b', 'c', 'a']
    assert all(0.0 <= value <= 1.0 for value in blended.values())


def test_blend_rejects_out_of_range_weights():
    for bad in (-0.1, 1.5, float('nan'), float('inf'), True, '0.5'):
        with pytest.raises(ValueError):
            lexical.blend_scores(COSINE, LEXICAL, bad)


# ------------------------------------------------ 接线:env 读取与端到端等价性


def test_blend_weight_is_read_from_the_environment(monkeypatch):
    monkeypatch.delenv('COGNITION_LEXICAL_BLEND', raising=False)
    assert retrieval._lexical_blend_weight() == 0.0
    monkeypatch.setenv('COGNITION_LEXICAL_BLEND', '0.35')
    assert retrieval._lexical_blend_weight() == 0.35
    monkeypatch.setenv('COGNITION_LEXICAL_BLEND', '0')
    assert retrieval._lexical_blend_weight() == 0.0
    for bad in ('abc', '', '-1', '2', 'nan'):
        monkeypatch.setenv('COGNITION_LEXICAL_BLEND', bad)
        assert retrieval._lexical_blend_weight() == 0.0


class OfflineSpace:
    """确定性的 2 维假编码器;fail_on 里列出的输入类型一律视为服务不可用。"""

    identity = digest('offline-lexical-space-v1')

    def __init__(self, *, fail_on=()):
        self.fail_on = tuple(fail_on)

    async def embed(self, texts, *, input_type):
        if input_type in self.fail_on:
            raise RuntimeError('provider exploded')
        return [[1.0, 0.0] for _ in texts]


class GroupedClient:
    """只实现 rank_cognition 用到的向量库表面;分数由用例指定。"""

    def __init__(self, scores):
        self.scores = dict(scores)
        self.collections = {}
        self.entries = {}

    async def collection_exists(self, name):
        return name in self.collections

    async def create_collection(self, collection_name, vectors_config):
        self.collections[collection_name] = vectors_config

    async def get_collection(self, name):
        return NS(config=NS(params=NS(vectors=VectorParams(size=2, distance=Distance.COSINE))))

    async def retrieve(self, *, collection_name, ids, with_payload=True, with_vectors=False):
        return []

    async def upsert(self, *, collection_name, points, wait=True):
        self.collections.setdefault(collection_name, None)
        return True

    async def query_points_groups(self, *, collection_name, query, query_filter, group_by, group_size,
                                  limit, with_payload, with_vectors):
        groups = [NS(id=entry['key'], hits=[NS(id=point_id, payload=entry['payload'],
                                               score=self.scores[entry['key']])])
                  for point_id, entry in self.entries.items()]
        return NS(groups=groups)


@asynccontextmanager
async def index_lock(project_id, *, wait=False):
    yield NS(check=AsyncMock())


def sessions_for(ledger, monkeypatch):
    @asynccontextmanager
    async def sessions():
        yield NS()

    monkeypatch.setattr('app.services.character_cognition.load_ledger', AsyncMock(return_value=ledger))
    return sessions


async def cosine_ranking(monkeypatch, *, blend, prompt, scores):
    """走真实 rank_cognition,唯一的外部世界是假 client / 假 space。"""
    monkeypatch.setenv('COGNITION_LEXICAL_BLEND', blend)
    space = OfflineSpace()
    client = GroupedClient(scores)
    client.entries = retrieval.index_entries(prompt, space.identity)
    return await retrieval.rank_cognition(prompt, client=client, space=space)


@pytest.mark.asyncio
async def test_rank_cognition_with_zero_blend_is_pure_cosine_order(monkeypatch):
    rows = {'甲': event(1, '工钱八百还没结清。'), '乙': event(2, '水路塌了。'), '丙': event(3, '校服补过两次。')}
    prompt = retrieval.CognitionPrompt.create(uuid4(), make_ledger(rows), '工钱八百 校服', (), 600,
                                             before_global_idx=5, before_chapter_id=None)
    keys = {name: observation_key(name, item) for name, item in rows.items()}
    # 余弦分刻意与词法分相反:词法最爱 甲,余弦最不爱 甲。
    scores = {keys['甲']: 0.1, keys['乙']: 0.9, keys['丙']: 0.5}
    pure = await cosine_ranking(monkeypatch, blend='0', prompt=prompt, scores=scores)
    assert pure == [keys['乙'], keys['丙'], keys['甲']]
    # w=0 时连词法都不得计算:让词法打分直接失败,排序仍然成立。
    with patch.object(retrieval.lexical_rank, 'rank', side_effect=AssertionError('lexical must not run')):
        default = await cosine_ranking(monkeypatch, blend='', prompt=prompt, scores=scores)
    assert default == pure
    assert await cosine_ranking(monkeypatch, blend='0.0', prompt=prompt, scores=scores) == pure


@pytest.mark.asyncio
async def test_rank_cognition_blend_one_follows_lexical_evidence(monkeypatch):
    rows = {'甲': event(1, '工钱八百还没结清。'), '乙': event(2, '水路塌了。'), '丙': event(3, '校服补过两次。')}
    prompt = retrieval.CognitionPrompt.create(uuid4(), make_ledger(rows), '工钱八百 校服', (), 600,
                                             before_global_idx=5, before_chapter_id=None)
    keys = {name: observation_key(name, item) for name, item in rows.items()}
    scores = {keys['甲']: 0.1, keys['乙']: 0.9, keys['丙']: 0.5}
    blended = await cosine_ranking(monkeypatch, blend='1', prompt=prompt, scores=scores)
    assert blended[0] == keys['甲'] and set(blended) == set(scores)


# ------------------------------------------- 降级:嵌入不可用时改走词法排序


@pytest.mark.asyncio
async def test_degraded_selection_is_lexically_ranked(monkeypatch, caplog):
    monkeypatch.setattr(settings, 'ALLOW_COGNITION_DEGRADED_CONTINUATION', True, raising=False)
    match = event(1, '工钱八百还没结清。')
    ledger = make_ledger({'甲': match, '乙': event(2, '水路塌了。'), '丙': event(3, '校服补过两次。')})
    prompt = retrieval.CognitionPrompt.create(uuid4(), ledger, '工钱八百', (), 600,
                                             before_global_idx=5, before_chapter_id=None)
    with caplog.at_level(logging.WARNING, logger='app.services.cognition_retrieval'):
        selection = await retrieval.resolve_prompt_selection(prompt, client=GroupedClient({}),
            space_loader=AsyncMock(return_value=OfflineSpace(fail_on=('passage',))),
            sessions=sessions_for(ledger, monkeypatch), lock_factory=index_lock)
    assert selection.ranking_source == retrieval.RANKING_SOURCE_LEXICAL
    assert selection.ranked_keys == (observation_key('甲', match),)
    assert selection.ranking_scores and selection.ranking_scores[0][1] > 0
    assert selection.text == render_history(ledger, 600, ranked_keys=selection.ranked_keys)
    assert '降级' in caplog.text and '词法' in caplog.text


@pytest.mark.asyncio
async def test_degraded_selection_without_candidate_text_stays_unranked(monkeypatch):
    """条目拿不到文本时回到既有的"无排序台账",不抛异常。"""
    monkeypatch.setattr(settings, 'ALLOW_COGNITION_DEGRADED_CONTINUATION', True, raising=False)
    blank = dict(event(1, ''), evidence='')
    ledger = make_ledger({'甲': blank, '乙': dict(event(2, ''), evidence='')})
    prompt = retrieval.CognitionPrompt.create(uuid4(), ledger, '工钱八百', (), 600,
                                             before_global_idx=5, before_chapter_id=None)
    selection = await retrieval.resolve_prompt_selection(prompt, client=GroupedClient({}),
        space_loader=AsyncMock(return_value=OfflineSpace(fail_on=('query', 'passage'))),
        sessions=sessions_for(ledger, monkeypatch), lock_factory=index_lock)
    assert selection.ranking_source == retrieval.RANKING_SOURCE_EMBEDDING
    assert selection.ranked_keys == () and selection.ranking_scores == ()
    assert selection.text == render_history(ledger, 600)


@pytest.mark.asyncio
async def test_lexical_fallback_failure_cannot_break_writing(monkeypatch, caplog):
    """词法兜底自身出错也只能回到无排序,绝不能让降级路径抛错。"""
    monkeypatch.setattr(settings, 'ALLOW_COGNITION_DEGRADED_CONTINUATION', True, raising=False)
    ledger = make_ledger({'甲': event(1, '工钱八百还没结清。')})
    prompt = retrieval.CognitionPrompt.create(uuid4(), ledger, '工钱八百', (), 600,
                                             before_global_idx=5, before_chapter_id=None)
    with patch.object(retrieval, '_lexical_recall', side_effect=ValueError('broken')):
        with caplog.at_level(logging.WARNING, logger='app.services.cognition_retrieval'):
            selection = await retrieval.resolve_prompt_selection(prompt, client=GroupedClient({}),
                space_loader=AsyncMock(return_value=OfflineSpace(fail_on=('passage',))),
                sessions=sessions_for(ledger, monkeypatch), lock_factory=index_lock)
    assert selection.ranked_keys == () and selection.ranking_source == retrieval.RANKING_SOURCE_EMBEDDING
    assert 'lexical_failed=ValueError' in caplog.text

def test_blend_weight_reads_settings_when_env_is_unset(monkeypatch):
    """运维可以通过配置项开启混合重排,不必依赖环境变量。"""
    from app.config import settings
    from app.services.cognition_retrieval import _lexical_blend_weight

    monkeypatch.delenv("COGNITION_LEXICAL_BLEND", raising=False)
    monkeypatch.setattr(settings, "COGNITION_LEXICAL_BLEND", 0.25, raising=False)
    assert _lexical_blend_weight() == 0.25

    # 环境变量仍然可以临时覆盖配置项(运维需要不改配置就能关掉)。
    monkeypatch.setenv("COGNITION_LEXICAL_BLEND", "0")
    assert _lexical_blend_weight() == 0.0
