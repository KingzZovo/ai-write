"""Chapter-level quality gate for mechanical prose traces.

This service re-evaluates generated chapter text after the main generator
finishes, optionally performs a targeted rewrite pass, and returns the best
available text together with safe diagnostics for API events / metadata.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import logging
import re
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services.chinese_prose_mechanics_checker import (
    ChineseProseMechanicsReport,
    analyze_chinese_prose_mechanics,
    output_scaffolding_line_indices,
)
from app.services.prompt_registry import run_text_prompt
from app.services.generation_context import generation_context_session

logger = logging.getLogger(__name__)

QualityGateStatus = Literal[
    "passed",
    "skipped",
    "improved_warning",
    "rewrite_failed",
    "blocked",
]

_MIN_LENGTH_RATIO = 0.70
# An automatic pass should not silently accept half of the requested scope.
# Short candidates are preserved for review, never padded to reach the floor.
def _resolve_min_target_ratio() -> float:
    """整章验收的篇幅下限(2026-11-17:从写死的 0.80 改为配置项单一来源)。

    与生成侧的 `SCENE_LENGTH_MIN_RATIO=0.95` 分工不同:那条线决定"单场写不够要不要
    补一次续写",这条线决定"整章交付能不能算通过"。章节是场景之和,所以验收线更低
    (0.80);两者各自只有一处来源,改口径只改 `CHAPTER_MIN_TARGET_WORD_RATIO`。
    """
    try:
        from app.config import settings

        value = float(getattr(settings, "CHAPTER_MIN_TARGET_WORD_RATIO", 0.80) or 0)
    except Exception:  # noqa: BLE001 - 配置读不到就按老口径
        return 0.80
    return max(0.0, min(1.0, value))


_MIN_TARGET_WORD_RATIO = _resolve_min_target_ratio()
# Rewrite-round budget comes from Settings.CHAPTER_MAX_REWRITE_ROUNDS, read at
# CALL time inside apply_chapter_quality_gate (audit 2026-07-26). The previous
# import-time os.getenv("CHAPTER_MAX_REWRITE_ROUNDS", "5") bypassed the config
# default of 2: a deployment without the env var silently got 5 rounds, and
# monkeypatched settings never took effect.
_DEFAULT_MAX_TOKENS = 4096

# PR-CH-TRUNCATION (2026-06-29): scene_writer sometimes hits its max_tokens
# ceiling; the OpenAI-compat stream ends mid-sentence and model_router's
# generate_stream never inspects finish_reason, so the partial scene is appended
# and the chapter is saved "completed" while ending on a dangling half-sentence.
# A finished Chinese chapter ends on sentence-terminal punctuation. Anything else
# on the final non-empty line means the tail was cut off.
_SENTENCE_TERMINALS = "。！？…”』」）】》—"
_TRAILING_FENCE_RE = re.compile(r"```[a-zA-Z]*\s*$")


def looks_truncated(text: str) -> bool:
    """Return True when the chapter body appears cut off mid-sentence.

    A complete Chinese chapter ends on terminal punctuation (。！？…) possibly
    followed by a closing quote/bracket. We strip trailing blank lines and a
    stray closing code fence, then check the last non-empty line's final char.
    Empty/blank text is NOT flagged here (that's a separate empty-output path).
    """
    if not text or not text.strip():
        return False
    cleaned = _TRAILING_FENCE_RE.sub("", text).strip()
    if not cleaned:
        return False
    last_char = cleaned[-1]
    return last_char not in _SENTENCE_TERMINALS


# PR-CH-REFUSAL (2026-07-04): relay occasionally misroutes a scene_writer text
# call to an image model, which replies with assistant/service refusal
# boilerplate (e.g. "我可以搜索图片，但目前似乎无法为您创建任何图片"). The
# orchestrator appends it as a scene; because it ends on 「。」 it slips past
# looks_truncated and the chapter is saved "completed" carrying a refusal
# instead of prose. These are DISTINCTIVE multi-word phrases that never occur
# in real narrative — single tokens like "图片" are deliberately excluded so
# legitimate prose ("她翻看着旧图片") is not flagged.
_REFUSAL_PHRASES = (
    "无法为您创建任何图片",
    "尚未开通图片创建功能",
    "无法为您生成图片",
    "我无法创建图片",
    "我不能生成图片",
)

# PR-REFUSAL-BROADEN (2026-07-26 audit): generic assistant refusal/meta openers
# ("I cannot ...", "抱歉，我不能...", "作为AI...") also reach this gate when a
# text route degrades to a plain chat refusal instead of prose. Unlike the exact
# image-misroute phrases above (matched over the whole text because that refusal
# arrives appended as a late scene), these generic markers are matched ONLY in
# the head of the text (first _REFUSAL_HEAD_WINDOW chars) and ONLY outside
# quoted dialogue: this function is applied to the full chapter body at persist
# time (chapter_persist_status, api/generate), so a whole-output refusal
# necessarily sits at the chapter opening, while a character saying 「我无法…」
# mid-story (or in opening dialogue) must never be flagged. 我无法 is bound to
# assistant-verb continuations so first-person narration (我无法忘记…) stays safe.
_REFUSAL_HEAD_WINDOW = 200
_GENERIC_REFUSAL_HEAD_RE = re.compile(
    r"I can(?:'|’)?t\b"
    r"|I cannot\b"
    r"|I(?:'|’)?m sorry,? but"
    r"|As an AI\b"
    r"|我无法(?:为您|为你|帮助|协助|提供|生成|创建|回答|处理|满足|继续)"
    r"|抱歉，我不能"
    r"|抱歉，我无法"
    r"|作为AI"
    r"|作为人工智能"
)
_QUOTE_OPEN_CHARS = "“‘「『"
_QUOTE_CLOSE_CHARS = "”’」』"


def _inside_quoted_dialogue(prefix: str) -> bool:
    """True when the position after ``prefix`` sits inside CJK quote pairs."""
    depth = 0
    for ch in prefix:
        if ch in _QUOTE_OPEN_CHARS:
            depth += 1
        elif ch in _QUOTE_CLOSE_CHARS:
            depth = max(0, depth - 1)
    return depth > 0


def looks_like_refusal(text: str) -> bool:
    """Return True when the text contains known assistant/service refusal
    boilerplate (typically an image-model misroute) rather than story prose.

    Two tiers: the distinctive image-misroute phrases are matched over the whole
    text (they can be appended as a late scene); generic refusal openers are
    matched only near the start of the text and outside quoted dialogue, so
    ordinary prose that merely mentions 图片/登录 — or a character saying
    「我无法帮助你」 deep in a chapter — is not flagged. Empty/blank text is NOT
    flagged.
    """
    if not text or not text.strip():
        return False
    if any(phrase in text for phrase in _REFUSAL_PHRASES):
        return True
    head = text.lstrip()[:_REFUSAL_HEAD_WINDOW]
    for match in _GENERIC_REFUSAL_HEAD_RE.finditer(head):
        if not _inside_quoted_dialogue(head[: match.start()]):
            return True
    return False

# PR-RELAY-ERROR-LEAK (2026-11-14): the OpenAI-compatible relay in front of the
# model gateway can answer HTTP 200 with an infrastructure diagnostic *as
# message content* — e.g. "⚠️ Upstream Gemini returned an empty response. ...
# Run `wrangler tail` ...". Any caller that trusts ``finish_reason == "stop"``
# would store it as finished prose. ``looks_truncated`` only stopped the observed
# sample by accident (its last character is an ASCII "."); the same diagnostic
# ending on a CJK terminal slips past every existing guard, and
# ``looks_like_refusal`` only knows assistant/image-model refusal boilerplate.
#
# Two tiers, because the markers differ in specificity:
#   * STRONG — a single hit is conclusive; these strings have no narrative use.
#   * WEAK  — infrastructure vocabulary; two independent hits are required.
_RELAY_ERROR_STRONG_RE = re.compile(
    r"(?:no (?:available )?channel for (?:this )?model"
    r"|no auth available"
    r"|invalid api key"
    r"|auth_unavailable"
    r"|model_not_found"
    r"|model_not_supported"
    r"|provider returned an error"
    r"|upstream (?:status|error|timeout) \d"
    r"|request id: "
    # 2026-11-16 实测(网关二进制 /root/migration-deploy/cliproxyapi/cli-proxy-api):
    # 除 Gemini 空响应外,中转还把自己或上游的错误码当 HTTP-200 正文透传。
    # 这些串在小说正文里没有叙事用途,单条命中即定性。
    r"|terminal_empty_incomplete"
    r"|incomplete empty response"
    r"|upstream execution failed"
    r"|quota_exhausted"
    r"|no available clients for any model"
    # 2026-11-17 实测:账号侧拒绝也会当正文返回,英文错误码是稳定的判据
    # (`account_needs_verification` / `permission_error`);中文前缀 `[错误]` 另在
    # provider_response_guard 按"开头"判定。
    r"|account_needs_verification"
    r"|\[错误\])",
    re.IGNORECASE,
)
_RELAY_ERROR_WEAK_MARKERS = (
    "returned an empty response",
    "wrangler tail",
    "GEMINI_BL",
    "egress IP",
    "Cloudflare/serverless",
    "worker returned",
    "Upstream ",
    # 上游把"输出预算全用来思考、没留答案"翻成自然语言的诊断(实测
    # deepseek-chat 在小 max_tokens 下整段正文就是这句话)。单一片段可能出现在
    # 英文文本里,故按 WEAK 处理:实测样本三句同现,必然命中。
    "entire output budget on reasoning",
    "simplify the request",
    "raise max_tokens",
)


def looks_like_relay_error(text: str) -> bool:
    """True when the payload is a gateway/relay diagnostic, not story prose."""
    if not text or not text.strip():
        return False
    if _RELAY_ERROR_STRONG_RE.search(text):
        return True
    lowered = text.lower()
    return sum(1 for marker in _RELAY_ERROR_WEAK_MARKERS if marker.lower() in lowered) >= 2


def chapter_persist_status(text: str, quality_meta: dict | None = None) -> str:
    """Decide the status a chapter save should carry (PR-BLOCKED-STATUS).

    ``draft`` when any of these hold, ``completed`` otherwise:

    - the text is empty or contains standalone output scaffolding, or
    - the text looks truncated (ends mid-sentence), or
    - the text contains refusal boilerplate (image-model misroute), or
    - the quality gate persisted it on block (``persisted_on_block`` in the
      save metadata) — persist-on-block keeps the best draft so nothing is
      lost, but a blocked chapter must never be silently marked completed.
    """
    if (not text or not text.strip() or looks_truncated(text) or looks_like_refusal(text)
            or looks_like_relay_error(text) or output_scaffolding_line_indices(text)):
        return "draft"
    if quality_meta and quality_meta.get("persisted_on_block"):
        return "draft"
    return "completed"


@dataclass
class ChapterQualityGateResult:
    status: QualityGateStatus
    original_text: str
    final_text: str
    initial_report: ChineseProseMechanicsReport
    final_report: ChineseProseMechanicsReport
    rewrite_rounds: int = 0
    rewrite_attempts: list[dict[str, Any]] = field(default_factory=list)
    warning_reason: str | None = None
    target_word_count: int | None = None
    min_target_word_count: int | None = None
    # 写后账目核对:本章写出的"总额"里,计划没写过的那些(见 numeric_check)。
    # 只是提示,不参与 status 判定,也不影响是否落库。
    numeric_conflicts: list[dict[str, Any]] = field(default_factory=list)
    # 本章写出的金额台账(按名目)。必须与 numeric_conflicts **一起**落进这份 quality_gate
    # 元数据:核对发生在新版本行创建之前,台账若不随这次落库走下去,本章首次生成就无处登记,
    # 后面的章节也就没有可比对象(实测过:只在已有版本行上补写会让整本书的首次遍历都不生效)。
    numeric_ledger: list[dict[str, Any]] = field(default_factory=list)

    def to_safe_metadata(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "rewrite_rounds": self.rewrite_rounds,
            "warning_reason": self.warning_reason,
            "target_word_count": self.target_word_count,
            "min_target_word_count": self.min_target_word_count,
            "numeric_conflicts": list(self.numeric_conflicts),
            "numeric_ledger": list(self.numeric_ledger),
            "initial_report": self.initial_report.to_safe_dict(),
            "final_report": self.final_report.to_safe_dict(),
            "rewrite_attempts": [
                {
                    "round": item.get("round"),
                    "accepted": item.get("accepted"),
                    "passed": item.get("passed"),
                    "candidate_len": item.get("candidate_len"),
                    "score": item.get("score"),
                    "reason": item.get("reason"),
                }
                for item in self.rewrite_attempts
            ],
        }

    @property
    def passed(self) -> bool:
        return self.status == "passed"


def _normalize_text(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"\*([^*]+)\*", r"\1", text)
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"^---+\s*$", "", text, flags=re.MULTILINE)
    text = re.sub(r"^>\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# Severity tiers for the bounded structural-repair rewrite (same rule as the
# AI-Novel-Writing-Assistant repair pass: a fix may never introduce a new
# high/critical issue). The mechanics report has no severity concept, so the
# tiers are declared here. CRITICAL = planner/story-bible material leaking into
# the reader-facing prose; HIGH = the narration breaking its own POV, logic or
# resource contract. Read via getattr so a future field rename degrades to 0
# instead of raising inside the gate.
CRITICAL_REPORT_FIELDS: tuple[str, ...] = (
    "story_bible_leakage_count",
    "meta_structure_leakage_count",
)
HIGH_REPORT_FIELDS: tuple[str, ...] = (
    "limited_pov_leak_count",
    "mundane_logic_violation_count",
    "resource_continuity_count",
    "action_causality_count",
)


def _quality_penalty(report: ChineseProseMechanicsReport) -> int:
    # No ranking by lexical frequency, sentence length or inferred POV/logic.
    return report.meta_structure_leakage_count


def _new_severe_findings(
    original_report: ChineseProseMechanicsReport,
    candidate_report: ChineseProseMechanicsReport,
) -> list[str]:
    """Severe defects the candidate ADDED relative to the original.

    Penalty ranking alone is unsafe: a candidate can win on the total score
    while newly leaking story-bible material or breaking POV. Strictly greater
    than (never >=), so an unchanged count is not a finding. Order is stable:
    critical fields first, then high, each in tuple order.
    """
    findings: list[str] = []
    for severity, names in (
        ("critical", CRITICAL_REPORT_FIELDS),
        ("high", HIGH_REPORT_FIELDS),
    ):
        for name in names:
            before = getattr(original_report, name, 0) or 0
            after = getattr(candidate_report, name, 0) or 0
            if after > before:
                findings.append(f"{severity}:{name}")
    return findings


def _candidate_is_acceptable(
    *,
    original_text: str,
    candidate_text: str,
    candidate_report: ChineseProseMechanicsReport,
    original_report: ChineseProseMechanicsReport,
    best_report: ChineseProseMechanicsReport,
    best_text: str,
    target_word_count: int | None = None,
    min_target_word_ratio: float = _MIN_TARGET_WORD_RATIO,
) -> tuple[bool, str | None]:
    if not candidate_text.strip():
        return False, "empty"

    if looks_like_refusal(candidate_text):
        return False, "refusal"
    if looks_truncated(candidate_text):
        return False, "truncated"

    original_len = len(original_text)
    candidate_len = len(candidate_text)
    min_len = max(1, int(original_len * _MIN_LENGTH_RATIO))
    if candidate_len < min_len:
        return False, f"too_short<{_MIN_LENGTH_RATIO:.2f}"
    if _target_length_shortfall(
        candidate_text,
        target_word_count=target_word_count,
        min_target_word_ratio=min_target_word_ratio,
    ):
        return False, "target_length_shortfall"

    def prose_lines(value: str) -> list[str]:
        scaffolding = output_scaffolding_line_indices(value)
        return [line.strip() for index, line in enumerate(value.splitlines())
                if line.strip() and index not in scaffolding]

    if prose_lines(candidate_text) != prose_lines(original_text):
        return False, "prose_changed"

    # A structural repair must not buy a better total score by introducing a
    # severe defect the original did not have; compare against the fixed
    # original report, never the rolling current one.
    new_severe = _new_severe_findings(original_report, candidate_report)
    if new_severe:
        return False, "new_severe:" + ",".join(new_severe)

    if candidate_report.passed:
        return True, None

    candidate_score = _quality_penalty(candidate_report)
    best_score = _quality_penalty(best_report)
    if candidate_score < best_score:
        return True, None
    return False, "no_improvement"


def _min_target_word_count(
    target_word_count: int | None,
    min_target_word_ratio: float = _MIN_TARGET_WORD_RATIO,
) -> int | None:
    if not target_word_count or int(target_word_count) <= 0:
        return None
    return max(1, int(int(target_word_count) * float(min_target_word_ratio)))


def _target_length_shortfall(
    text: str,
    *,
    target_word_count: int | None,
    min_target_word_ratio: float = _MIN_TARGET_WORD_RATIO,
) -> bool:
    minimum = _min_target_word_count(target_word_count, min_target_word_ratio)
    if minimum is None:
        return False
    return len((text or "").strip()) < minimum


def _build_rewrite_user_content(
    *,
    text: str,
    initial_report: ChineseProseMechanicsReport,
    round_idx: int,
    previous_attempts: list[dict[str, Any]],
) -> str:
    return (
        "请只去掉正文中混入的独立章节标题、编号或结束标签，输出完整正文。"
        "不要重写句子、删减情节、补情节、改人物关系或改变编号、数量、时间和物件去向。"
        "人物谈论书籍章节的正常叙述和对白应保留。"
        "停顿、长短句、对白比例、心理描写、重复动作及用词统计均不构成此次改稿理由。"
        "不要输出解释、分析、标题或Markdown。\n"
        f"处理轮次：{round_idx}；独立结构标签数：{initial_report.meta_structure_leakage_count}。\n"
        f"上一轮处理结果：{json.dumps([{k: a.get(k) for k in ('round', 'accepted', 'reason')} for a in previous_attempts], ensure_ascii=False)}\n"
        f"【待处理的完整正文】\n{text}"
    )


async def _resolve_project_genre_hint(db: AsyncSession | None, project_id: str | None) -> str | None:
    """Best-effort project genre lookup for the era/register-bleed detector.

    Combines Project.genre and Project.genre_profile_code into one hint string;
    any failure (missing project, stub db in tests) yields None, which keeps the
    era detector inactive (never guess the setting's era).
    """
    if not project_id:
        return None
    try:
        from app.models.project import Project

        async with generation_context_session(db) as read_db:
            project = await read_db.get(Project, project_id)
            if project is None:
                return None
            hint = " ".join(
                str(part)
                for part in (
                    getattr(project, "genre", None),
                    getattr(project, "genre_profile_code", None),
                )
                if part
            ).strip()
            return hint or None
    except Exception:
        return None


async def _apply_chapter_quality_gate_inner(
    *,
    text: str,
    db: AsyncSession | None,
    project_id: str | None = None,
    chapter_id: str | None = None,
    skip_polish: bool = False,
    max_rewrite_rounds: int | None = None,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
    target_word_count: int | None = None,
    min_target_word_ratio: float = _MIN_TARGET_WORD_RATIO,
    genre_hint: str | None = None,
) -> ChapterQualityGateResult:
    """Inspect output safety and structure, with bounded scaffold-only repair.

    The gate returns the best available candidate. It never raises for bad prose
    alone; callers decide whether to persist the returned text and emit warning
    events. ``genre_hint`` feeds the era/register-bleed detector; when omitted
    it is resolved from the project row so existing call sites get genre-aware
    reports without changes.
    """
    if max_rewrite_rounds is None:
        # Call-time read so config changes / monkeypatched settings win.
        max_rewrite_rounds = settings.CHAPTER_MAX_REWRITE_ROUNDS

    if genre_hint is None:
        genre_hint = await _resolve_project_genre_hint(db, project_id)

    original_text = text or ""
    initial_report = analyze_chinese_prose_mechanics(original_text, genre_hint=genre_hint)
    min_target = _min_target_word_count(target_word_count, min_target_word_ratio)

    # 2026-11-17 移除:原先对项目**所有**人物名逐个判断"本章没出现"并 logger.warning,
    # 一本 30 角色的书每章会产出几十条噪声,把真正的告警淹没;结果不进
    # ChapterQualityGateResult、不影响 status、也没有任何消费方,异常还被 `except: pass`
    # 完全吞掉。需要"大纲声明出场但正文缺席"的核对时,应当在大纲/场景合同层做。

    scaffold_indices = output_scaffolding_line_indices(original_text)
    invalid_reason = (
        "empty" if not original_text.strip() else
        "refusal" if looks_like_refusal(original_text) else
        "truncated" if looks_truncated("\n".join(
            line for index, line in enumerate(original_text.splitlines()) if index not in scaffold_indices
        )) else None
    )
    if invalid_reason:
        return ChapterQualityGateResult(
            status="blocked", original_text=original_text, final_text=original_text,
            initial_report=initial_report, final_report=initial_report,
            warning_reason=invalid_reason, target_word_count=target_word_count,
            min_target_word_count=min_target,
        )

    if skip_polish:
        return ChapterQualityGateResult(
            status="skipped",
            original_text=original_text,
            final_text=original_text,
            initial_report=initial_report,
            final_report=initial_report,
            target_word_count=target_word_count,
            min_target_word_count=min_target,
        )

    if initial_report.passed:
        if _target_length_shortfall(
            original_text,
            target_word_count=target_word_count,
            min_target_word_ratio=min_target_word_ratio,
        ):
            return ChapterQualityGateResult(
                status="blocked",
                original_text=original_text,
                final_text=original_text,
                initial_report=initial_report,
                final_report=initial_report,
                warning_reason="target_length_shortfall",
                target_word_count=target_word_count,
                min_target_word_count=min_target,
            )
        return ChapterQualityGateResult(
            status="passed",
            original_text=original_text,
            final_text=original_text,
            initial_report=initial_report,
            final_report=initial_report,
            target_word_count=target_word_count,
            min_target_word_count=min_target,
        )

    current_text = original_text
    current_report = initial_report
    best_text = original_text
    best_report = initial_report
    attempts: list[dict[str, Any]] = []
    warning_reason: str | None = None

    extra_system = "本次任务仅清理输出结构，保持小说原文；不要执行风格润色。"

    for round_idx in range(1, max(0, max_rewrite_rounds) + 1):
        user_content = _build_rewrite_user_content(
            text=current_text,
            initial_report=current_report,
            round_idx=round_idx,
            previous_attempts=attempts,
        )
        logger.info(
            "chapter_quality_gate rewrite start project_id=%s chapter_id=%s round=%d penalty=%d",
            project_id,
            chapter_id,
            round_idx,
            _quality_penalty(current_report),
        )
        result = await run_text_prompt(
            task_type="rewrite",
            user_content=user_content,
            db=db,
            extra_system=extra_system,
            project_id=project_id,
            chapter_id=chapter_id,
            max_tokens=max_tokens,
        )
        candidate_text = _normalize_text(result.text or "")
        candidate_report = analyze_chinese_prose_mechanics(candidate_text, genre_hint=genre_hint)
        new_severe = _new_severe_findings(initial_report, candidate_report)
        accepted, reason = _candidate_is_acceptable(
            original_text=original_text,
            candidate_text=candidate_text,
            candidate_report=candidate_report,
            original_report=initial_report,
            best_report=best_report,
            best_text=best_text,
            target_word_count=target_word_count,
            min_target_word_ratio=min_target_word_ratio,
        )
        attempt_record = {
            "round": round_idx,
            "candidate_len": len(candidate_text),
            "passed": candidate_report.passed,
            "score": _quality_penalty(candidate_report),
            "accepted": accepted,
            "reason": reason,
            "new_severe": new_severe,
            "report": candidate_report.to_safe_dict(),
        }
        attempts.append(attempt_record)

        if accepted:
            best_text = candidate_text
            best_report = candidate_report
            current_text = candidate_text
            current_report = candidate_report
            if candidate_report.passed:
                return ChapterQualityGateResult(
                    status="passed",
                    original_text=original_text,
                    final_text=best_text,
                    initial_report=initial_report,
                    final_report=best_report,
                    rewrite_rounds=round_idx,
                    rewrite_attempts=attempts,
                    target_word_count=target_word_count,
                    min_target_word_count=min_target,
                )
            warning_reason = "improved_but_not_passed"
        else:
            warning_reason = reason or "rejected"

    return ChapterQualityGateResult(
        status="blocked",
        original_text=original_text,
        final_text=best_text,
        initial_report=initial_report,
        final_report=best_report,
        rewrite_rounds=len(attempts),
        rewrite_attempts=attempts,
        warning_reason=warning_reason or "quality_gate_blocked",
        target_word_count=target_word_count,
        min_target_word_count=min_target,
    )


async def apply_chapter_quality_gate(
    *,
    text: str,
    db: AsyncSession | None,
    project_id: str | None = None,
    chapter_id: str | None = None,
    skip_polish: bool = False,
    max_rewrite_rounds: int | None = None,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
    target_word_count: int | None = None,
    min_target_word_ratio: float = _MIN_TARGET_WORD_RATIO,
    genre_hint: str | None = None,
) -> ChapterQualityGateResult:
    """质量门 + 写后账目核对。

    在这里挂钩做账目核对,是为了让三个落库路径(桌面流式 / 后台任务 / 管线)一次都覆盖到:
    三处都经过本函数,然后才把 ``to_safe_metadata()`` 写进章节元数据。

    核对**严格非阻断**:失败只记日志,不改 ``status``,不改正文,不抛。
    """
    result = await _apply_chapter_quality_gate_inner(
        text=text, db=db, project_id=project_id, chapter_id=chapter_id,
        skip_polish=skip_polish, max_rewrite_rounds=max_rewrite_rounds,
        max_tokens=max_tokens, target_word_count=target_word_count,
        min_target_word_ratio=min_target_word_ratio, genre_hint=genre_hint,
    )
    try:
        from app.services.numeric_check import attach_numeric_conflicts

        await attach_numeric_conflicts(
            result, db=db, project_id=project_id, chapter_id=chapter_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("amount check skipped: %s", exc)
    return result
