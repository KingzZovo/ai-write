"""渐进式规划:只为"即将开写的那一章"补最小资产(借鉴 AI-Novel-Writing-Assistant)。

对方产品的原则是**首章关键路径最小化**:写第 1 章之前,只同步等待 5 件最小资产,
完整世界观、远期卷规划、第 2 章以后的合同都不是首章前置条件。

    story_base        精简故事基础(全书大纲)
    world_slice       开篇世界切片(随全书大纲一起产出)
    core_characters   核心角色(Character 表,或全书大纲里的角色段)
    chapter_route     前 3~5 章路线(本章所在卷的 chapter_summaries)
    chapter_contract  本章合同(本章章纲)

与本仓库现状(``scripts/ops/plan_novel.py`` 先一次性生成完整书纲 + 整卷 100 章卷纲)
相反,``ensure_minimal_plan_for_chapter`` **缺什么只补什么**、而且只补到"够写这一章":
书纲缺才生成书纲;卷纲缺才生成卷纲,且只入库目标章之前的 ``max_route_chapters`` 章;
本章合同缺才生成本章合同。**绝不**因为"远期卷规划不存在"而阻塞第 1 章。

事务约定:本函数是"ensure"边界,补写成功后会自己 ``commit``(补写失败不改库)。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from uuid import UUID, uuid4

from sqlalchemy import select

from app.models.project import Character, Chapter, Outline, Project, Volume
from app.services.outline_generator import OutlineGenerator, extract_chapter_breakdown
from app.services.outline_readiness import has_meaningful_outline_content

logger = logging.getLogger(__name__)


# 最小资产的规范顺序 —— ``missing`` 永远按这个顺序输出。
PIECES = ("story_base", "world_slice", "core_characters", "chapter_route", "chapter_contract")


@dataclass
class MinimalPlanStatus:
    """本章开写前的最小资产盘点结果。"""

    ready: bool
    missing: list[str]  # 如 ["story_base", "world_slice", "core_characters", "chapter_route", "chapter_contract"]
    have: dict  # 已有的部分(补写后仍存在的部分按 PIECES 记在这里)
    # 生成/持久化过程中被吞掉的错误说明;非空不代表 crash,只是对应资产没补上。
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "ready": self.ready,
            "missing": list(self.missing),
            "have": dict(self.have),
            "errors": list(self.errors),
        }


@dataclass
class _State:
    """一次读取到的库内现状(补写过程中在内存里同步更新,不重复读库)。"""

    project: Project | None = None
    book: Outline | None = None
    book_content: dict | None = None
    volume: Volume | None = None
    volume_idx: int = 1
    volume_outline: Outline | None = None
    volume_content: dict | None = None
    characters: list = field(default_factory=list)
    chapter: Chapter | None = None
    chapter_outline: Outline | None = None
    chapter_plan: dict | None = None


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------

async def _read_state(db, project_id, chapter_idx: int) -> _State:
    """读取判断所需的一切(全部按 project_id 收窄;测试用 fake db 忽略 where)。"""
    pid = UUID(str(project_id))
    state = _State()
    state.project = await db.get(Project, pid)

    outlines = list((await db.execute(select(Outline).where(Outline.project_id == pid))).scalars().all())
    volumes = list((await db.execute(select(Volume).where(Volume.project_id == pid))).scalars().all())
    state.characters = list(
        (await db.execute(select(Character).where(Character.project_id == pid))).scalars().all()
    )

    state.book = _pick_book(outlines)
    if state.book is not None:
        state.book_content = dict(state.book.content_json or {})

    state.volume, state.volume_idx = _resolve_volume(volumes, outlines, chapter_idx)
    if state.volume is not None:
        state.volume_outline = _pick_volume_outline(outlines, state.volume)
        if state.volume_outline is not None:
            state.volume_content = dict(state.volume_outline.content_json or {})

    if state.volume is not None:
        chapters = list(
            (await db.execute(select(Chapter).where(Chapter.volume_id == state.volume.id))).scalars().all()
        )
        state.chapter = _pick_chapter(chapters, state.volume, chapter_idx)

    state.chapter_outline = _pick_chapter_outline(outlines, state.volume_outline, state.chapter, chapter_idx)
    if state.chapter is not None and isinstance(state.chapter.outline_json, dict) and state.chapter.outline_json:
        state.chapter_plan = dict(state.chapter.outline_json)
    elif state.chapter_outline is not None:
        state.chapter_plan = dict(state.chapter_outline.content_json or {})
    return state


def _pick_book(outlines) -> Outline | None:
    books = [o for o in outlines if getattr(o, "level", None) == "book" and o.content_json]
    if not books:
        return None
    return max(books, key=lambda o: (o.version or 1, str(o.id)))


def _resolve_volume(volumes, outlines, chapter_idx: int) -> tuple[Volume | None, int]:
    """本章所在卷。

    最小规划刻意只服务**开篇卷**:优先返回路线已覆盖目标章的卷,否则退回最小卷号的卷。
    ``chapter_idx`` 按卷内章号解释(与 ``Chapter.chapter_idx`` 一致)。
    """
    ordered = sorted(volumes, key=lambda v: (v.volume_idx, str(v.id)))
    if not ordered:
        return None, 1
    for volume in ordered:
        outline = _pick_volume_outline(outlines, volume)
        if outline is not None and chapter_idx in extract_chapter_breakdown(outline.content_json):
            return volume, volume.volume_idx
    return ordered[0], ordered[0].volume_idx


def _pick_volume_outline(outlines, volume: Volume | None) -> Outline | None:
    if volume is None:
        return None
    candidates = [
        o for o in outlines
        if getattr(o, "level", None) == "volume" and _volume_outline_matches(o, volume)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda o: (o.is_confirmed or 0, o.version or 1, str(o.id)))


def _volume_outline_matches(outline, volume: Volume) -> bool:
    content = outline.content_json or {}
    if not isinstance(content, dict):
        return False
    if content.get("volume_id") is not None:
        return str(content["volume_id"]) == str(volume.id)
    return str(content.get("volume_idx")) == str(volume.volume_idx)


def _pick_chapter(chapters, volume: Volume, chapter_idx: int) -> Chapter | None:
    matches = [
        c for c in chapters
        if str(c.volume_id) == str(volume.id) and c.chapter_idx == chapter_idx
    ]
    return matches[0] if matches else None


def _pick_chapter_outline(outlines, volume_outline, chapter, chapter_idx: int) -> Outline | None:
    candidates = []
    for o in outlines:
        if getattr(o, "level", None) != "chapter" or not o.content_json:
            continue
        data = o.content_json or {}
        if chapter is not None and str(data.get("chapter_id") or "") == str(chapter.id):
            candidates.append(o)
            continue
        if volume_outline is not None and o.parent_id == volume_outline.id and str(data.get("chapter_idx")) == str(chapter_idx):
            candidates.append(o)
            continue
        if chapter is None and volume_outline is None and str(data.get("chapter_idx")) == str(chapter_idx):
            candidates.append(o)
    if not candidates:
        return None
    return max(candidates, key=lambda o: (o.version or 1, str(o.id)))


# ---------------------------------------------------------------------------
# 判断"够不够写这一章"
# ---------------------------------------------------------------------------

def _usable(content) -> bool:
    """可用 = 非空、非解析错误、且含可写内容。"""
    return (
        isinstance(content, dict)
        and not content.get("_parse_error")
        and has_meaningful_outline_content(content)
    )


def _world_slice_present(book_content) -> bool:
    """开篇世界切片来自全书大纲的世界观段(staged 书纲必带"六、世界观设定集")。"""
    if not isinstance(book_content, dict):
        return False
    structured = book_content.get("structured")
    if isinstance(structured, dict):
        world = structured.get("world_setting")
        if isinstance(world, str) and world.strip():
            return True
    for key in ("world_setting", "world", "worldview"):
        value = book_content.get(key)
        if isinstance(value, str) and value.strip():
            return True
    raw = book_content.get("raw_text")
    return isinstance(raw, str) and "世界观" in raw


def _book_has_characters(book_content) -> bool:
    """核心角色在全书大纲里的出处(二、主要角色与小传)。"""
    if not isinstance(book_content, dict):
        return False
    structured = book_content.get("structured")
    if isinstance(structured, dict):
        chars = structured.get("characters")
        if isinstance(chars, str) and chars.strip():
            return True
        if isinstance(chars, (list, dict)) and chars:
            return True
    raw = book_content.get("raw_text")
    return isinstance(raw, str) and "主要角色" in raw


def _route(content) -> dict[int, dict]:
    return extract_chapter_breakdown(content)


# ---------------------------------------------------------------------------
# 补写(全部自己 add + flush;函数末尾 commit)
# ---------------------------------------------------------------------------

def _book_user_input(project: Project | None) -> str:
    if project is None:
        return ""
    parts = [project.premise or "", project.title or "", project.genre or ""]
    return "\n".join(p for p in parts if p).strip()


def _book_scale(project: Project | None) -> dict | None:
    if project is None or not project.target_word_count:
        return None
    try:
        from app.services.outline_generator import compute_scale

        settings = project.settings_json if isinstance(project.settings_json, dict) else {}
        chapter_words = int(settings.get("chapter_words") or 2000)
        chapters_per_volume = int(settings.get("chapters_per_volume") or 100)
        return compute_scale(
            project.target_word_count,
            chapter_words=chapter_words,
            chapters_per_volume_target=chapters_per_volume,
        )
    except Exception:  # noqa: BLE001 - 规模只是提示,拿不到就退回自由发挥
        return None


async def _persist_book(db, project_id, content: dict) -> Outline:
    row = Outline(
        id=uuid4(),
        project_id=UUID(str(project_id)),
        level="book",
        parent_id=None,
        content_json=dict(content),
        is_confirmed=1,
    )
    db.add(row)
    await db.flush()
    return row


async def _ensure_volume_row(db, project_id, state: _State) -> Volume:
    if state.volume is not None:
        return state.volume
    volume = Volume(
        id=uuid4(),
        project_id=UUID(str(project_id)),
        volume_idx=state.volume_idx,
        title=f"第{state.volume_idx}卷",
    )
    db.add(volume)
    await db.flush()
    state.volume = volume
    return volume


def _route_content(existing_content, generated_content, limit: int) -> dict:
    """把生成器给的整卷路线裁到前 ``limit`` 章;已有路线优先,只补新增章号。

    取舍见模块内 ``ensure_minimal_plan_for_chapter`` 的注释:生成器不支持局部生成,
    所以我们生成整卷但只保留足够写目标章的那几章。
    """
    generated_map = _route(generated_content)
    existing_map = _route(existing_content)
    merged = dict(existing_map)
    for idx in sorted(generated_map):
        if idx not in merged:
            merged[idx] = generated_map[idx]
    route = [merged[i] for i in sorted(merged)[:limit]]
    base = dict(existing_content) if existing_content else dict(generated_content)
    base["chapter_summaries"] = route
    # 清掉别名键,避免 extract_chapter_breakdown 读到未裁剪的旧数组。
    base.pop("batch", None)
    base.pop("chapters", None)
    base["_progressive_route"] = {
        "planned_chapters": len(route),
        "limit": limit,
        "truncated": len(generated_map) > limit,
    }
    return base


async def _persist_volume(db, project_id, state: _State, content: dict) -> tuple[Outline, Volume]:
    volume = await _ensure_volume_row(db, project_id, state)
    content = dict(content)
    content.update(volume_id=str(volume.id), volume_idx=volume.volume_idx)
    if state.volume_outline is not None:
        state.volume_outline.content_json = content
        state.volume_outline.is_confirmed = 1
        row = state.volume_outline
    else:
        row = Outline(
            id=uuid4(),
            project_id=UUID(str(project_id)),
            level="volume",
            parent_id=state.book.id if state.book is not None else None,
            content_json=content,
            is_confirmed=1,
        )
        db.add(row)
    await db.flush()
    return row, volume


async def _persist_chapter_contract(db, project_id, state: _State, content: dict, chapter_idx: int) -> Outline:
    content = dict(content)
    content.setdefault("chapter_idx", chapter_idx)
    if state.volume is not None:
        content.setdefault("volume_id", str(state.volume.id))
    if state.chapter is not None:
        content.setdefault("chapter_id", str(state.chapter.id))
        state.chapter.outline_json = content
    if state.chapter_outline is not None:
        state.chapter_outline.content_json = content
        row = state.chapter_outline
    else:
        row = Outline(
            id=uuid4(),
            project_id=UUID(str(project_id)),
            level="chapter",
            parent_id=state.volume_outline.id if state.volume_outline is not None else None,
            content_json=content,
        )
        db.add(row)
    await db.flush()
    return row


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

async def ensure_minimal_plan_for_chapter(
    db,
    project_id,
    chapter_idx: int,
    *,
    generator=None,
    max_route_chapters: int = 5,
) -> MinimalPlanStatus:
    """补齐"开写第 ``chapter_idx`` 章"所需的最小资产,返回盘点结果。

    判断与补写顺序(缺什么补什么,依赖不满足就跳过并记 errors):

      1. story_base —— 全书大纲缺/不可用 → ``generate_book_outline(..., staged=True)``;
         这一次调用同时产出 world_slice 与 core_characters(三段式书纲本就包含它们)。
      2. world_slice —— 只在书纲存在但缺世界观时记说明;最小规划不单独生成世界观。
      3. core_characters —— Character 表为空且书纲也没有角色段时记说明。
      4. chapter_route —— 本章所在卷的路线不覆盖 ``chapter_idx`` 时生成卷纲:
         - 没有卷纲 → 生成整卷,只入库前 ``max_route_chapters`` 章(取舍见下)。
         - 已有卷纲但路线不足 → 若已有章数 < ``max_route_chapters`` 则生成整卷并合并补足,
           否则不再生成(路线已到最小规划上限,超出范围无法安全补)。
         **生成器不支持"只生成前 N 章"**,所以这里生成整卷、只保留前 N 章,其余丢弃 ——
         见下方说明。
      5. chapter_contract —— 路线已覆盖本章、但本章章纲缺 → ``generate_chapter_outline``。

    只生成前 N 章路线的取舍:``OutlineGenerator.generate_volume_outline`` 没有"局部生成"
    入口(内部按 chapter_count 分 5 章一批串行调用),改它签名会破坏既有契约。因此当卷纲缺失
    时,我们照常调用它生成整卷,再**只把前 N 章 chapter_summaries 落库**,其余丢弃。代价是
    这次调用仍然按整卷计费;收益是库里的路线、后续章纲 prompt 与阅读面都只暴露 N 章,符合
    "最小前置资产"。若未来生成器支持 ``max_chapters`` 之类参数,可在此处直接传参替换掉丢弃。

    ``generator`` 可注入(测试用 fake);缺省 ``OutlineGenerator(project_id)``。
    """
    if type(chapter_idx) is not int or chapter_idx < 1:
        raise ValueError("chapter_idx 必须是正整数")
    if type(max_route_chapters) is not int or max_route_chapters < 1:
        raise ValueError("max_route_chapters 必须是正整数")

    if generator is None:
        generator = OutlineGenerator(str(project_id))

    state = await _read_state(db, project_id, chapter_idx)
    errors: list[str] = []
    wrote = False

    book_present = _usable(state.book_content)
    world_present = book_present and _world_slice_present(state.book_content)
    characters_present = bool(state.characters) or (book_present and _book_has_characters(state.book_content))
    route_present = bool(state.volume_content) and chapter_idx in _route(state.volume_content)
    contract_present = _usable(state.chapter_plan)

    # 1) story_base(顺带 world_slice / core_characters)
    if not book_present:
        try:
            content = await generator.generate_book_outline(
                user_input=_book_user_input(state.project),
                staged=True,
                scale=_book_scale(state.project),
            )
        except Exception as exc:  # noqa: BLE001 - 补写失败不 crash
            errors.append(f"story_base 生成失败:{exc!r}")
            content = None
        if _usable(content):
            state.book = await _persist_book(db, project_id, content)
            state.book_content = content
            wrote = True
            book_present = True
            world_present = _world_slice_present(content)
            characters_present = bool(state.characters) or _book_has_characters(content)
        else:
            errors.append("story_base 仍缺失:书纲生成未返回可用内容")

    if book_present and not world_present:
        errors.append("world_slice 缺失:书纲没有世界观段,最小规划不单独生成世界观(需重生成书纲)")
    if book_present and not characters_present:
        errors.append("core_characters 缺失:Character 表为空且书纲没有角色段(需重生成书纲)")

    # 2) chapter_route(需要 story_base)
    if not route_present:
        if not book_present:
            errors.append("chapter_route 缺失:缺少 story_base,无法生成卷纲")
        elif state.volume_content is not None and len(_route(state.volume_content)) >= max_route_chapters:
            existing_route_len = len(_route(state.volume_content))
            errors.append(
                f"chapter_route 缺失:卷纲只覆盖前 {existing_route_len} 章,"
                f"第 {chapter_idx} 章超出最小路线范围(max_route_chapters={max_route_chapters});需完整卷规划"
            )
        else:
            try:
                generated = await generator.generate_volume_outline(
                    book_outline=state.book_content,
                    volume_idx=state.volume_idx,
                    staged=True,
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(f"chapter_route 生成失败:{exc!r}")
                generated = None
            if _usable(generated):
                content = _route_content(state.volume_content, generated, max_route_chapters)
                row, _volume = await _persist_volume(db, project_id, state, content)
                state.volume_outline = row
                state.volume_content = row.content_json
                wrote = True
                route_present = chapter_idx in _route(state.volume_content)
                if not route_present:
                    errors.append(
                        f"chapter_route 仍缺失:生成的卷纲路线未覆盖第 {chapter_idx} 章"
                    )
            elif generated is not None:
                errors.append("chapter_route 仍缺失:卷纲生成返回解析错误")

    # 3) chapter_contract(需要 story_base + 覆盖本章的路线)
    if not contract_present:
        if not route_present:
            errors.append(f"chapter_contract 缺失:第 {chapter_idx} 章路线不存在,不能凭空生成章合同")
        elif book_present:
            try:
                plan = await generator.generate_chapter_outline(
                    book_outline=state.book_content,
                    volume_outline=state.volume_content,
                    chapter_idx=chapter_idx,
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(f"chapter_contract 生成失败:{exc!r}")
                plan = None
            if _usable(plan):
                state.chapter_outline = await _persist_chapter_contract(
                    db, project_id, state, plan, chapter_idx
                )
                state.chapter_plan = plan
                wrote = True
                contract_present = True
            elif plan is not None:
                errors.append("chapter_contract 仍缺失:章纲生成返回解析错误")
        else:
            errors.append("chapter_contract 缺失:缺少 story_base,无法生成章合同")

    have: dict = {}
    present = {
        "story_base": book_present,
        "world_slice": world_present,
        "core_characters": characters_present,
        "chapter_route": route_present,
        "chapter_contract": contract_present,
    }
    if book_present:
        have["story_base"] = str(state.book.id) if state.book is not None else True
    if world_present:
        have["world_slice"] = True
    if characters_present:
        have["core_characters"] = len(state.characters) or "book_outline"
    if route_present:
        have["chapter_route"] = sorted(_route(state.volume_content))
    if contract_present:
        have["chapter_contract"] = (
            str(state.chapter_outline.id) if state.chapter_outline is not None else True
        )

    missing = [piece for piece in PIECES if not present[piece]]

    if wrote:
        try:
            await db.commit()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"提交生成结果失败:{exc!r}")

    return MinimalPlanStatus(
        ready=not missing,
        missing=missing,
        have=have,
        errors=errors,
    )
