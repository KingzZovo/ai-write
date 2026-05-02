"""Scene-staged chapter writing orchestrator (v1.5.0 C1).

Replaces the single-shot "generation" prompt with a two-stage pipeline:

1. ``scene_planner`` (standard tier, structured JSON):
   chapter outline + context pack -> list of 3-6 SceneBrief.
2. ``scene_writer`` (flagship tier, streaming):
   per-scene, takes the SceneBrief + a rolling summary of already-written
   scenes -> 800-1200 char prose.

The orchestrator joins per-scene streams into one continuous chunk stream
so the existing SSE infrastructure (api/generate.py event_stream + auto-save)
works unchanged. A ``\n\n`` separator is yielded between scenes.

Design notes
------------
- Scene boundaries (titles / metadata) are NOT yielded as text. The caller
  can subscribe to the on-scene-start hook by passing ``on_scene_start``.
- ``plan_scenes`` is robust to imperfect JSON output: it tries strict JSON
  parse first, then ```json fenced parse, then a fallback heuristic that
  builds N=ceil(target_words/1000) generic SceneBriefs from the chapter
  outline so the chapter never silently fails to scene-mode produce text.
- Rolling "prior scenes summary" is intentionally cheap: we keep the last
  ~600 chars of each completed scene plus its title/key_action so writer
  has continuity context without exploding the prompt.
"""
from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass, field
from typing import AsyncIterator, Awaitable, Callable, Optional
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.context_pack import ContextPack, ContextPackBuilder
from app.services.prompt_registry import run_text_prompt, stream_text_prompt

logger = logging.getLogger(__name__)


DEFAULT_TARGET_WORDS = 3500
MIN_SCENE_WORDS = 800
MAX_SCENE_WORDS = 1200


@dataclass
class SceneBrief:
    idx: int
    title: str
    brief: str
    pov: str = ""
    location: str = ""
    time_cue: str = ""
    key_action: str = ""
    target_words: int = 1000
    hook: str = ""

    @classmethod
    def from_dict(cls, idx: int, raw: dict) -> "SceneBrief":
        def _s(key: str, default: str = "") -> str:
            v = raw.get(key)
            return str(v).strip() if v is not None else default

        target_words = raw.get("target_words")
        try:
            target_words = int(target_words) if target_words is not None else 1000
        except (TypeError, ValueError):
            target_words = 1000
        target_words = max(MIN_SCENE_WORDS, min(MAX_SCENE_WORDS, target_words))

        # idx may be supplied by the model; we override with the canonical
        # 1-based positional idx so concatenation order is deterministic.
        return cls(
            idx=idx,
            title=_s("title") or f"场景 {idx}",
            brief=_s("brief"),
            pov=_s("pov"),
            location=_s("location"),
            time_cue=_s("time_cue"),
            key_action=_s("key_action"),
            target_words=target_words,
            hook=_s("hook"),
        )

    def to_writer_user_content(self) -> str:
        bullets: list[str] = []
        bullets.append(f"【场景号】第 {self.idx} 场")
        if self.title:
            bullets.append(f"【标题】{self.title}")
        if self.pov:
            bullets.append(f"【视角】{self.pov}")
        if self.location:
            bullets.append(f"【地点】{self.location}")
        if self.time_cue:
            bullets.append(f"【时间】{self.time_cue}")
        if self.key_action:
            bullets.append(f"【主要动作】{self.key_action}")
        bullets.append(f"【目标字数】约 {self.target_words} 字 (800-1200)")
        if self.hook:
            bullets.append(f"【场末过渡】{self.hook}")
        else:
            bullets.append("【场末过渡】（本场为末场，需送入本章钩子，但不干预下一章）")
        if self.brief:
            bullets.append("【场景 brief】")
            bullets.append(self.brief)
        return "\n".join(bullets)


# ---------------------------------------------------------------------------
# Planner: chapter outline -> List[SceneBrief]
# ---------------------------------------------------------------------------


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_ARRAY_RE = re.compile(r"\[\s*\{.*\}\s*\]", re.DOTALL)


