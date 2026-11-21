# Mechanism Fixes Status (2026-07-11)

Completed by Hermes after Claude session stalled on root privilege restriction.

## Fix1
- DONE previously: non-truncatable character name roster in context_pack + scene_orchestrator

## Fix2
- DONE: generation-path labels 第X章/卷 replaced with [CH-n]/[VOL-n] in:
  - context_pack.py / narrative_quality_gates.py (Claude)
  - memory.py, character_roster.py, arc_loop.py, related_chapters.py
  - foreshadow_manager.py, foreshadow_lifecycle.py
  - cascade_regenerator.py, compass_service.py, constory_checker.py, memory_compactor.py
- Remaining 第X章 strings in export/outline title defaults are UI/export labels, not prose prompt context.

## Fix3
- DONE: meta_structure_leakage_zero added to CHINESE_PROSE_MECHANICS_PROMPT
- DONE: rewrite guidance in chapter_quality_gate

## Fix4
- DONE: meta_structure_leakage_count in ChineseProseMechanicsReport
- DONE: regex counter for 第X章 / [CH-n] / 本章完 etc.
- DONE: fails quality gate + rewrite penalty
- VERIFY: good meta=0 passed=True; bad meta=3 passed=False; all touched files py_compile OK
