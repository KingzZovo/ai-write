"""
Hierarchical Outline Generator

Generates outlines at three levels:
1. Book-level: Overall plot arc, core characters, world-building, estimated scale
2. Volume-level: Per-volume conflicts, turning points, new/departing characters
3. Chapter-level: Per-chapter plot points, characters, emotional arc, transitions
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import AsyncIterator

from app.services.model_router import get_model_router

logger = logging.getLogger(__name__)

BOOK_OUTLINE_SYSTEM = """你是小说策划师。直接输出大纲内容，不要说任何多余的话。

禁止输出以下内容：
- 任何开场白、自我介绍、"好的"、"下面给出"、"如果你愿意"、"希望对你有帮助"之类的废话
- 任何Markdown格式（不要用#、**、-、>、```、---）
- 任何对用户说的话，你不是在对话，你是在输出一份文档

大纲必须包含以下九个部分（用空行和段落标题分隔）：

一、书名与核心概念
简短有力的书名，一句话概括故事内核。

二、主要角色与小传（至少5人）
每个角色单独一段，信息必须齐全：
- 基本信息：姓名、年龄、性别、外貌（一两句抓特征）、身份标签
- 出身与成长：家世、童年环境、关键童年事件、求学/师承
- 性格与动机：内核性格、表面伪装、所渴望之物、所恐惧之物、最不能碰的底线
- 关键创伤：塑造他的一次具体事件（必须有时间、地点、人物、冲击）
- 言语风格：1-2句代表性对白，口吻独特
- 核心关系：与另外两三个主要角色的羁绊

三、主角能力成长表
用一张清晰的表列出主角的能力变化，每卷一行，字段包括：
- 卷数
- 修为/境界/关键能力
- 关键道具/武器/功法
- 触发升级的事件
- 副作用或代价
表格形式：卷数 | 境界能力 | 道具 | 触发事件 | 代价

四、角色关系网
明确写出主要角色之间的关系：谁和谁是敌人、盟友、师徒、恋人、竞争者。用"A → B：关系描述"的格式。

五、势力格局
列出3-5个主要势力/阵营，每个势力的核心利益、代表人物、与主角的关系。

六、世界观设定集
系统罗列，每个子项单独一段，内容必须具体不可空泛：
- 地理：大陆/国家/城邦/特殊地貌，给出主要名称和位置关系
- 历史：前朝/大战/神话断代，标记至少两个对当下有持续影响的历史事件
- 种族与势力：人族/妖族/异族/教派，各自特征与彼此的宿怨
- 宗教与神话：信仰体系、主神/异神、禁忌
- 政治与经济：权力结构、货币、贸易路线、阶级流动性
- 文化与日常：服饰、饮食、节庆、婚丧、礼仪
- 力量体系：境界或魔法分级、获取方式、能做到的事、代价与反噬、禁忌
- 特殊物品：重要法宝/神器/秘术，来源和传说

七、分卷规划
每卷写出核心冲突、关键转折、主角状态变化。

八、核心伏笔
3-5条贯穿全书的伏笔线，标明埋设时机和预期消解条件。

九、基调与类型标签
用 3-5 个短标签表达整体叙事基调（如"冷峻悬疑""贵族权谋""灾异意象"），再用一句话说明要避开哪些常见套路。

文字风格：句子长短不一，段落开头各不相同，像一个老编辑在给新人讲故事方案，口语化但专业。"""

# v1.4.2 — staged book-outline prompts. The full BOOK_OUTLINE_SYSTEM asks a
# model to emit all nine sections in one response, which routinely exceeds 10k
# Chinese chars (~15-18k tokens) and hits the long-output quality cliff. We
# split the 9 sections across 3 calls, each staying under ~4k tokens so every
# response sits in the model's safe output zone.
BOOK_OUTLINE_SKELETON_SYSTEM = """你是小说策划师。只输出骨架五段（一、三、七、八、九），不要废话，不要 Markdown。

这是分阶段生成的第一阶段，后续阶段会补上二、四、五、六段，所以这里必须保留“一、”“三、”“七、”“八、”“九、”这些原编号。

一、书名与核心概念
简短有力的书名，一句话概括故事内核。

三、主角能力成长表
用表格列出主角每卷的能力变化，字段：卷数 | 修为境界/关键能力 | 道具武器功法 | 触发升级的事件 | 代价或反噬。至少 5 行。