def _try_parse_scene_array(raw: str) -> list[dict] | None:
    """Best-effort parse of an LLM response into a list[dict] of scene briefs.

    Strategies, in order:
      1. strict json.loads on the whole string
      2. extract first ```json ... ``` fenced block
      3. extract first [...] array regex match
    Returns None if all strategies fail.
    """
    if not raw:
        return None
    candidates: list[str] = [raw.strip()]
    fence_match = _FENCE_RE.search(raw)
    if fence_match:
        candidates.append(fence_match.group(1).strip())
    array_match = _ARRAY_RE.search(raw)
    if array_match:
        candidates.append(array_match.group(0).strip())

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
            return parsed
        if isinstance(parsed, dict) and isinstance(parsed.get("scenes"), list):
            return parsed["scenes"]
    return None


def _fallback_scene_briefs(target_words: int, chapter_outline_text: str) -> list[SceneBrief]:
    """Build deterministic scene briefs when the planner LLM fails to JSON.

    We pick N = round(target_words / 1000), clamp to [3, 6], then split the
    chapter outline text into N roughly equal slices to seed the briefs so
    the writer still has *some* structural anchor per scene.
    """
    n = max(3, min(6, round(max(target_words, MIN_SCENE_WORDS) / 1000)))
    text = (chapter_outline_text or "").strip()
    if text:
        chunk_size = max(1, len(text) // n)
        slices = [text[i * chunk_size : (i + 1) * chunk_size] for i in range(n)]
        slices[-1] = text[(n - 1) * chunk_size :]  # tail gets remainder
    else:
        slices = [""] * n
    per_scene = max(MIN_SCENE_WORDS, min(MAX_SCENE_WORDS, target_words // n))
    briefs: list[SceneBrief] = []
    for i, sl in enumerate(slices, start=1):
        briefs.append(
            SceneBrief(
                idx=i,
                title=f"场景 {i}",
                brief=sl[:200],
                pov="",
                location="",
                time_cue="",
                key_action="",
                target_words=per_scene,
                hook="" if i == n else "接下一场",
            )
        )
    return briefs


class SceneOrchestrator:
    """Two-stage chapter writer: planner -> per-scene streaming writer."""

    def __init__(self, *, default_target_words: int = DEFAULT_TARGET_WORDS) -> None:
        self.default_target_words = default_target_words

    # ----- planner ---------------------------------------------------------

    async def plan_scenes(
        self,
        *,
        pack: ContextPack,
        db: AsyncSession,
        project_id: str | UUID,
        chapter_id: Optional[str | UUID],
        target_words: int,
        n_scenes_hint: Optional[int] = None,
        user_instruction: str = "",
    ) -> list[SceneBrief]:
        """Run scene_planner LLM call and return SceneBrief list (>=3, <=6)."""
        # Re-use ContextPack's system prompt as planner background, then
        # inject the planner-specific user instruction.
        background = pack.to_system_prompt()
        chapter_outline_text = self._extract_chapter_outline_text(pack)
        hint_line = (
            f"推荐场景数 = {n_scenes_hint}\n"
            if isinstance(n_scenes_hint, int) and 3 <= n_scenes_hint <= 6
            else ""
        )
        instr_block = (
            f"【额外用户指令（改写要求）】\n{user_instruction.strip()}\n\n"
            if user_instruction and user_instruction.strip()
            else ""
        )
        user_content = (
            f"{instr_block}"
            f"本章目标字数：约 {target_words} 字\n"
            f"{hint_line}"
            f"请按系统提示输出严格 JSON。"
        )
        try:
            result = await run_text_prompt(
                task_type="scene_planner",
                user_content=user_content,
                db=db,
                extra_system=background,
                project_id=str(project_id),
                chapter_id=str(chapter_id) if chapter_id else None,
                rag_hits=[],
            )
            raw_text = getattr(result, "text", "") or ""
        except Exception as exc:  # broad: planner is best-effort, never block writer
            logger.warning("scene_planner LLM call failed: %s", exc)
            raw_text = ""

        parsed = _try_parse_scene_array(raw_text)
        if not parsed:
            logger.warning(
                "scene_planner returned unparseable output (len=%d); using fallback",
                len(raw_text),
            )
            return _fallback_scene_briefs(target_words, chapter_outline_text)

        briefs: list[SceneBrief] = []
        for i, raw in enumerate(parsed[:6], start=1):
            if not isinstance(raw, dict):
                continue
            briefs.append(SceneBrief.from_dict(i, raw))
        if len(briefs) < 3:
            logger.warning(
                "scene_planner returned %d briefs (<3); using fallback instead",
                len(briefs),
            )
            return _fallback_scene_briefs(target_words, chapter_outline_text)
        return briefs

    @staticmethod
    def _extract_chapter_outline_text(pack: ContextPack) -> str:
        """Best-effort: pull chapter outline / volume context from the pack."""
        # ContextPack stringifies its own to_system_prompt(); we just take a
        # bounded slice as the fallback seed text.
        try:
            sys_prompt = pack.to_system_prompt()
        except Exception:
            return ""
        return sys_prompt[-2000:] if sys_prompt else ""

    # ----- writer ----------------------------------------------------------

    async def write_scene_stream(
        self,
        *,
        scene: SceneBrief,
        pack: ContextPack,
        prior_scenes_summary: str,
        db: AsyncSession,
        project_id: str | UUID,
        chapter_id: Optional[str | UUID],
        user_instruction: str = "",
    ) -> AsyncIterator[str]:
        """Stream prose for a single scene through scene_writer."""
        background = pack.to_system_prompt()
        ctx_block = scene.to_writer_user_content()
        prior_block = (
            f"\n\n【已写场景凝缩】\n{prior_scenes_summary}"
            if prior_scenes_summary
            else "\n\n【已写场景】本场为本章首场，请从本章开场起手。"
        )
        instr_block = (
            f"\n\n【额外用户指令（改写要求）】\n{user_instruction.strip()}"
            if user_instruction and user_instruction.strip()
            else ""
        )
        user_content = ctx_block + prior_block + instr_block + "\n\n请开始写本场景。"
        async for chunk in stream_text_prompt(
            task_type="scene_writer",
            user_content=user_content,
            db=db,
            extra_system=background,
            project_id=str(project_id),
            chapter_id=str(chapter_id) if chapter_id else None,
            rag_hits=[],
        ):
            yield chunk

    # ----- end-to-end ------------------------------------------------------

    async def orchestrate_chapter_stream(
        self,
        *,
        project_id: str | UUID,
        volume_id: str | UUID,
        chapter_idx: int,
        db: AsyncSession,
        chapter_id: Optional[str | UUID] = None,
        user_instruction: str = "",
        target_words: Optional[int] = None,
        n_scenes_hint: Optional[int] = None,
        on_scene_start: Optional[Callable[[SceneBrief], Awaitable[None]]] = None,
    ) -> AsyncIterator[str]:
        """Build pack, plan scenes, then stream all scenes back-to-back.

        ``on_scene_start`` (if given) is awaited just before each scene's
        first chunk is emitted, with the SceneBrief as argument.
        """
        pack = await ContextPackBuilder(db=db).build(
            project_id=project_id,
            volume_id=volume_id,
            chapter_idx=chapter_idx,
        )
        twords = target_words or self.default_target_words
        briefs = await self.plan_scenes(
            pack=pack,
            db=db,
            project_id=project_id,
            chapter_id=chapter_id,
            target_words=twords,
            n_scenes_hint=n_scenes_hint,
            user_instruction=user_instruction,
        )
        prior_summary_parts: list[str] = []
        for i, scene in enumerate(briefs):
            if on_scene_start is not None:
                try:
                    await on_scene_start(scene)
                except Exception as cb_err:
                    logger.warning("on_scene_start callback failed: %s", cb_err)
            scene_text_parts: list[str] = []
            if i > 0:
                # visible separator between scenes (kept minimal)
                yield "\n\n"
            async for chunk in self.write_scene_stream(
                scene=scene,
                pack=pack,
                prior_scenes_summary="\n\n".join(prior_summary_parts),
                db=db,
                project_id=project_id,
                chapter_id=chapter_id,
                user_instruction=user_instruction,
            ):
                if chunk:
                    scene_text_parts.append(chunk)
                yield chunk
            full_scene_text = "".join(scene_text_parts)
            prior_summary_parts.append(self._summarize_scene(scene, full_scene_text))

    @staticmethod
    def _summarize_scene(scene: SceneBrief, scene_text: str) -> str:
        """Cheap rolling summary: title + key_action + last 600 chars of prose."""
        tail = (scene_text or "").strip()
        if len(tail) > 600:
            tail = tail[-600:]
        head_line = f"[场 {scene.idx} | {scene.title}]"
        if scene.key_action:
            head_line += f" 主要动作：{scene.key_action}"
        if not tail:
            return head_line
        return f"{head_line}\n末段：{tail}"
