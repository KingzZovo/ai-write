# Defect Governance: class immunity, not instance patches

Convention (2026-07): every defect found by a reader or the evaluator in generated
prose is classified into a defect CLASS before any fix lands. An instance fix
(blacklisting one phrase, patching one chapter) without class immunization is
not accepted. Each class gets, where applicable:

1. **Contract rule (prompt)** — a generation-before bullet in
   `narrative_quality_gates.CHINESE_PROSE_MECHANICS_PROMPT` and
   `narrative_contract.WRITER_CONTRACT_PROMPT`, so the first draft avoids it.
2. **Deterministic detector** — where the class is mechanically checkable, a
   field in `chinese_prose_mechanics_checker.analyze_chinese_prose_mechanics`.
   Detectors are either **BLOCK** (flip `passed`, trigger the gate's rewrite
   loop) or **WARN** (feed `_quality_penalty` + rewrite/preflight instructions;
   never block alone — used when false positives are plausible).
3. **Automatic recurrence surfacing** — the recurring-defect ledger: before
   each generation, the preflight builder aggregates the latest scored
   `chapter_evaluations.issues_json` of the project's last
   `PREFLIGHT_RECURRING_DEFECT_CHAPTERS` (5) chapters. Any violation tag seen in
   `PREFLIGHT_RECURRING_DEFECT_MIN_CHAPTERS` (2) or more of them becomes a
   【本项目高发问题】 block carrying the tag's `repair_action` from
   `QUALITY_GATE_RULES` plus one recent example (≤80 chars). Deterministic, no
   LLM, no human intervention.

The loop: detector fires → penalty steers the gate rewrite → persisted
evaluation issues feed the ledger → next chapter's preflight pre-empts the
class. Genre-gated detectors (era bleed) stay inactive when the genre is
unknown or ambiguous — never guess.

## Currently immunized classes

| Class | Detector field(s) | Severity | Notes |
|---|---|---|---|
| Meta/structure leakage (第X章/[CH-n]) | `meta_structure_leakage_count` | BLOCK | |
| Story-bible leakage (设定名朗读) | `story_bible_leakage_count` | BLOCK | |
| Micro-action overload (推眼镜×3…) | `micro_action_overload`, `micro_action_density_per_1000` | WARN | budget 3/1000 chars |
| Rare-phrase repetition (verbal tics) | `repeated_rare_phrase_count`, `repeated_rare_phrases` | WARN | 4–6 char n-gram ×3+ |
| Translationese (英语骨架直译) | `translationese_marker_count` | BLOCK | |
| Pseudo-literary compression (伪文学压缩腔) | `pseudo_literary_register_count`, `plain_contemporary_violation_count` | BLOCK | |
| Duplicate explanation / padding | `duplicate_explanation_span_count`, `repeated_realization_run` | BLOCK | |
| Era/register bleed (genre anachronism) | `era_register_class`, `era_register_conflict_count`, `era_register_conflicts` | WARN | genre-gated: `MODERN_SETTING_FLAGS` (两炷香/时辰/三更天/N丈…) in modern genres, `PERIOD_SETTING_FLAGS` (分钟/公里/手机…) in period genres; unknown genre → inactive |
| Scene-seam duplication (接缝复述) | `seam_duplication_count`, `seam_duplication_pairs` | WARN | narration sentences ≥10 chars, char-bigram Jaccard ≥0.6 within ~800 chars; quoted speech masked out |
| Assistant refusal in prose | `chapter_quality_gate.looks_like_refusal` | persist as `draft` | |
| Mid-sentence truncation | `chapter_quality_gate.looks_truncated` | persist as `draft` | |
| World-logic violations (time/space/info/…) | evaluator tags → `QUALITY_GATE_RULES` | evaluator + ledger | not mechanically checkable; immunized via contract + recurrence ledger |

## Adding a new class

- Name the class, not the instance ("era bleed", not "两炷香").
- Add a contract bullet in both prompt files, in each file's voice.
- If mechanically checkable, add a detector field + `to_safe_dict` entry +
  `_quality_penalty` weight + rewrite bullet + preflight line + tests
  (positive fixture, negative fixture, false-positive guard).
- Prefer WARN for anything with plausible false positives; BLOCK only for
  patterns that never belong in prose.
- The ledger picks up evaluator-tagged recurrences automatically; nothing to
  wire per class.