七、分卷规划
每卷写清核心冲突、关键转折、主角状态变化。至少 3 卷。

八、核心伏笔
3–5 条贯穿全书的伏笔线，每条写明埋设时机、预期消解条件。

九、基调与类型标签
3–5 个短标签表达整体敘事基调（如“冷峻悬疑”“贵族权谋”“灾异意象”），再用一句话说明要避开哪些常见套路。

文字风格：句子长短不一，像老编辑在给新人讲故事方案，口语化但专业。"""

BOOK_OUTLINE_CHARACTERS_SYSTEM = """你是小说策划师。只输出角色与关系三段（二、四、五），不要废话，不要 Markdown。

必须基于用户创意和已生成的骨架（见 user 消息）保持一致。段落标题必须保留“二、”“四、”“五、”原编号，方便后续拼接。

二、主要角色与小传（至少 5 人）
每个角色单独一段，信息必须齐全：
- 基本信息：姓名、年龄、性别、外貌（一两句抓特征）、身份标签
- 出身与成长：家世、童年环境、关键童年事件、求学/师承
- 性格与动机：内核性格、表面伪装、所渴望之物、所恐惧之物、最不能碰的底线
- 关键创伤：塑造他的一次具体事件（必须有时间、地点、人物、冲击）
- 言语风格：1-2 句代表性对白，口吻独特
- 核心关系：与另外两三个主要角色的羁绊

四、角色关系网
明确写出主要角色之间的关系：谁和谁是敌人、盟友、师徒、恋人、竞争者。用“A → B：关系描述”的格式。

五、势力格局
3-5 个主要势力/阵营，每个势力的核心利益、代表人物、与主角的关系。

文字风格：像老编辑讲角色方案，具体不空泛。"""

BOOK_OUTLINE_WORLD_SYSTEM = """你是小说策划师。只输出世界观设定集（六），不要废话，不要 Markdown。

必须基于用户创意和已生成的骨架保持设定一致。段落标题保持“六、”原编号。

六、世界观设定集
系统罗列以下 8 个子项，每个子项单独成段，内容必须具体，不可空泛：
- 地理：大陆/国家/城邦/特殊地貌，给出主要名称和位置关系
- 历史：前朝/大战/神话断代，标记至少两个对当下有持续影响的历史事件
- 种族与势力：人族/妖族/异族/教派，各自特征与彼此的宿怨
- 宗教与神话：信仰体系、主神/异神、禁忌
- 政治与经济：权力结构、货币、贸易路线、阶级流动性
- 文化与日常：服饰、饮食、节庆、婚丧、礼仪
- 力量体系：境界或魔法分级、获取方式、能做到的事、代价与反噬、禁忌
- 特殊物品：重要法宝/神器/秘术，来源和传说

文字风格：像老编辑写设定集，具体可落笔，避免“大致”“大概”之类虚词。"""

VOLUME_OUTLINE_SYSTEM = """你是一位经验丰富的小说策划师。根据全书大纲和指定的卷号，生成该卷的详细大纲。

要求输出 JSON 格式：
{
  "volume_idx": 卷号,
  "title": "卷名",
  "core_conflict": "本卷核心冲突",
  "turning_points": ["转折点1", "转折点2"],
  "new_characters": [
    {"name": "角色名", "identity": "身份", "role": "作用"}
  ],
  "departing_characters": ["退场角色名"],
  "foreshadows": {
    "planted": [{"description": "新埋伏笔", "resolve_conditions": ["条件"]}],
    "resolved": ["本卷消解的伏笔描述"]
  },
  "emotional_arc": "本卷情感基调变化",
  "chapter_count": 本卷预计章数,
  "chapter_summaries": [
    {
      "chapter_idx": 1,
      "title": "章名",
      "summary": "本章概要（30-50字）",
      "key_events": ["事件"]
    }
  ],
  "transition_to_next": "与下一卷的衔接"
}

输出纯 JSON，不要包含 markdown 代码块标记"""

CHAPTER_OUTLINE_SYSTEM = """你是一位经验丰富的小说策划师。根据卷大纲和指定的章号，生成该章的详细大纲。

