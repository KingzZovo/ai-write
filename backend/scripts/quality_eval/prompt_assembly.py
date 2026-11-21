"""按生产写作链真实使用的 prompt 模块拼装输入。

这里刻意不自己写一套写作提示,而是直接 import 生产模块,这样对
``narrative_contract`` / ``narrative_quality_gates`` / ``prompts.scene_writing``
的任何改动都会立刻反映到验收结果里;反过来,验收结论也只对这些真实模块成立。

三个变体:

- ``scene_writer``:场景写作链(scene_orchestrator.write_scene_stream)的输入结构。
  系统提示 = SCENE_WRITER_SYSTEM;用户内容 = 场景要求 + 角色名册 + WRITER_CONTRACT_PROMPT
  + 台账说明 + 首场说明。
- ``chapter_single_shot``:整章单发链(chapter_generator)的输入结构。
  系统提示 = CHAPTER_WRITER_SYSTEM;首条 system = preflight_scene_blueprint_prompt。
- ``bare``:只给系统提示"你是中文小说作者"的对照基线,用于区分"契约到底有没有用"。

``snapshot`` 参数用于重放历史契约:块文本按名字替换,于是"改动前/改动后"可以在同一
模型、同一温度、同一采样次数下对照。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

VARIANTS = ("scene_writer", "chapter_single_shot", "bare")

_TASK_TYPES = {
    "scene_writer": "scene_writer",
    "chapter_single_shot": "generation",
    "bare": "generation",
}


@dataclass(frozen=True)
class PromptBundle:
    variant: str
    system: str
    user: str
    task_type: str
    blocks: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    block_texts: dict[str, str] = field(default_factory=dict)

    @property
    def sha256(self) -> str:
        payload = self.system + "\x00" + self.user
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def to_metadata(self) -> dict[str, Any]:
        return {
            "variant": self.variant,
            "task_type": self.task_type,
            "prompt_sha256": self.sha256,
            "system_chars": len(self.system),
            "user_chars": len(self.user),
            "blocks": list(self.blocks),
        }


def _block(name: str, source: str, text: str) -> dict[str, Any]:
    return {"name": name, "source": source, "chars": len(text),
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]}


def _render(case: Any, variant: str) -> PromptBundle:
    if variant not in VARIANTS:
        raise ValueError(f"unknown prompt variant: {variant}")

    from app.services import narrative_contract as nc
    from app.services import narrative_quality_gates as nqg
    from app.services.prompts import scene_writing

    if variant == "scene_writer":
        system = scene_writing.SCENE_WRITER_SYSTEM
        roster = "、".join(case.cast)
        roster_block = (
            f"\n\n【角色名册】本次上下文角色姓名(并非都需出场):{roster}\n"
            "可依角色档案或作者明确要求使用别名与称谓;结合场景关系区分重名,不得擅自改名或混同人物。"
        )
        ledger_block = (
            "\n\n【规划与正文】\n"
            "上面的场景要求尚待正文落实,辅助安排本场进展,不能覆盖作者明确要求或已写事实。"
            "按前文实际状态接写,写出本场应有的变化;合理省略按共享写作合同处理。"
        )
        user = (
            case.brief
            + roster_block
            + "\n\n" + nc.WRITER_CONTRACT_PROMPT
            + ledger_block
            + "\n\n【已写场景】本场为本章首场,请从本章开场起手。"
            + "\n\n请开始写本场景。"
        )
        texts = {
            "scene_brief": case.brief,
            "character_roster": roster_block,
            "writer_contract": nc.WRITER_CONTRACT_PROMPT,
            "planning_ledger_note": ledger_block,
            "scene_writer_system": system,
        }
        return PromptBundle(
            variant, system, user, _TASK_TYPES[variant],
            tuple(_block(name, src, texts[name]) for name, src in (
                ("scene_brief", "case.brief"),
                ("character_roster", "scene_orchestrator.write_scene_stream"),
                ("writer_contract", "narrative_contract.WRITER_CONTRACT_PROMPT"),
                ("planning_ledger_note", "scene_orchestrator.write_scene_stream"),
                ("scene_writer_system", "prompts.scene_writing.SCENE_WRITER_SYSTEM"),
            )),
            texts,
        )

    if variant == "chapter_single_shot":
        system = scene_writing.CHAPTER_WRITER_SYSTEM
        blueprint = nqg.preflight_scene_blueprint_prompt(chapter_idx=1)
        user = blueprint + "\n\n" + case.brief
        texts = {
            "preflight_blueprint": blueprint,
            "scene_brief": case.brief,
            "chapter_writer_system": system,
        }
        return PromptBundle(
            variant, system, user, _TASK_TYPES[variant],
            tuple(_block(name, src, texts[name]) for name, src in (
                ("preflight_blueprint", "narrative_quality_gates.preflight_scene_blueprint_prompt"),
                ("scene_brief", "case.brief"),
                ("chapter_writer_system", "prompts.scene_writing.CHAPTER_WRITER_SYSTEM"),
            )),
            texts,
        )

    system = "你是中文小说作者。只输出小说正文。"
    user = case.brief
    texts = {"scene_brief": case.brief, "bare_system": system}
    return PromptBundle(
        variant, system, user, _TASK_TYPES[variant],
        tuple(_block(name, src, texts[name]) for name, src in (
            ("scene_brief", "case.brief"),
            ("bare_system", "inline"),
        )),
        texts,
    )


def build_prompt(case: Any, variant: str = "scene_writer",
                 snapshot: dict[str, Any] | None = None) -> PromptBundle:
    """构造该变体的提示词;给出 ``snapshot`` 时用快照里的契约块替换当前渲染结果。"""
    bundle = _render(case, variant)
    if not snapshot:
        return bundle
    overrides = (snapshot.get("blocks") or {}).get(variant) or {}
    if not overrides:
        return bundle
    user = bundle.user
    blocks = []
    texts = dict(bundle.block_texts)
    for block in bundle.blocks:
        name = block["name"]
        replacement = overrides.get(name)
        if replacement is None:
            blocks.append(block)
            continue
        current = bundle.block_texts.get(name, "")
        if current and current in user:
            user = user.replace(current, replacement)
        texts[name] = replacement
        blocks.append(_block(name, f"snapshot:{snapshot.get('rev', '?')}", replacement))
    return PromptBundle(bundle.variant, bundle.system, user, bundle.task_type, tuple(blocks), texts)


def production_contract_snapshot() -> dict[str, str]:
    """把本次验收所依赖的生产契约版本记进产物,便于回溯。"""
    from app.services import narrative_contract as nc
    from app.services import narrative_quality_gates as nqg

    from app.services.prompts import scene_writing

    return {
        "narrative_contract_version": nc.NARRATIVE_CONTRACT_VERSION,
        "blueprint_version": nqg.BLUEPRINT_VERSION,
        "writer_contract_sha256": hashlib.sha256(
            nc.WRITER_CONTRACT_PROMPT.encode("utf-8")).hexdigest()[:16],
        "scene_writer_system_sha256": hashlib.sha256(
            scene_writing.SCENE_WRITER_SYSTEM.encode("utf-8")).hexdigest()[:16],
    }