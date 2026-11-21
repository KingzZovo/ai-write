"""No-embedding, no-LLM lexical evidence ranking for cognition recall.

SAG2 式做法:字符二元组 + IDF 加权 + 长词精确命中加成,不调用任何嵌入模型或
语言模型,纯标准库。实测 2000 条 / 29 万字符台账单次排序约 0.15 秒、55 万字符约
0.24 秒(纯 Python 单线程,建索引占大头),即百万字符级亚秒级;它只是兜底与
可选重排,不在每章的常驻热路径上。用途有两个:

* 嵌入服务不可用时作为召回兜底(``cognition_retrieval`` 的降级分支);
* 嵌入正常时按 ``COGNITION_LEXICAL_BLEND`` 权重做可选混合重排。

评分近似 BM25(k1=1.2, b=0.75)并叠加固定长词加成:BM25 本身对"人名/物件名"
这类低文档频率词已经加权,长词加成补上 BM25 拆成二元组后丢掉的精确短语证据。
所有返回值只取决于输入(同输入两次调用逐元素相等),不读时间、随机数或环境。
"""
from __future__ import annotations

import math
import unicodedata
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

BM25_K1 = 1.2
BM25_B = 0.75
# 查询里长度 >= 3 的连续片段在文档中原样出现时的固定小权重。
LONG_HIT_MIN = 3
LONG_HIT_BONUS = 0.5
# 只检查片段内最长匹配,长度上限避免长句退化成 O(n^2)。
LONG_HIT_MAX = 12


def normalize(text) -> str:
    """NFKC、去标点与空白、casefold。非字符串一律当作空文本。

    ``str.isalnum`` 与 "Unicode 类别首字母是 L 或 N" 等价(str 的字母/数字定义
    就是 L*/N*),但它是 C 级单次调用,在整册台账上快一倍以上。
    """
    if not isinstance(text, str):
        return ''
    folded = unicodedata.normalize('NFKC', text).casefold()
    return ''.join(ch for ch in folded if ch.isalnum())


def _runs(text: str):
    """把规范化文本切成"连续 ASCII 词"与"连续非 ASCII 文字"两类片段。"""

    runs: list[tuple[str, str]] = []
    current = ''
    kind = ''
    for ch in text:
        ch_kind = 'ascii' if ch.isascii() and ch.isalnum() else 'text'
        if current and ch_kind != kind:
            runs.append((kind, current))
            current = ''
        current += ch
        kind = ch_kind
    if current:
        runs.append((kind, current))
    return runs


def char_ngrams(text, n: int = 2) -> list[str]:
    """字符 n 元组;连续 ASCII 词额外贡献其小写整词(词长短于 n 时只留整词)。"""
    return _ngrams(normalize(text), n)


def _ngrams(text: str, n: int) -> list[str]:
    """已归一化文本的 n 元组;建索引时与 ``normalize`` 共用一次规范化。"""
    if type(n) is not int or n < 1:
        raise ValueError('lexical_ngram_size_invalid')
    grams: list[str] = []
    for kind, run in _runs(text):
        if kind == 'ascii':
            # 整词只在比 n 长时才补充信息('井' 与 'jing' 都靠 n 元组覆盖)。
            if len(run) > n:
                grams.append(run)
            if len(run) < n:
                grams.append(run)
            else:
                grams.extend(run[start:start + n] for start in range(len(run) - n + 1))
            continue
        if len(run) < n:
            grams.append(run)
            continue
        grams.extend(run[start:start + n] for start in range(len(run) - n + 1))
    return grams


def long_phrases(query) -> list[str]:
    """查询里长度 >= LONG_HIT_MIN 的连续片段(顺序即出现顺序)。"""
    return [run for _kind, run in _runs(normalize(query)) if len(run) >= LONG_HIT_MIN]


def _minmax(values: Sequence[float]) -> list[float]:
    low, high = min(values), max(values)
    if high - low <= 0:
        # 全等时没有任何区分度可归一化;统一取 0,让另一路分数决定顺序。
        return [0.0 for _ in values]
    return [(value - low) / (high - low) for value in values]


def blend_scores(cosine: Mapping[str, float], lexical: Mapping[str, float], weight: float) -> dict[str, float]:
    """(1-w)*cos_norm + w*lexical_norm,两路各自 min-max 归一化。

    ``weight == 0`` 时逐字返回余弦分(不归一化、不改序),保证默认路径与
    纯余弦排序完全等价。词法分缺失的候选按该路最小分(0.0)处理;归一化区间
    只看余弦分已有的候选,因为向量检索之外的条目本来就不在本轮候选里。
    """
    if isinstance(weight, bool) or not isinstance(weight, (int, float)) or not math.isfinite(weight):
        raise ValueError('lexical_blend_weight_invalid')
    weight = float(weight)
    if not 0.0 <= weight <= 1.0:
        raise ValueError('lexical_blend_weight_invalid')
    ordered = {key: float(value) for key, value in cosine.items()}
    if weight == 0.0 or not ordered:
        return ordered
    keys = list(ordered)
    cosine_norm = _minmax([ordered[key] for key in keys])
    lexical_norm = _minmax([float(lexical.get(key, 0.0)) for key in keys])
    return {key: (1.0 - weight) * cos + weight * lex
            for key, cos, lex in zip(keys, cosine_norm, lexical_norm)}


