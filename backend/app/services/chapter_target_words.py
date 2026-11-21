"""每章字数 / 每卷章数的统一读取入口。

项目设置里这两项各有两套历史键名,只认一套会让用户设的值被静默忽略:

  每章字数:``target_chapter_words``(规范,前端 ProjectSettingsModal 写)
            / ``chapter_words``(简写,探针与脚本写)
  每卷章数:``chapters_per_volume_min`` / ``_max`` / ``_target``(规范三键)
            / ``chapters_per_volume``(简写,单个"每卷多少章"的真值)

夹取语义(两套键不同,不要互相套用):
  - 规范三键沿用既有 ``[1, LEGACY_CHAPTERS_PER_VOLUME_LIMIT]`` 夹取 —— 旧语义里它们是
    "小范围"的每卷章数(如 3-4 章/卷);``resolve_chapters_per_volume_pref`` 保持不变。
  - 简写键 ``chapters_per_volume`` 是每卷章数的真值(如 100),**不夹取**。
  - 但被夹取过的规范值不再当作卷纲硬约束:``resolve_volume_chapter_range`` 对超出旧窗口的
    规范值按真值返回,否则卷纲会把 100 章/卷压成 50 章/卷,与 ``build_planning_scale`` 的
    每卷章数互相矛盾(实测:确认过的书纲 5 卷 × 100 章,卷纲却只产出 50 章 × 4000 字,
    因为每章预算 = 卷目标字数 ÷ 50)。
"""

from __future__ import annotations

from typing import Any

CHAPTER_DEFAULT_WORD_COUNT = 4000
LEGACY_CHAPTER_DEFAULT_WORD_COUNT = 50_000

DEFAULT_CHAPTERS_PER_VOLUME_MIN = 100
DEFAULT_CHAPTERS_PER_VOLUME_MAX = 200
DEFAULT_CHAPTERS_PER_VOLUME_TARGET = 150

# 规范三键的历史夹取上限(旧语义的小范围每卷章数)。
LEGACY_CHAPTERS_PER_VOLUME_LIMIT = 50
# 单卷章数硬上限,与 outline_generator._MAX_VOLUME_CHAPTERS 一致。
MAX_VOLUME_CHAPTERS = 200

_CHAPTER_WORDS_KEYS = ("target_chapter_words", "chapter_words")
_CANONICAL_CHAPTERS_PER_VOLUME_KEYS = (
    "chapters_per_volume_min",
    "chapters_per_volume_max",
    "chapters_per_volume_target",
)
_SHARED_CHAPTERS_PER_VOLUME_KEY = "chapters_per_volume"


def _settings_dict(settings_json: Any) -> dict:
    return settings_json if isinstance(settings_json, dict) else {}


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def is_legacy_chapter_target_word_count(value: Any) -> bool:
    return _positive_int(value) == LEGACY_CHAPTER_DEFAULT_WORD_COUNT


def resolve_chapter_target_word_count(
    chapter_target_word_count: Any,
    project_target_chapter_words: Any = None,
    *,
    fallback: int = CHAPTER_DEFAULT_WORD_COUNT,
) -> int:
    """Return the effective target used for generation and UI display.

    50_000 was the previous DB default, not a deliberate user preference in
    most rows. Treat that exact value as legacy when resolving targets so old
    rows inherit the project/default chapter size instead of forcing filler.
    """
    chapter_target = _positive_int(chapter_target_word_count)
    if chapter_target and chapter_target != LEGACY_CHAPTER_DEFAULT_WORD_COUNT:
        return chapter_target

    project_target = _positive_int(project_target_chapter_words)
    if project_target:
        return project_target

    fallback_target = _positive_int(fallback)
    return fallback_target or CHAPTER_DEFAULT_WORD_COUNT


def resolve_chapter_words(
    settings_json: Any,
    *,
    fallback: int = CHAPTER_DEFAULT_WORD_COUNT,
) -> int:
    """每章字数:``target_chapter_words``(规范)→ ``chapter_words``(简写)→ 默认 4000。

    规范键写的是历史上的 50_000 缺省值时按没写处理(与 ``resolve_chapter_target_word_count``
    同一口径),以免一行旧数据盖掉用户后来写的简写键。
    """
    settings = _settings_dict(settings_json)
    return resolve_chapter_target_word_count(
        settings.get(_CHAPTER_WORDS_KEYS[0]),
        settings.get(_CHAPTER_WORDS_KEYS[1]),
        fallback=fallback,
    )