要求输出 JSON 格式：
{
  "chapter_idx": 章号,
  "title": "章名",
  "plot_points": [
    "剧情要点1（具体描述本章要发生什么）",
    "剧情要点2",
    "剧情要点3"
  ],
  "characters_present": ["出场角色"],
  "locations": ["场景地点"],
  "emotional_curve": {
    "opening": "开头情绪基调",
    "development": "发展过程情绪变化",
    "ending": "结尾情绪基调"
  },
  "word_count_target": 目标字数(整数),
  "foreshadow_notes": "本章伏笔相关提示（如有）",
  "transition_from_previous": "与上一章的衔接",
  "transition_to_next": "与下一章的衔接"
}

输出纯 JSON，不要包含 markdown 代码块标记"""


class OutlineGenerator:
    """Generates hierarchical outlines: book → volume → chapter."""

    def __init__(self):
        self.router = get_model_router()

    async def generate_book_outline(
        self,
        user_input: str,
        stream: bool = False,
        staged: bool = True,
    ) -> dict | AsyncIterator[str]:
        """Generate a book-level outline from user's creative input.

        v1.4.2 default (``staged=True``): splits the 9-section outline into
        three sequential LLM calls so no single response has to cover 10k+
        Chinese chars. Stages B and C run in parallel since they both depend
        only on stage A's skeleton. Avoids the long-output quality cliff.

        ``stream=True`` keeps the legacy single-call behavior; staged mode
        is not yet exposed over SSE.
        """
        from app.services.prompt_loader import load_prompt
        system = await load_prompt("outline_book", fallback=BOOK_OUTLINE_SYSTEM)
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": f"请根据以下创意生成全书大纲：\n\n{user_input}"},
        ]

        if stream and staged:
            # v1.4.2 Task B: structured staged SSE stream.
            # Emits stage_start / stage_chunk / stage_end / done dicts
            # that api/generate.py serializes over SSE.
            return self._generate_book_outline_staged_stream(user_input)

        if stream:
            return self.router.generate_stream(
                task_type="outline",
                messages=messages,
            )

        if staged:
            return await self._generate_book_outline_staged(user_input)

        result = await self.router.generate(
            task_type="outline",
            messages=messages,
        )
        return self._parse_json(result.text)

    # ------------------------------------------------------------------
    # v1.4.2 — staged book outline implementation
    # ------------------------------------------------------------------
    _SECTION_NUMS = ("一", "二", "三", "四", "五", "六", "七", "八", "九")

    async def _generate_book_outline_staged(self, user_input: str) -> dict:
        """Three-call staged book outline (A skeleton → B/C in parallel).

        Each stage stays below ~4k tokens so output quality stays in the
        safe zone for every model tier. The three stage outputs are
        reassembled into a single 9-section document in canonical order.
        """
        # Stage A — skeleton (一、三、七、八、九)
        skeleton_msgs = [
            {"role": "system", "content": BOOK_OUTLINE_SKELETON_SYSTEM},
            {"role": "user", "content": f"创意：\n{user_input}\n\n请生成骨架五段。"},
        ]
        skeleton_result = await self.router.generate(
            task_type="outline_book",
            messages=skeleton_msgs,
        )
        skeleton_text = (getattr(skeleton_result, "text", "") or "").strip()
        if not skeleton_text:
            logger.warning("Staged outline: stage A returned empty text")
            return {"raw_text": "", "_parse_error": True, "_staged": True}

        shared_context = (
            f"创意：\n{user_input}\n\n已生成的骨架：\n{skeleton_text}\n"
        )
        characters_msgs = [
            {"role": "system", "content": BOOK_OUTLINE_CHARACTERS_SYSTEM},
            {"role": "user", "content": shared_context + "\n请生成二、四、五三段。"},
        ]
        world_msgs = [
            {"role": "system", "content": BOOK_OUTLINE_WORLD_SYSTEM},
            {"role": "user", "content": shared_context + "\n请生成六、世界观设定集。"},
        ]
        characters_result, world_result = await asyncio.gather(
            self.router.generate(task_type="outline_book", messages=characters_msgs),
            self.router.generate(task_type="outline_book", messages=world_msgs),
            return_exceptions=True,
        )

        def _safe_text(res, stage_name: str) -> str:
            if isinstance(res, BaseException):
                logger.warning(
                    "Staged outline: stage %s failed: %s", stage_name, res
                )
                return ""
            return (getattr(res, "text", "") or "").strip()

        characters_text = _safe_text(characters_result, "B/characters")
        world_text = _safe_text(world_result, "C/world")

        combined = self._reassemble_sections(
            skeleton_text, characters_text, world_text
        )
        return {
            "raw_text": combined,
            "_staged": True,
            "_stages": {
                "skeleton": bool(skeleton_text),
                "characters": bool(characters_text),
                "world": bool(world_text),
            },
        }

    def _reassemble_sections(
        self,
        skeleton_text: str,
        characters_text: str,
        world_text: str,
    ) -> str:
        """Split each stage's text by 一..九 headers and emit in canonical order.

        Each section is owned by exactly one stage (A: 一、三、七、八、九;
        B: 二、四、五; C: 六), so we let the first occurrence win
        when a stray stage happens to emit a section it doesn't own.
        """
        buckets: dict[str, str] = {}
        for text in (skeleton_text, characters_text, world_text):
            for num, body in self._iter_sections(text):
                buckets.setdefault(num, body)
        ordered: list[str] = []
        for num in self._SECTION_NUMS:
            body = buckets.get(num)
            if body:
                ordered.append(body.strip())
        return "\n\n".join(ordered)

    def _iter_sections(self, text: str):
        """Yield (section_num, full_section_text) pairs for 一..九 headers."""
        if not text:
            return
        pattern = re.compile(
            rf"(?m)^(?P<num>[{''.join(self._SECTION_NUMS)}])、"
        )
        matches = list(pattern.finditer(text))
        for i, m in enumerate(matches):
            start = m.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            yield m.group("num"), text[start:end]

    # ------------------------------------------------------------------
    # v1.4.2 Task B — staged book-outline SSE stream
    # ------------------------------------------------------------------
    async def _generate_book_outline_staged_stream(
        self, user_input: str
    ):
        """Stream the staged book outline as structured SSE-ready events.

        Yields dicts with one of the following ``event`` values:

        - ``stage_start``: {stage, label, index, total}
        - ``stage_chunk``: {stage, delta}
        - ``stage_end``:   {stage, full_text}
        - ``error``:       {stage, message}
        - ``done``:        {full_outline, _stages}

        Stage A (skeleton) is streamed first and must complete before B/C
        start. Stages B (characters) and C (world) run concurrently and
        interleave their chunks by arrival order via an ``asyncio.Queue``.
        """
        # Stage A — skeleton (一、三、七、八、九).
        yield {
            "event": "stage_start",
            "stage": "A",
            "label": "骨架",
            "index": 1,
            "total": 3,
        }
        a_buf: list[str] = []
        skeleton_msgs = [
            {"role": "system", "content": BOOK_OUTLINE_SKELETON_SYSTEM},
            {
                "role": "user",
                "content": f"创意：\n{user_input}\n\n请生成骨架五段。",
            },
        ]
        try:
            async for delta in self.router.generate_stream(
                task_type="outline_book",
                messages=skeleton_msgs,
            ):
                if not delta:
                    continue
                a_buf.append(delta)
                yield {"event": "stage_chunk", "stage": "A", "delta": delta}
        except Exception as exc:  # noqa: BLE001 — surface as structured event
            logger.warning("Staged stream: stage A failed: %s", exc)
            yield {"event": "error", "stage": "A", "message": str(exc)}
            yield {
                "event": "done",
                "full_outline": "",
                "_stages": {"skeleton": False, "characters": False, "world": False},
            }
            return

        skeleton_text = "".join(a_buf).strip()
        yield {"event": "stage_end", "stage": "A", "full_text": skeleton_text}

        if not skeleton_text:
            yield {
                "event": "done",
                "full_outline": "",
                "_stages": {"skeleton": False, "characters": False, "world": False},
            }
            return

        shared_context = (
            f"创意：\n{user_input}\n\n已生成的骨架：\n{skeleton_text}\n"
        )
        stages = [
            {
                "code": "B",
                "label": "角色",
                "index": 2,
                "msgs": [
                    {
                        "role": "system",
                        "content": BOOK_OUTLINE_CHARACTERS_SYSTEM,
                    },
                    {
                        "role": "user",
                        "content": shared_context + "\n请生成二、四、五三段。",
                    },
                ],
            },
            {
                "code": "C",
                "label": "世界观",
                "index": 3,
                "msgs": [
                    {
                        "role": "system",
                        "content": BOOK_OUTLINE_WORLD_SYSTEM,
                    },
                    {
                        "role": "user",
                        "content": shared_context + "\n请生成六、世界观设定集。",
                    },
                ],
            },
        ]

        queue: asyncio.Queue = asyncio.Queue()
        buffers: dict[str, list[str]] = {"B": [], "C": []}
        DONE_MARK = object()

        async def _worker(stage: dict) -> None:
            code = stage["code"]
            await queue.put(
                {
                    "event": "stage_start",
                    "stage": code,
                    "label": stage["label"],
                    "index": stage["index"],
                    "total": 3,
                }
            )
            try:
                async for delta in self.router.generate_stream(
                    task_type="outline_book",
                    messages=stage["msgs"],
                ):
                    if not delta:
                        continue
                    buffers[code].append(delta)
                    await queue.put(
                        {"event": "stage_chunk", "stage": code, "delta": delta}
                    )
                await queue.put(
                    {
                        "event": "stage_end",
                        "stage": code,
                        "full_text": "".join(buffers[code]).strip(),
                    }
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Staged stream: stage %s failed: %s", code, exc
                )
                await queue.put(
                    {"event": "error", "stage": code, "message": str(exc)}
                )
            finally:
                await queue.put(DONE_MARK)

        tasks = [asyncio.create_task(_worker(s)) for s in stages]
        remaining = len(tasks)
        try:
            while remaining > 0:
                item = await queue.get()
                if item is DONE_MARK:
                    remaining -= 1
                    continue
                yield item
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        characters_text = "".join(buffers["B"]).strip()
        world_text = "".join(buffers["C"]).strip()
        combined = self._reassemble_sections(
            skeleton_text, characters_text, world_text
        )
        yield {
            "event": "done",
            "full_outline": combined,
            "_stages": {
                "skeleton": bool(skeleton_text),
                "characters": bool(characters_text),
                "world": bool(world_text),
            },
        }

    async def generate_volume_outline(
        self,
        book_outline: dict,
        volume_idx: int,
        user_notes: str = "",
        stream: bool = False,
    ) -> dict | AsyncIterator[str]:
        """Generate a volume-level outline from the book outline."""
        context = (
            f"全书大纲：\n{json.dumps(book_outline, ensure_ascii=False, indent=2)}\n\n"
            f"请生成第 {volume_idx} 卷的详细大纲。"
        )
        if user_notes:
            context += f"\n\n用户补充说明：{user_notes}"

        messages = [
            {"role": "system", "content": VOLUME_OUTLINE_SYSTEM},
            {"role": "user", "content": context},
        ]

        if stream:
            return self.router.generate_stream(
                task_type="outline",
                messages=messages,
            )

        result = await self.router.generate(
            task_type="outline",
            messages=messages,
        )
        return self._parse_json(result.text)

    async def generate_chapter_outline(
        self,
        book_outline: dict,
        volume_outline: dict,
        chapter_idx: int,
        previous_chapter_summary: str = "",
        user_notes: str = "",
        stream: bool = False,
    ) -> dict | AsyncIterator[str]:
        """Generate a chapter-level outline from the volume outline."""
        context = (
            f"全书大纲摘要：\n{json.dumps({'title': book_outline.get('title'), 'main_plot': book_outline.get('main_plot')}, ensure_ascii=False)}\n\n"
            f"本卷大纲：\n{json.dumps(volume_outline, ensure_ascii=False, indent=2)}\n\n"
            f"请生成第 {chapter_idx} 章的详细大纲。"
        )
        if previous_chapter_summary:
            context += f"\n\n上一章摘要：{previous_chapter_summary}"
        if user_notes:
            context += f"\n\n用户补充说明：{user_notes}"

        messages = [
            {"role": "system", "content": CHAPTER_OUTLINE_SYSTEM},
            {"role": "user", "content": context},
        ]

        if stream:
            return self.router.generate_stream(
                task_type="outline",
                messages=messages,
            )

        result = await self.router.generate(
            task_type="outline",
            messages=messages,
        )
        return self._parse_json(result.text)

    def _parse_json(self, text: str) -> dict:
        """Parse JSON from LLM response, handling markdown code blocks."""
        cleaned = text.strip()
        if cleaned.startswith("```"):
            lines = cleaned.split("\n")
            lines = lines[1:]  # remove opening ```json
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]  # remove closing ```
            cleaned = "\n".join(lines)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            logger.warning("Failed to parse outline JSON, returning raw text")
            return {"raw_text": text, "_parse_error": True}