@dataclass(frozen=True)
class LexicalIndex:
    """文档频率、平均长度与每篇的 tf 计数;只由输入文档决定。"""

    documents: tuple = field(repr=False)
    term_freqs: tuple = field(repr=False)
    doc_freqs: dict = field(repr=False)
    lengths: tuple
    average_length: float
    n_docs: int

    def idf(self, term: str) -> float:
        """平滑 IDF:某个词在所有文档里都没出现时也返回正数而不是除零。"""
        frequency = self.doc_freqs.get(term, 0)
        return math.log(1.0 + (self.n_docs - frequency + 0.5) / (frequency + 0.5))

    def __len__(self):
        return self.n_docs


def build_index(docs: Sequence[str]) -> LexicalIndex:
    documents: list[str] = []
    term_freqs: list[Counter] = []
    doc_freqs: Counter = Counter()
    lengths: list[int] = []
    for doc in docs:
        if not isinstance(doc, str):
            raise TypeError('lexical_document_invalid')
        text = normalize(doc)
        counts = Counter(_ngrams(text, 2))
        documents.append(text)
        term_freqs.append(counts)
        doc_freqs.update(counts.keys())
        lengths.append(sum(counts.values()))
    total = sum(lengths)
    return LexicalIndex(tuple(documents), tuple(term_freqs), dict(doc_freqs), tuple(lengths),
                        (total / len(lengths)) if lengths else 0.0, len(lengths))


def _longest_hit(phrase: str, document: str) -> int:
    for size in range(min(len(phrase), LONG_HIT_MAX), LONG_HIT_MIN - 1, -1):
        for start in range(len(phrase) - size + 1):
            if phrase[start:start + size] in document:
                return size
    return 0


def _score_at(index: LexicalIndex, position: int, query_terms: Mapping[str, int],
              phrases: Sequence[str]) -> float:
    counts = index.term_freqs[position]
    if not counts:
        return 0.0
    average = index.average_length or 1.0
    denominator = BM25_K1 * (1.0 - BM25_B + BM25_B * index.lengths[position] / average)
    total = 0.0
    for term in query_terms:
        frequency = counts.get(term, 0)
        if not frequency:
            continue
        # 查询内重复不额外加权(BM25 标准做法),只看文档内饱和频率。
        total += index.idf(term) * (frequency * (BM25_K1 + 1.0)) / (frequency + denominator)
    if not total:
        # 长词片段出现必然意味着它的二元组也出现,零分文档不必扫短语。
        return 0.0
    document = index.documents[position]
    for phrase in phrases:
        if _longest_hit(phrase, document) >= LONG_HIT_MIN:
            total += LONG_HIT_BONUS
    return total


def score(query, doc, *, index: LexicalIndex | None = None, position: int = 0) -> float:
    """BM25 近似 + IDF + 长词精确命中加成;单文档调用时临时建索引。"""
    if index is None:
        index = build_index([doc])
        position = 0
    if type(position) is not int or not 0 <= position < index.n_docs:
        raise ValueError('lexical_document_position_invalid')
    query_terms = Counter(char_ngrams(query))
    if not query_terms:
        return 0.0
    return _score_at(index, position, query_terms, long_phrases(query))


def rank(docs: Sequence[str], query, *, top_k: int | None = None) -> list[tuple[int, float]]:
    """按分数降序、同分按索引升序返回 (doc_index, score);空输入返回空列表。

    只返回分数为正(至少共享一个 n 元组或长词命中)的文档:零分文档不是"召回
    证据",把它们塞进排序会把预算浪费在完全无关的记录上。
    """
    if top_k is not None and (type(top_k) is not int or top_k < 0):
        raise ValueError('lexical_top_k_invalid')
    if top_k == 0:
        return []
    documents = tuple(docs)
    query_terms = Counter(char_ngrams(query))
    if not documents or not query_terms:
        return []
    index = build_index(documents)
    phrases = long_phrases(query)
    scored = [(position, _score_at(index, position, query_terms, phrases)) for position in range(len(documents))]
    ranked = [(position, value) for position, value in scored if value > 0.0]
    ranked.sort(key=lambda item: (-item[1], item[0]))
    return ranked if top_k is None else ranked[:top_k]