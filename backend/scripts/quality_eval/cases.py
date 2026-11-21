"""固定样本集的读取与校验。

样本集是质量验收的输入合同:每个 case 必须自带"必须保持的事实""不得新增的元素"
和"本场必须完成的结果",否则检测器无法给出可复核的结论。

字段:
- id / label / genre:标识与题材标签。
- pov / pov_character / cast:叙述视角、视角人物、出场人物表。用于视角越界检测。
- target_words / word_range:目标字数与可接受区间。
- brief:交给模型的场景要求(与生产链的"作者要求"同构)。
- must_keep[]:必须保持的事实。``tokens_all`` 必须全部出现,``tokens_any`` 至少出现一个,
  ``co_any`` 需在同一段内与主词共现。
- forbidden[]:不得新增的元素。``tokens_any`` 任一出现即算违规。
- required_outcomes[]:本场必须完成的结果,判定方式同 must_keep。
- mechanism_terms[]:机制/规则词。用于规则复述检测。
- numbers_allowed[]:允许出现的数量表达。用于数字改动检测。
- ending_required_any[]:结尾必须落到的东西。用于结尾未兑现检测。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class CaseSetError(ValueError):
    """样本集结构不合法;调用方必须停止,不得静默降级。"""


_TOKEN_KEYS = ("tokens_all", "tokens_any", "co_any", "pattern_any")


@dataclass(frozen=True)
class Requirement:
    id: str
    desc: str
    tokens_all: tuple[str, ...] = ()
    tokens_any: tuple[str, ...] = ()
    co_any: tuple[str, ...] = ()
    pattern_any: tuple[str, ...] = ()


@dataclass(frozen=True)
class Case:
    id: str
    label: str
    genre: str
    pov: str
    pov_character: str
    cast: tuple[str, ...]
    target_words: int
    word_range: tuple[int, int]
    brief: str
    must_keep: tuple[Requirement, ...]
    forbidden: tuple[Requirement, ...]
    required_outcomes: tuple[Requirement, ...]
    mechanism_terms: tuple[str, ...]
    numbers_allowed: tuple[str, ...]
    ending_required_any: tuple[str, ...]
    ending_mode: str = "settled"

    @property
    def non_pov_cast(self) -> tuple[str, ...]:
        return tuple(name for name in self.cast if name != self.pov_character)

    def to_metadata(self) -> dict[str, Any]:
        """无正文的元信息,用于报告与哈希。"""
        return {
            "id": self.id,
            "label": self.label,
            "genre": self.genre,
            "pov": self.pov,
            "pov_character": self.pov_character,
            "cast": list(self.cast),
            "target_words": self.target_words,
            "word_range": list(self.word_range),
            "must_keep": [r.desc for r in self.must_keep],
            "forbidden": [r.desc for r in self.forbidden],
            "required_outcomes": [r.desc for r in self.required_outcomes],
        }


def _req(raw: Any, where: str) -> Requirement:
    if not isinstance(raw, dict):
        raise CaseSetError(f"{where}: requirement must be an object")
    rid = raw.get("id")
    desc = raw.get("desc")
    if not isinstance(rid, str) or not rid.strip():
        raise CaseSetError(f"{where}: requirement needs a non-empty id")
    if not isinstance(desc, str) or not desc.strip():
        raise CaseSetError(f"{where}/{rid}: requirement needs a non-empty desc")
    values: dict[str, tuple[str, ...]] = {}
    for key in _TOKEN_KEYS:
        value = raw.get(key, [])
        if not isinstance(value, list) or any(not isinstance(v, str) or not v for v in value):
            raise CaseSetError(f"{where}/{rid}: {key} must be a list of non-empty strings")
        values[key] = tuple(value)
    if not any(values.values()):
        raise CaseSetError(f"{where}/{rid}: requirement needs at least one token key")
    if values["pattern_any"]:
        # 正则先编译一次,写错就当场失败,而不是运行时静默当成"没命中"
        for pattern in values["pattern_any"]:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise CaseSetError(f"{where}/{rid}: bad regex {pattern!r}: {exc}") from None
    return Requirement(id=rid, desc=desc, **values)


def _str_tuple(raw: Any, where: str) -> tuple[str, ...]:
    if not isinstance(raw, list) or any(not isinstance(v, str) or not v for v in raw):
        raise CaseSetError(f"{where} must be a list of non-empty strings")
    return tuple(raw)


def _parse_case(raw: Any, index: int) -> Case:
    where = f"cases[{index}]"
    if not isinstance(raw, dict):
        raise CaseSetError(f"{where}: case must be an object")
    cid = raw.get("id")
    if not isinstance(cid, str) or not cid.strip():
        raise CaseSetError(f"{where}: case needs a non-empty id")
    where = f"case {cid}"

    def need_str(key: str) -> str:
        value = raw.get(key)
        if not isinstance(value, str) or not value.strip():
            raise CaseSetError(f"{where}: {key} must be a non-empty string")
        return value

    cast = _str_tuple(raw.get("cast"), f"{where}.cast")
    if len(cast) < 2:
        raise CaseSetError(f"{where}: cast needs at least two named characters")
    pov_character = need_str("pov_character")
    if pov_character not in cast:
        raise CaseSetError(f"{where}: pov_character must be listed in cast")

    word_range = raw.get("word_range")
    if (not isinstance(word_range, list) or len(word_range) != 2
            or any(type(v) is not int for v in word_range) or word_range[0] >= word_range[1]):
        raise CaseSetError(f"{where}: word_range must be two ascending integers")
    target_words = raw.get("target_words")
    if type(target_words) is not int or target_words <= 0:
        raise CaseSetError(f"{where}: target_words must be a positive integer")
    if not word_range[0] <= target_words <= word_range[1]:
        raise CaseSetError(f"{where}: target_words must lie inside word_range")

    groups: dict[str, tuple[Requirement, ...]] = {}
    for key in ("must_keep", "forbidden", "required_outcomes"):
        raw_group = raw.get(key)
        if not isinstance(raw_group, list) or not raw_group:
            raise CaseSetError(f"{where}: {key} must be a non-empty list")
        groups[key] = tuple(_req(item, f"{where}.{key}") for item in raw_group)

    return Case(
        id=cid,
        label=need_str("label"),
        genre=need_str("genre"),
        pov=need_str("pov"),
        pov_character=pov_character,
        cast=cast,
        target_words=target_words,
        word_range=(word_range[0], word_range[1]),
        brief=need_str("brief"),
        must_keep=groups["must_keep"],
        forbidden=groups["forbidden"],
        required_outcomes=groups["required_outcomes"],
        mechanism_terms=_str_tuple(raw.get("mechanism_terms", []), f"{where}.mechanism_terms"),
        numbers_allowed=_str_tuple(raw.get("numbers_allowed", []), f"{where}.numbers_allowed"),
        ending_required_any=_str_tuple(raw.get("ending_required_any", []), f"{where}.ending_required_any"),
        ending_mode=raw.get("ending_mode", "settled"),
    )


@dataclass(frozen=True)
class CaseSet:
    version: str
    cases: tuple[Case, ...]
    source: str = ""
    case_index: dict[str, Case] = field(default_factory=dict)

    def by_id(self, case_id: str) -> Case:
        try:
            return self.case_index[case_id]
        except KeyError:
            raise CaseSetError(f"unknown case id: {case_id}") from None


def load_case_set(path: str | Path) -> CaseSet:
    """读取样本集;结构不合法时抛 CaseSetError,不返回部分结果。"""
    p = Path(path)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CaseSetError(f"cannot read case set {p}: {exc}") from None
    if not isinstance(raw, dict):
        raise CaseSetError("case set must be a JSON object")
    version = raw.get("version")
    if not isinstance(version, str) or not version.strip():
        raise CaseSetError("case set needs a version string")
    raw_cases = raw.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise CaseSetError("case set needs a non-empty cases list")
    cases = tuple(_parse_case(item, i) for i, item in enumerate(raw_cases))
    ids = [c.id for c in cases]
    if len(set(ids)) != len(ids):
        raise CaseSetError("case ids must be unique")
    if len({c.genre for c in cases}) < 3:
        raise CaseSetError("sample set must cover at least three genres")
    per_genre: dict[str, int] = {}
    for c in cases:
        per_genre[c.genre] = per_genre.get(c.genre, 0) + 1
    thin = sorted(g for g, n in per_genre.items() if n < 2)
    if thin:
        raise CaseSetError(f"each genre needs at least two scenes; thin genres: {thin}")
    return CaseSet(version=version, cases=cases, source=str(p),
                   case_index={c.id: c for c in cases})