def resolve_chapters_per_volume_pref(settings_json: dict | None) -> tuple[int, int] | None:
    """Resolve an explicit chapters-per-volume (lo, hi) range from settings_json.

    Values from the canonical three keys are clamped to [1, 50]; non-numeric /
    non-positive values are ignored. The shorthand ``chapters_per_volume`` key
    is *not* clamped (it carries the real per-volume chapter count, e.g. 100)
    and only fills canonical keys that are absent. Returns ``None`` when no key
    carries a usable value — callers must then keep the legacy behaviour
    (plan-driven counts, 10-chapter fallback).
    """
    settings = _settings_dict(settings_json)
    if not settings:
        return None

    def _clamped(key: str) -> int | None:
        value = _positive_int(settings.get(key))
        if value is None:
            return None
        return min(value, LEGACY_CHAPTERS_PER_VOLUME_LIMIT)

    lo = _clamped(_CANONICAL_CHAPTERS_PER_VOLUME_KEYS[0])
    hi = _clamped(_CANONICAL_CHAPTERS_PER_VOLUME_KEYS[1])
    tgt = _clamped(_CANONICAL_CHAPTERS_PER_VOLUME_KEYS[2])
    shared = _positive_int(settings.get(_SHARED_CHAPTERS_PER_VOLUME_KEY))
    if shared is not None:
        lo = lo if lo is not None else shared
        hi = hi if hi is not None else shared
        tgt = tgt if tgt is not None else shared
    candidates = [v for v in (lo, hi, tgt) if v is not None]
    if not candidates:
        return None
    lo = lo if lo is not None else min(candidates)
    hi = hi if hi is not None else max(candidates)
    if hi < lo:
        lo, hi = hi, lo
    return lo, hi


def _raw_canonical_chapter_counts(settings_json: Any) -> list[int]:
    settings = _settings_dict(settings_json)
    values = [
        value
        for value in (
            _positive_int(settings.get(key))
            for key in _CANONICAL_CHAPTERS_PER_VOLUME_KEYS
        )
        if value is not None
    ]
    return values


def resolve_volume_chapter_range(settings_json: Any) -> tuple[int, int] | None:
    """卷纲阶段的每卷章数硬约束区间(settings 的真值优先于旧的夹取窗口)。

    ``resolve_chapters_per_volume_pref`` 的 [1, 50] 夹取保留给"旧三键的小范围语义";
    但当规范键写了超出该窗口的值(如前端/用户写的"每卷 100 章")时,夹取会把 100 压成 50
    —— 卷纲于是硬约束成 50 章/卷,把已确认书纲的 100 章/卷静默减半,每章预算翻倍。
    这种越窗值按真值使用(上限 ``MAX_VOLUME_CHAPTERS``);窗口内的值与简写键行为不变。
    """
    pref = resolve_chapters_per_volume_pref(settings_json)
    if pref is None:
        return None
    raw = _raw_canonical_chapter_counts(settings_json)
    if not raw or max(raw) <= LEGACY_CHAPTERS_PER_VOLUME_LIMIT:
        return pref
    lo, hi = min(raw), max(raw)
    return min(lo, MAX_VOLUME_CHAPTERS), min(hi, MAX_VOLUME_CHAPTERS)


def resolve_chapters_per_volume_scale(
    settings_json: Any,
    *,
    default_min: int = DEFAULT_CHAPTERS_PER_VOLUME_MIN,
    default_max: int = DEFAULT_CHAPTERS_PER_VOLUME_MAX,
    default_target: int = DEFAULT_CHAPTERS_PER_VOLUME_TARGET,
) -> tuple[int, int, int]:
    """``compute_scale`` 的每卷章数入参 (min, max, target)。

    规范三键(**不夹取**,这里算的是规模而非卷纲硬约束)→ 简写键 ``chapters_per_volume``
    → 默认 100 / 200 / 150。简写键同时填 min / max / target 三个槽位,使"每卷 100 章"
    精确落成 100 章/卷而不是一个区间。
    """

    settings = _settings_dict(settings_json)
    shared = _positive_int(settings.get(_SHARED_CHAPTERS_PER_VOLUME_KEY))

    def _resolve(key: str, default: int) -> int:
        value = _positive_int(settings.get(key))
        if value is not None:
            return value
        return shared if shared is not None else default

    return (
        _resolve(_CANONICAL_CHAPTERS_PER_VOLUME_KEYS[0], default_min),
        _resolve(_CANONICAL_CHAPTERS_PER_VOLUME_KEYS[1], default_max),
        _resolve(_CANONICAL_CHAPTERS_PER_VOLUME_KEYS[2], default_target),
    )


def build_planning_scale(target_word_count: Any, settings_json: Any) -> dict | None:
    """一次读全(每章字数 + 每卷章数)再算规模,避免各调用点各读一个键。

    ``None`` 表示没有可用目标字数(或全部设置都读不到),调用方退回自由发挥。
    """
    from app.services.outline_generator import compute_scale

    chapter_words = resolve_chapter_words(settings_json)
    cmin, cmax, ctgt = resolve_chapters_per_volume_scale(settings_json)
    return compute_scale(
        target_word_count,
        chapter_words=chapter_words,
        chapters_per_volume_min=cmin,
        chapters_per_volume_max=cmax,
        chapters_per_volume_target=ctgt,
    )
