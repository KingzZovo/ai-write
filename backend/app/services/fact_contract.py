"""Current identity inputs and lexical observations for long-form generation.

Vector retrieval and character cards are useful context, but they are not a
hard source of truth. Author text owns its projection over older structure.
Shared role labels and different names alone cannot establish that two mentions
refer to the same person, or certify the literary quality of a manuscript.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project import Character, Outline
from app.services.outline_authority import TEXT_REVISION, outline_for_generation
from app.services.outline_consistency_gate import extract_role_names
from app.services.outline_order import REF, UNAVAILABLE
from app.services.outline_readiness import has_meaningful_outline_content, is_degraded_outline


ROLE_LABELS: dict[str, str] = {
    "protagonist": "主角",
    "mentor": "导师/救援者",
    "father": "父亲",
    "mother": "母亲",
}

ROLE_REGISTRY_KEYS: dict[str, tuple[str, ...]] = {
    "protagonist": ("protagonist", "main_character", "主角"),
    "mentor": ("mentor", "rescuer", "导师", "救援者"),
    "father": ("father", "父亲"),
    "mother": ("mother", "母亲"),
}

PROFILE_ROLE_HINTS: dict[str, tuple[str, ...]] = {
    "protagonist": ("protagonist", "main_character", "主角", "男主", "女主"),
    "mentor": ("mentor", "rescuer", "导师", "救援者", "外勤导师"),
    "father": ("father", "父亲"),
    "mother": ("mother", "母亲"),
}


@dataclass(slots=True)
class FactContract:
    role_names: dict[str, str] = field(default_factory=dict)
    registered_character_names: list[str] = field(default_factory=list)
    source: str = ""
    outline_role_names: dict[str, list[str]] = field(default_factory=dict)
    role_name_sources: dict[str, str] = field(default_factory=dict)
    outline_source: dict[str, Any] | None = None

    def to_prompt(self) -> str:
        lines = [
            "以下记录角色姓名和核心身份，不能代替前文正文、角色卡中的位置、能力、关系与当前状态。",
            "这些身份记录的优先级高于无来源的向量检索改名推测；不得擅自把同一人物换成另一人。",
            "作者本轮明确要求修改姓名、关系或设定时，按作者要求处理，不得用旧记录覆盖本轮任务。",
            "同类身份可以属于不同人物；这里的导师、父亲等记录不表示全书只有一位导师或一组亲属关系。",
            "只识别明确的姓名称谓声明，不能穷尽人物；未识别出姓名不表示作者没有写到这个人。",
        ]
        if self.role_names:
            lines.append("按来源识别的姓名与称谓（不表示关系唯一）：")
            for role, name in self.role_names.items():
                label = ROLE_LABELS.get(role, role)
                origin = {"author_text": "当前作者文字", "saved_outline": "当前确认书纲",
                          "character_profile": "登记档案标签，需结合正文核对"}.get(self.role_name_sources.get(role))
                lines.append(f"- {label}: {name}" + (f"（{origin}）" if origin else ""))
        for role, names in self.outline_role_names.items():
            if len(names) > 1:
                lines.append(f"- {ROLE_LABELS.get(role, role)}的多个人名记录: {'、'.join(names)}；须按上下文分别核对，不能合并为同一人物。")
        if self.registered_character_names:
            names = "、".join(self.registered_character_names)
            lines.append("登记姓名不能证明当前角色身份，可能包含旧提取；不能用旧档案槽位恢复作者已经撤掉的安排。")
            lines.append(f"已登记角色名: {names}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "role_names": dict(self.role_names),
            "registered_character_names": list(self.registered_character_names),
            "source": self.source,
            "outline_role_names": deepcopy(self.outline_role_names),
            "role_name_sources": dict(self.role_name_sources),
            "outline_source": deepcopy(self.outline_source),
        }


@dataclass(slots=True)
class FactContractIssue:
    code: str
    message: str
    severity: str = "error"
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "severity": self.severity,
            "details": self.details,
        }


@dataclass(slots=True)
class FactContractReport:
    ok: bool
    issues: list[FactContractIssue] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "issues": [issue.to_dict() for issue in self.issues],
            "facts": self.facts,
        }


class FactContractError(RuntimeError):
    def __init__(self, report: FactContractReport):
        self.report = report
        message = "; ".join(issue.message for issue in report.issues) or "fact contract failed"
        super().__init__(message)


class FactContractSourceError(ValueError):
    """A saved current outline cannot be replaced with stale identity inputs."""


def _coerce_name(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("name", "姓名", "value"):
            name = _coerce_name(value.get(key))
            if name:
                return name
    return ""


def _add_role_name(role_names: dict[str, str], role: str, name: str) -> None:
    cleaned = _coerce_name(name)
    if not cleaned:
        return
    role_names.setdefault(role, cleaned)


def contract_from_outline_payload(payload: Any) -> FactContract:
    try:
        if (not isinstance(payload, dict) or not payload
                or payload.get("parse_error") or payload.get("_parse_error")
                or is_degraded_outline(payload)
                or "available" in payload and payload["available"] is not True
                or payload.get(UNAVAILABLE) or REF in payload and not payload[REF]
                or "raw_text" in payload and not isinstance(payload["raw_text"], str)):
            raise ValueError("invalid current outline")
        author_text = bool(payload.get(TEXT_REVISION))
        if author_text and not payload.get("raw_text", "").strip():
            raise ValueError("empty author revision")
        # Apply this before extraction: passing the original payload to the
        # extractor would silently reinstate the very registry being suppressed.
        projected = outline_for_generation(payload)
        if not isinstance(projected, dict) or not has_meaningful_outline_content({
                key: value for key, value in projected.items() if key not in {"title", "available", "parse_error"}}):
            raise ValueError("unavailable current outline")
        digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        registry = projected.get("entity_registry", {})
        if not isinstance(registry, dict):
            raise ValueError("invalid current registry")
        raw_text = projected.get("raw_text", "")
        if not raw_text.strip():
            raw_text = projected.get("summary", "")
        if not isinstance(raw_text, str):
            raise ValueError("invalid current text")
    except (ValueError, TypeError):
        raise FactContractSourceError("当前确认书纲无法作为人物身份材料，请核对当前修订，不能回退旧书纲或旧人物槽位。") from None

    candidates = {role: set(names) for role, names in extract_role_names(raw_text, projected).items()}
    for role, keys in ROLE_REGISTRY_KEYS.items():
        for key in keys:
            name = _coerce_name(registry.get(key))
            if name:
                candidates.setdefault(role, set()).add(name)
    role_names = {role: next(iter(names)) for role, names in candidates.items() if len(names) == 1}
    authority = "author_text" if author_text else "saved_outline"
    return FactContract(role_names=role_names, source="outline_payload",
        outline_role_names={role: sorted(names) for role, names in candidates.items()},
        role_name_sources={role: authority for role in role_names},
        outline_source={"authority": authority, "content_hash": digest})


def _profile_role(profile: dict[str, Any]) -> str:
    values: list[str] = []
    for key in ("role", "type", "identity", "身份", "定位"):
        value = profile.get(key)
        if isinstance(value, str):
            values.append(value)
    labels = {value.strip().casefold() for value in values if value.strip()}
    roles = {role for role, hints in PROFILE_ROLE_HINTS.items()
             if labels.intersection(hint.casefold() for hint in hints)}
    # Narrative descriptions ("不是主角", "主角的父亲") are not slot labels.
    # Multiple explicit labels need relation-aware interpretation, not priority
    # based on the order of this dictionary.
    return next(iter(roles)) if len(roles) == 1 else ""


async def build_fact_contract(db: AsyncSession, project_id: str) -> FactContract:
    registered_names: list[str] = []

    outline_result = await db.execute(
        select(Outline.id, Outline.version, Outline.content_json)
        .where(Outline.project_id == str(project_id), Outline.level == "book", Outline.is_confirmed == 1)
        .order_by(Outline.version.desc(), Outline.created_at.desc(), Outline.id.desc())
        .limit(1)
        .execution_options(autoflush=False)
    )
    # A row containing JSON/SQL null is a damaged current source, not the
    # absence of a confirmed outline. Never fall through to an older identity.
    outline_row = outline_result.one_or_none()
    contract = contract_from_outline_payload(outline_row.content_json) if outline_row is not None else FactContract()
    if outline_row is not None:
        contract.outline_source.update(id=str(outline_row.id), version=outline_row.version)

    char_result = await db.execute(
        select(Character.name, Character.profile_json)
        .where(Character.project_id == str(project_id)).order_by(Character.name.asc())
        .execution_options(autoflush=False)
    )
    profile_candidates: dict[str, set[str]] = {}
    for char in char_result.all():
        name = (char.name or "").strip()
        if name and name not in registered_names:
            registered_names.append(name)
        profile = char.profile_json if isinstance(char.profile_json, dict) else {}
        role = _profile_role(profile)
        if role and name:
            profile_candidates.setdefault(role, set()).add(name)
    # A plain-text replacement need not carry TEXT_REVISION. Absence of a
    # role in the current confirmed outline is not permission to reconstruct
    # it from profile labels whose provenance has not been checked here.
    if outline_row is None:
        for role, names in profile_candidates.items():
            if len(names) == 1 and role not in contract.outline_role_names:
                _add_role_name(contract.role_names, role, next(iter(names)))
                contract.role_name_sources[role] = "character_profile"

    contract.registered_character_names = registered_names
    contract.source = "confirmed_book_outline+characters" if outline_row is not None else "characters"
    return contract


def validate_text_against_fact_contract(text: str, contract: FactContract) -> FactContractReport:
    """Record lexical differences; ``ok`` is not a contextual or literary verdict."""
    issues: list[FactContractIssue] = []
    observed = extract_role_names(text or "")
    for role, names in observed.items():
        canonical = contract.role_names.get(role)
        if canonical:
            wrong = [name for name in names if name != canonical]
            if wrong:
                label = ROLE_LABELS.get(role, role)
                issues.append(
                    FactContractIssue(
                        "role_name_drift",
                        f"{label}称谓下出现不同姓名：记录为{canonical}，本稿出现{','.join(wrong)}；需结合上下文核对是否同一人物。",
                        # Keep the legacy report code, but do not infer an
                        # identity conflict from a shared role label alone.
                        severity="warning",
                        details={"role": role, "canonical": canonical, "observed": names,
                                 "identity_established": False},
                    )
                )
        elif len(names) > 1:
            issues.append(
                FactContractIssue(
                    "role_identity_ambiguous",
                    f"同类身份出现多个姓名，尚不能据此认定为同一人物：{role}={','.join(names)}",
                    severity="warning",
                    details={"role": role, "observed": names, "identity_established": False},
                )
            )
    return FactContractReport(
        ok=not any(issue.severity == "error" for issue in issues),
        issues=issues,
        facts={"contract": contract.to_dict(), "observed_role_names": observed,
               "scope": "explicit_role_declarations_only",
               "text_hash": hashlib.sha256((text or "").encode()).hexdigest(),
               "editorial_status": "needs_author_review"},
    )


def assert_text_matches_fact_contract(text: str, contract: FactContract) -> FactContractReport:
    report = validate_text_against_fact_contract(text, contract)
    if not report.ok:
        raise FactContractError(report)
    return report
