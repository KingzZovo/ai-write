"""Authoritative fact contract for long-form generation.

Vector retrieval and character cards are useful context, but they are not a
hard source of truth. This module builds a small canonical contract from the
book outline/entity registry and character table, injects it into prompts, and
validates generated text before it is marked usable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project import Character, Outline
from app.services.outline_consistency_gate import extract_role_names


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

    def to_prompt(self) -> str:
        lines = [
            "以下是本项目唯一事实契约，优先级高于大纲节选、向量检索、角色卡与临时续写指令。",
            "不得把同一角色改名、换名、同位体化；若上下文出现冲突，以本契约为准。",
        ]
        if self.role_names:
            lines.append("核心角色槽位：")
            for role, name in self.role_names.items():
                label = ROLE_LABELS.get(role, role)
                lines.append(f"- {label}: {name}")
        if self.registered_character_names:
            names = "、".join(self.registered_character_names[:80])
            lines.append(f"已登记角色名: {names}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "role_names": dict(self.role_names),
            "registered_character_names": list(self.registered_character_names),
            "source": self.source,
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
    role_names: dict[str, str] = {}
    if isinstance(payload, dict):
        registry = payload.get("entity_registry")
        if isinstance(registry, dict):
            for role, keys in ROLE_REGISTRY_KEYS.items():
                for key in keys:
                    _add_role_name(role_names, role, registry.get(key))
        raw_text = payload.get("raw_text") or payload.get("summary") or ""
        if isinstance(raw_text, str):
            extracted = extract_role_names(raw_text, payload)
            for role, names in extracted.items():
                if len(names) == 1:
                    _add_role_name(role_names, role, names[0])
    return FactContract(role_names=role_names, source="outline_payload")


def _profile_role(profile: dict[str, Any]) -> str:
    values: list[str] = []
    for key in ("role", "type", "identity", "身份", "定位"):
        value = profile.get(key)
        if isinstance(value, str):
            values.append(value)
    text = " ".join(values).lower()
    for role, hints in PROFILE_ROLE_HINTS.items():
        if any(hint.lower() in text for hint in hints):
            return role
    return ""


async def build_fact_contract(db: AsyncSession, project_id: str) -> FactContract:
    role_names: dict[str, str] = {}
    registered_names: list[str] = []

    outline_result = await db.execute(
        select(Outline.content_json)
        .where(Outline.project_id == str(project_id), Outline.level == "book")
        .order_by(Outline.version.desc())
        .limit(1)
    )
    outline_payload = outline_result.scalar_one_or_none()
    outline_contract = contract_from_outline_payload(outline_payload)
    role_names.update(outline_contract.role_names)

    char_result = await db.execute(
        select(Character).where(Character.project_id == str(project_id)).order_by(Character.name.asc())
    )
    for char in char_result.scalars().all():
        name = (char.name or "").strip()
        if name and name not in registered_names:
            registered_names.append(name)
        profile = char.profile_json if isinstance(char.profile_json, dict) else {}
        role = _profile_role(profile)
        if role:
            _add_role_name(role_names, role, name)

    return FactContract(
        role_names=role_names,
        registered_character_names=registered_names,
        source="book_outline+characters",
    )


def validate_text_against_fact_contract(text: str, contract: FactContract) -> FactContractReport:
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
                        f"{label}槽位与事实契约冲突：应为{canonical}，生成中出现{','.join(wrong)}",
                        details={"role": role, "canonical": canonical, "observed": names},
                    )
                )
        elif len(names) > 1:
            issues.append(
                FactContractIssue(
                    "role_name_conflict",
                    f"同一角色槽位出现多个姓名：{role}={','.join(names)}",
                    details={"role": role, "observed": names},
                )
            )
    return FactContractReport(
        ok=not any(issue.severity == "error" for issue in issues),
        issues=issues,
        facts={"contract": contract.to_dict(), "observed_role_names": observed},
    )


def assert_text_matches_fact_contract(text: str, contract: FactContract) -> FactContractReport:
    report = validate_text_against_fact_contract(text, contract)
    if not report.ok:
        raise FactContractError(report)
    return report
