"""Prose mechanics rules must have exactly one source of truth in the blueprint pipeline.

Guards against the historical hotfix drift in narrative_quality_gates.py:
- the Chinese prose mechanics rule catalog duplicated inline inside the chapter
  blueprint (section 八) with diverging wording,
- a content-less duplicate 「八、」 section heading,
- four different version literals (v4.13 / v4.12 / v4.9 / v4.8) in one module.
"""
from __future__ import annotations

import inspect
import re

import app.services.narrative_quality_gates as nqg
from app.services.narrative_quality_gates import (
    CHINESE_PROSE_MECHANICS_PROMPT,
    contract_hard_gate_prompt,
    preflight_scene_blueprint_prompt,
)

HEADING_RE = re.compile(r"^[零一二三四五六七八九十]+、")


def _blueprint() -> str:
    return preflight_scene_blueprint_prompt(chapter_idx=1)


def test_prose_mechanics_single_occurrence_in_blueprint() -> None:
    blueprint = _blueprint()

    # The blueprint must reuse the canonical constant verbatim instead of
    # carrying a second hand-maintained copy of the rules.
    assert CHINESE_PROSE_MECHANICS_PROMPT.strip() in blueprint

    # Signature rule phrases must appear exactly once (no double definition,
    # neither a stale inline copy nor a stray duplicate in another section).
    for phrase in (
        "提词器",  # communication_damping
        "粗鄙直接比俏皮更真实",  # plain_register_no_wit
        "坐标测绘",  # focal_measure_only
        "口诀式对仗",  # dialogue_symmetry_break (canonical wording)
        "全知式摆设盘点",  # 视角流动 (canonical wording)
        "视角与动作联动",  # rule present only in the canonical catalog
    ):
        assert blueprint.count(phrase) == 1, phrase

    # Both prompt surfaces are fed from the same constant.
    assert CHINESE_PROSE_MECHANICS_PROMPT.strip() in contract_hard_gate_prompt()


def test_no_duplicate_section_numbering() -> None:
    blueprint = _blueprint()
    lines = blueprint.splitlines()

    headings = [line for line in lines if HEADING_RE.match(line)]
    ordinals = [h.split("、", 1)[0] for h in headings]
    assert len(ordinals) == len(set(ordinals)), f"duplicate section ordinals: {ordinals}"

    # No empty-shell section: the next non-empty line after a heading must not
    # be another section heading.
    for idx, line in enumerate(lines):
        if not HEADING_RE.match(line):
            continue
        next_nonempty = next((l for l in lines[idx + 1 :] if l.strip()), "")
        assert not HEADING_RE.match(next_nonempty), f"empty-shell section: {line}"


def test_version_single_source() -> None:
    # One module-level version constant...
    assert nqg.BLUEPRINT_VERSION == "v4.13"

    # ...and no other version literal anywhere in the module source.
    source = inspect.getsource(nqg)
    assert set(re.findall(r"v4\.\d+", source)) == {nqg.BLUEPRINT_VERSION}

    # Rendered prompts only ever reference that single version.
    blueprint = _blueprint()
    assert set(re.findall(r"v4\.\d+", blueprint)) == {nqg.BLUEPRINT_VERSION}
    assert f"direct_generation_first_{nqg.BLUEPRINT_VERSION}" in blueprint


def test_calibration_prompts_single_source() -> None:
    """C1: the over-revision calibration prompts live only in narrative_contract;
    chapter_evaluator and auto_revise must import (not re-define) them, so the
    wording has one source of truth."""
    import inspect

    import app.services.narrative_contract as nc
    import app.services.chapter_evaluator as ce
    import app.services.auto_revise as ar

    assert nc.EVALUATOR_CALIBRATION_PROMPT.strip()
    assert nc.REVISE_CALIBRATION_PROMPT.strip()

    # The literal block headers must not be redefined in the consumer modules.
    ce_src = inspect.getsource(ce)
    ar_src = inspect.getsource(ar)
    assert "EVALUATOR_CALIBRATION_PROMPT =" not in ce_src
    assert "REVISE_CALIBRATION_PROMPT =" not in ar_src
    # Consumers reference the imported names.
    assert "EVALUATOR_CALIBRATION_PROMPT" in ce_src
    assert "REVISE_CALIBRATION_PROMPT" in ar_src
