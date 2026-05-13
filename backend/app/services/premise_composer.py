"""PR-C-PREMISE-STRUCTURED (2026-05-13): deterministic composer.

Transforms a structured premise dict (see ``schemas.project.PremiseStructured``)
into:

* ``premise``   — human-readable narrative paragraph (back-compat with the
                old free-form column and all downstream consumers).
* ``core_seed`` — the 1-2 sentence essence injected into every prompt as the
                primary anti-homogenization anchor.

No LLM call: composition is pure-Python so it is cheap, idempotent, and
testable. Future iterations may add an optional LLM-refinement pass that
stores the result in ``core_seed`` directly while leaving this deterministic
fallback in place.

Design notes:
* All fields are optional; the composer silently skips empty ones. A premise
  with only ``protagonist`` still produces a usable seed.
* ``anti_patterns`` are deliberately surfaced in BOTH outputs — the model
  needs the negative space to avoid template drift (lct2 motivation).
* The output is stable: identical input always yields identical strings.
"""
from __future__ import annotations

from typing import Any


def _clean(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _clean_list(values: Any) -> list[str]:
    if not values:
        return []
    if isinstance(values, str):
        values = [values]
    out: list[str] = []
    for v in values:
        s = _clean(v)
        if s:
            out.append(s)
    return out


def compose_premise(structured: dict[str, Any] | None) -> tuple[str | None, str | None]:
    """Return ``(premise_paragraph, core_seed)``.

    Both may be ``None`` when the structured input is empty.
    """
    if not structured:
        return None, None

    protagonist = _clean(structured.get("protagonist"))
    central_conflict = _clean(structured.get("central_conflict"))
    world_premise = _clean(structured.get("world_premise"))
    theme = _clean(structured.get("theme"))
    tone = _clean(structured.get("tone"))
    unique_hooks = _clean_list(structured.get("unique_hooks"))
    anti_patterns = _clean_list(structured.get("anti_patterns"))

    # ----- core_seed: 1-2 sentence distillation --------------------------
    seed_parts: list[str] = []
    if protagonist and central_conflict:
        seed_parts.append(f"{protagonist}——{central_conflict}")
    elif protagonist:
        seed_parts.append(protagonist)
    elif central_conflict:
        seed_parts.append(central_conflict)
    if theme:
        seed_parts.append(f"主题：{theme}")
    if tone:
        seed_parts.append(f"基调：{tone}")
    if unique_hooks:
        seed_parts.append("独有交资：" + "、".join(unique_hooks[:3]))
    if anti_patterns:
        seed_parts.append("明令避免：" + "、".join(anti_patterns[:3]))
    core_seed = "。".join(p for p in seed_parts if p)
    if core_seed and not core_seed.endswith("。"):
        core_seed += "。"
    core_seed = core_seed or None

    # ----- premise: full narrative paragraph ------------------------------
    paragraph_lines: list[str] = []
    if protagonist:
        paragraph_lines.append(f"主角：{protagonist}。")
    if world_premise:
        paragraph_lines.append(f"世界设定：{world_premise}。")
    if central_conflict:
        paragraph_lines.append(f"核心冲突：{central_conflict}。")
    if theme:
        paragraph_lines.append(f"主题：{theme}。")
    if tone:
        paragraph_lines.append(f"语调：{tone}。")
    if unique_hooks:
        paragraph_lines.append("独特设定/钩子：" + "、".join(unique_hooks) + "。")
    if anti_patterns:
        paragraph_lines.append("明令避免的套路：" + "、".join(anti_patterns) + "。")
    premise = "\n".join(paragraph_lines) if paragraph_lines else None

    return premise, core_seed


def merge_premise_fields(
    structured: dict[str, Any] | None,
    user_premise: str | None,
    user_core_seed: str | None,
) -> tuple[str | None, str | None, dict[str, Any] | None]:
    """Apply precedence rules when persisting a project.

    Returns ``(premise, core_seed, premise_structured)`` ready to set on the
    ORM instance.

    Rules (back-compat first):
    * If ``structured`` is provided, the composer fills any field the user did
      not explicitly override. User-supplied ``premise``/``core_seed`` always win.
    * If ``structured`` is absent, fall back to legacy single-field behavior.
    """
    composed_premise, composed_seed = compose_premise(structured)
    final_premise = user_premise if user_premise else composed_premise
    final_seed = user_core_seed if user_core_seed else composed_seed
    return final_premise, final_seed, structured
