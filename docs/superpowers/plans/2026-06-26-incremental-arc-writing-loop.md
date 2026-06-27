# 弧式增量创作循环 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现"点子→补全设定→一章一章写→一弧结束给建议问下一段"的增量创作编排层，弧物理复用 Volume 表、章节复用 B 的 `run_chapter_pipeline`，状态寄存 volume-level `Outline.content_json._arc`，零数据库迁移。

**Architecture:** 新增 `services/arc_loop.py`（纯状态机 + LLM 弧大纲/问题/建议生成）与 `api/arc.py`（6 个端点）。弧状态机 `advance_arc_state` 是纯函数可无 LLM 单测。所有 LLM 调用走既有 `run_structured_prompt`/`run_text_prompt`（带 json_repair + 降级）。`CHAPTER_PIPELINE_ENABLED` / `AskUserPause` / `Volume` / `Outline` 全部复用，零迁移。

**Tech Stack:** Python / FastAPI / SQLAlchemy async / pytest + pytest-asyncio。

---

## 关键既有事实（已核对当前代码）

- `Volume(project_id, title, volume_idx, summary, target_word_count)` — `app/models/project.py:68`。创建模式见 `app/api/volumes.py:59`（`db.add` + `flush` + `refresh`）。
- `Outline(project_id, level, parent_id, content_json, version, is_confirmed)` — `app/models/project.py:134`。`level` 是自由字符串，`content_json` 是 JSON（可放 `_arc` 命名空间，无需迁移）。创建模式见 `app/api/outlines.py:67`。
- `Chapter(volume_id, title, chapter_idx, outline_json, content_text, word_count, status, summary, target_word_count)` — `app/models/project.py:94`。
- `run_chapter_pipeline(*, text, db, project_id=None, chapter_id=None, target_word_count=None, chapter_outline=None, prev_chapter_tail="", skip_polish=False) -> ChapterPipelineResult` — `app/services/chapter_pipeline.py:102`。**本计划不改它**。A 只组装"下一章 brief"，正文生成仍走既有 `/api/generate/chapter`。
- `run_structured_prompt(task_type, user_content, db, extra_system="", project_id=None, chapter_id=None, **kwargs) -> dict` — `app/services/prompt_registry.py:783`，带 json_repair + strict retry，无注册 prompt 抛 `ValueError`。
- `run_text_prompt(task_type, user_content, db, ...) -> GenerationResult`，`result.text` 为正文。
- `_TASK_TYPE_FALLBACK`（`prompt_registry.py:520`）：未注册 task_type 回退到既有 prompt。
- 路由注册在 `app/main.py:244+`（一行 `app.include_router(...)`）。
- 测试惯例：`cd /root/ai-write/backend && .venv/bin/python -m pytest`。本计划全部用 mock LLM 单测，不打真 LLM。
- `get_db`（`app/db/session.py:136`）成功提交、异常回滚。

---

## File Structure

| 文件 | 责任 | 新建/改动 |
|------|------|-----------|
| `backend/app/services/arc_loop.py` | `ArcState` 解析/序列化、`advance_arc_state` 纯状态机、`build_arc_kickoff_questions`、`generate_arc_outline`、`build_arc_completion_suggestions`、`build_next_chapter_brief`（均 mock-LLM 可测） | 新建 |
| `backend/app/api/arc.py` | 6 端点：start / current / next-direction / chapter-brief / complete / next-arc | 新建 |
| `backend/app/main.py` | 注册 arc 路由（1 行） | 改 |
| `backend/app/services/prompt_registry.py` | `_TASK_TYPE_FALLBACK` 增 `arc_outline→outline_volume`、`arc_kickoff→critic`、`arc_suggest→critic` | 改 1 处 |
| `backend/tests/services/test_arc_loop.py` | arc_loop 单测 | 新建 |
| `backend/tests/api/test_arc_api.py` | arc API 契约单测 | 新建 |

---

## Task 1: ArcState 数据结构 + 解析/序列化

**Files:**
- Create: `backend/app/services/arc_loop.py`
- Test: `backend/tests/services/test_arc_loop.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/services/test_arc_loop.py
from __future__ import annotations


def test_arc_state_roundtrip_and_defaults() -> None:
    from app.services.arc_loop import ArcState, parse_arc_state, serialize_arc_state

    raw = {
        "volume_idx": 1,
        "_arc": {
            "is_arc": True,
            "title": "边境小城御敌",
            "core_setup": "主角在边境小城的大敌",
            "opening_scene": "有人上门找茬",
            "target_chapters": 20,
            "status": "active",
            "chapters_written": 0,
            "running_outline": "",
            "next_direction": None,
            "suggestions": [],
        },
    }
    state = parse_arc_state(raw)
    assert state is not None
    assert state.title == "边境小城御敌"
    assert state.target_chapters == 20
    assert state.status == "active"
    assert state.chapters_written == 0

    # serialize 回 content_json，_arc 命名空间保留 volume_idx
    out = serialize_arc_state(state, volume_idx=1)
    assert out["volume_idx"] == 1
    assert out["_arc"]["title"] == "边境小城御敌"
    assert out["_arc"]["is_arc"] is True


def test_parse_arc_state_returns_none_for_non_arc() -> None:
    from app.services.arc_loop import parse_arc_state

    # 旧 volume outline（无 _arc）→ None，便于 API 对旧项目返回 null
    assert parse_arc_state({"volume_idx": 3, "core_conflict": "x"}) is None
    assert parse_arc_state({}) is None
    assert parse_arc_state(None) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/ai-write/backend && .venv/bin/python -m pytest tests/services/test_arc_loop.py -k arc_state -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.arc_loop'`

- [ ] **Step 3: Write minimal implementation**

```python
# backend/app/services/arc_loop.py
"""弧式增量创作循环（子项目 A）。

哲学：从点子出发，一弧（≈20 章一段连贯故事）一弧地写，绝不预先规划几百几千
章的大伏笔。弧物理复用 Volume；弧状态寄存 volume-level Outline.content_json
的 _arc 命名空间（零迁移）。章节正文生成复用 B 的 run_chapter_pipeline。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

ARC_STATUS_ACTIVE = "active"               # 可继续写下一章
ARC_STATUS_AWAITING = "awaiting_direction"  # 等作者给下一章方向
ARC_STATUS_COMPLETED = "completed"          # 本弧写满，等开下一弧

_DEFAULT_TARGET_CHAPTERS = 20
_MIN_TARGET = 4
_MAX_TARGET = 40


def clamp_target_chapters(value: object) -> int:
    try:
        n = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return _DEFAULT_TARGET_CHAPTERS
    return max(_MIN_TARGET, min(_MAX_TARGET, n))


@dataclass
class ArcState:
    title: str
    core_setup: str
    opening_scene: str
    target_chapters: int = _DEFAULT_TARGET_CHAPTERS
    status: str = ARC_STATUS_ACTIVE
    chapters_written: int = 0
    running_outline: str = ""
    next_direction: str | None = None
    suggestions: list[str] = field(default_factory=list)


def parse_arc_state(content_json: dict | None) -> ArcState | None:
    """从 volume-level Outline.content_json 取出 _arc；非弧返回 None。"""
    if not isinstance(content_json, dict):
        return None
    arc = content_json.get("_arc")
    if not isinstance(arc, dict) or not arc.get("is_arc"):
        return None
    return ArcState(
        title=str(arc.get("title") or ""),
        core_setup=str(arc.get("core_setup") or ""),
        opening_scene=str(arc.get("opening_scene") or ""),
        target_chapters=clamp_target_chapters(arc.get("target_chapters")),
        status=str(arc.get("status") or ARC_STATUS_ACTIVE),
        chapters_written=int(arc.get("chapters_written") or 0),
        running_outline=str(arc.get("running_outline") or ""),
        next_direction=arc.get("next_direction"),
        suggestions=list(arc.get("suggestions") or []),
    )


def serialize_arc_state(state: ArcState, *, volume_idx: int) -> dict:
    """组装回 content_json（含 volume_idx 供前端分卷映射 + _arc 命名空间）。"""
    return {
        "volume_idx": volume_idx,
        "_arc": {
            "is_arc": True,
            "title": state.title,
            "core_setup": state.core_setup,
            "opening_scene": state.opening_scene,
            "target_chapters": state.target_chapters,
            "status": state.status,
            "chapters_written": state.chapters_written,
            "running_outline": state.running_outline,
            "next_direction": state.next_direction,
            "suggestions": list(state.suggestions),
        },
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/ai-write/backend && .venv/bin/python -m pytest tests/services/test_arc_loop.py -k arc_state -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
cd /root/ai-write && git add backend/app/services/arc_loop.py backend/tests/services/test_arc_loop.py
git commit -m "feat(arc): ArcState parse/serialize with _arc namespace (zero migration)"
```

---

## Task 2: `advance_arc_state` 纯状态机

**Files:**
- Modify: `backend/app/services/arc_loop.py`
- Test: `backend/tests/services/test_arc_loop.py`

- [ ] **Step 1: Write the failing test**

```python
def test_advance_after_writing_chapter_awaits_direction() -> None:
    from app.services.arc_loop import (
        ArcState, advance_arc_state, ARC_STATUS_AWAITING,
    )

    state = ArcState(title="t", core_setup="c", opening_scene="o",
                     target_chapters=20, status="active", chapters_written=0)
    new = advance_arc_state(state, event="chapter_written",
                            running_outline_append="第1章：主角出场。")
    assert new.chapters_written == 1
    assert new.status == ARC_STATUS_AWAITING        # 未写满 → 等方向
    assert "第1章" in new.running_outline
    assert new.next_direction is None               # 写完清空上一条方向


def test_advance_to_completed_when_target_reached() -> None:
    from app.services.arc_loop import ArcState, advance_arc_state, ARC_STATUS_COMPLETED

    state = ArcState(title="t", core_setup="c", opening_scene="o",
                     target_chapters=2, status="active", chapters_written=1)
    new = advance_arc_state(state, event="chapter_written",
                            running_outline_append="第2章。")
    assert new.chapters_written == 2
    assert new.status == ARC_STATUS_COMPLETED       # 写满 → completed


def test_advance_set_direction_returns_to_active() -> None:
    from app.services.arc_loop import ArcState, advance_arc_state, ARC_STATUS_ACTIVE

    state = ArcState(title="t", core_setup="c", opening_scene="o",
                     target_chapters=20, status="awaiting_direction", chapters_written=1)
    new = advance_arc_state(state, event="set_direction",
                            next_direction="主角发现跑不了，打算狐假虎威")
    assert new.status == ARC_STATUS_ACTIVE
    assert new.next_direction == "主角发现跑不了，打算狐假虎威"


def test_advance_set_direction_blocked_when_completed() -> None:
    from app.services.arc_loop import ArcState, advance_arc_state, ARC_STATUS_COMPLETED

    state = ArcState(title="t", core_setup="c", opening_scene="o",
                     target_chapters=2, status="completed", chapters_written=2)
    # 弧已满，给方向不应重开（仍 completed）
    new = advance_arc_state(state, event="set_direction", next_direction="x")
    assert new.status == ARC_STATUS_COMPLETED
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/ai-write/backend && .venv/bin/python -m pytest tests/services/test_arc_loop.py -k advance -v`
Expected: FAIL with `ImportError: cannot import name 'advance_arc_state'`

- [ ] **Step 3: Write minimal implementation**

Append to `backend/app/services/arc_loop.py`:

```python
def advance_arc_state(
    state: ArcState,
    *,
    event: str,
    running_outline_append: str = "",
    next_direction: str | None = None,
) -> ArcState:
    """纯状态机：根据事件推进弧状态（无 LLM、无 IO，可单测）。

    event:
      - "chapter_written": chapters_written+1，追加 running_outline，清 next_direction，
        写满→completed 否则→awaiting_direction。
      - "set_direction": 仅当未 completed 时，写入 next_direction 并回到 active。
    """
    s = ArcState(
        title=state.title,
        core_setup=state.core_setup,
        opening_scene=state.opening_scene,
        target_chapters=state.target_chapters,
        status=state.status,
        chapters_written=state.chapters_written,
        running_outline=state.running_outline,
        next_direction=state.next_direction,
        suggestions=list(state.suggestions),
    )
    if event == "chapter_written":
        s.chapters_written += 1
        if running_outline_append:
            s.running_outline = (
                f"{s.running_outline}\n{running_outline_append}".strip()
                if s.running_outline else running_outline_append.strip()
            )
        s.next_direction = None
        if s.chapters_written >= s.target_chapters:
            s.status = ARC_STATUS_COMPLETED
        else:
            s.status = ARC_STATUS_AWAITING
    elif event == "set_direction":
        if s.status != ARC_STATUS_COMPLETED:
            s.next_direction = next_direction
            s.status = ARC_STATUS_ACTIVE
    return s
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/ai-write/backend && .venv/bin/python -m pytest tests/services/test_arc_loop.py -k advance -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
cd /root/ai-write && git add backend/app/services/arc_loop.py backend/tests/services/test_arc_loop.py
git commit -m "feat(arc): advance_arc_state pure state machine"
```

---

## Task 3: 弧大纲生成 `generate_arc_outline`（哲学约束 + 降级）

**Files:**
- Modify: `backend/app/services/arc_loop.py`
- Test: `backend/tests/services/test_arc_loop.py`

- [ ] **Step 1: Write the failing test**

```python
import pytest


def test_build_arc_outline_prompt_forbids_long_range() -> None:
    from app.services.arc_loop import build_arc_outline_prompt

    prompt = build_arc_outline_prompt(
        idea="玄幻，主角穿越到边境小城",
        background="功法体系XX，战力体系YY",
        core_setup="主角在边境小城有大敌",
        opening_scene="有人上门找茬",
        target_chapters=20,
    )
    # A 的灵魂：必须显式禁止千章伏笔、只规划本弧
    assert "只规划" in prompt or "只规划本弧" in prompt
    assert "20" in prompt
    assert "伏笔" in prompt   # 禁止跨弧大伏笔的措辞
    assert "有人上门找茬" in prompt


@pytest.mark.asyncio
async def test_generate_arc_outline_happy(monkeypatch) -> None:
    import app.services.arc_loop as al

    async def fake_structured(task_type, user_content, db, **kwargs):
        assert task_type == "arc_outline"
        return {
            "title": "边境小城御敌",
            "beats": [
                {"chapter": 1, "beat": "主角穿越，遇上门挑衅"},
                {"chapter": 2, "beat": "狐假虎威吓退对手"},
            ],
        }

    monkeypatch.setattr(al, "run_structured_prompt", fake_structured)

    result = await al.generate_arc_outline(
        idea="玄幻穿越", background="体系XX", core_setup="有大敌",
        opening_scene="上门找茬", target_chapters=20, db=object(),
        project_id="p",
    )
    assert result["title"] == "边境小城御敌"
    assert len(result["beats"]) == 2


@pytest.mark.asyncio
async def test_generate_arc_outline_degrades(monkeypatch) -> None:
    import app.services.arc_loop as al

    async def boom(*a, **k):
        raise RuntimeError("relay 503")

    monkeypatch.setattr(al, "run_structured_prompt", boom)

    result = await al.generate_arc_outline(
        idea="x", background="y", core_setup="z", opening_scene="w",
        target_chapters=20, db=object(), project_id="p",
    )
    # 失败 → available=False 哨兵，调用方据此回滚不建半截 Volume
    assert result["available"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/ai-write/backend && .venv/bin/python -m pytest tests/services/test_arc_loop.py -k arc_outline -v`
Expected: FAIL with `ImportError: cannot import name 'build_arc_outline_prompt'`

- [ ] **Step 3: Write minimal implementation**

Append to `backend/app/services/arc_loop.py` (add `run_structured_prompt` import at top):

```python
# 加到顶部 import 区：
# from app.services.prompt_registry import run_structured_prompt, run_text_prompt

_ARC_OUTLINE_CONTRACT = """\
你是网文增量创作的弧规划师。只规划「当前这一段连贯故事」（一个弧，约 {target} 章），
绝不规划几百几千章之后的大伏笔、终局或跨弧悬念。

硬约束：
1. 只规划本弧 {target} 章，每章给一个 beat 方向（一句话）。
2. 禁止埋设超出本弧的长线伏笔、终局铺垫、跨弧悬念。
3. 钩子只做弧内钩子（本弧内能回收）。
4. 大纲是软骨架：作者每章可用新方向改写后续走向，不要写死。

只输出 JSON，不要解释或 Markdown：
{{
  "title": "本弧标题（如：边境小城御敌）",
  "beats": [{{"chapter": 1, "beat": "一句话方向"}}]
}}"""


def build_arc_outline_prompt(
    *,
    idea: str,
    background: str,
    core_setup: str,
    opening_scene: str,
    target_chapters: int,
) -> str:
    contract = _ARC_OUTLINE_CONTRACT.format(target=target_chapters)
    return (
        f"{contract}\n\n"
        f"【点子】{idea}\n"
        f"【背景设定】{background}\n"
        f"【本弧核心设定】{core_setup}\n"
        f"【开场场景】{opening_scene}\n"
        f"【本弧章数】{target_chapters}"
    )


async def generate_arc_outline(
    *,
    idea: str,
    background: str,
    core_setup: str,
    opening_scene: str,
    target_chapters: int,
    db: object,
    project_id: object = None,
) -> dict:
    """生成小弧大纲。失败返回 {"available": False}（调用方回滚，不建半截 Volume）。"""
    prompt = build_arc_outline_prompt(
        idea=idea, background=background, core_setup=core_setup,
        opening_scene=opening_scene, target_chapters=target_chapters,
    )
    try:
        parsed = await run_structured_prompt(
            "arc_outline", prompt, db, project_id=project_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("generate_arc_outline failed; degrading: %s", exc)
        return {"available": False}
    if not isinstance(parsed, dict):
        return {"available": False}
    parsed.setdefault("available", True)
    parsed.setdefault("title", "")
    parsed.setdefault("beats", [])
    return parsed
```

Also add to the top import block of `arc_loop.py`:

```python
from app.services.prompt_registry import run_structured_prompt, run_text_prompt
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/ai-write/backend && .venv/bin/python -m pytest tests/services/test_arc_loop.py -k arc_outline -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
cd /root/ai-write && git add backend/app/services/arc_loop.py backend/tests/services/test_arc_loop.py
git commit -m "feat(arc): generate_arc_outline with anti-long-range philosophy + degradation"
```

---

## Task 4: kickoff 问题 + 弧末建议 + 下一章 brief

**Files:**
- Modify: `backend/app/services/arc_loop.py`
- Test: `backend/tests/services/test_arc_loop.py`

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_kickoff_questions_happy_and_degrade(monkeypatch) -> None:
    import app.services.arc_loop as al

    async def fake_structured(task_type, user_content, db, **kwargs):
        assert task_type == "arc_kickoff"
        return {"questions": ["主角的金手指是什么？", "边境小城的大敌叫什么？"]}

    monkeypatch.setattr(al, "run_structured_prompt", fake_structured)
    qs = await al.build_arc_kickoff_questions(
        idea="玄幻穿越", background="体系XX", db=object(), project_id="p",
    )
    assert len(qs) == 2

    async def boom(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(al, "run_structured_prompt", boom)
    qs2 = await al.build_arc_kickoff_questions(
        idea="x", background="y", db=object(), project_id="p",
    )
    assert qs2 == []   # 失败→空列表（跳过补全，不阻断）


@pytest.mark.asyncio
async def test_completion_suggestions(monkeypatch) -> None:
    import app.services.arc_loop as al

    async def fake_structured(task_type, user_content, db, **kwargs):
        assert task_type == "arc_suggest"
        return {"suggestions": ["进城拜师", "被仇家追杀", "捡到秘籍"]}

    monkeypatch.setattr(al, "run_structured_prompt", fake_structured)
    sugg = await al.build_arc_completion_suggestions(
        background="体系XX", running_outline="边境御敌已了",
        db=object(), project_id="p",
    )
    assert len(sugg) == 3


def test_build_next_chapter_brief() -> None:
    from app.services.arc_loop import ArcState, build_next_chapter_brief

    state = ArcState(title="边境御敌", core_setup="有大敌", opening_scene="上门找茬",
                     target_chapters=20, status="active", chapters_written=1,
                     running_outline="第1章：主角穿越。",
                     next_direction="狐假虎威吓退对手")
    brief = build_next_chapter_brief(state, arc_beats=[
        {"chapter": 2, "beat": "对峙"}])
    # brief 必须含：本弧标题、到目前故事线、作者下一步方向、本章 beat
    assert "边境御敌" in brief
    assert "第1章：主角穿越" in brief
    assert "狐假虎威" in brief
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/ai-write/backend && .venv/bin/python -m pytest tests/services/test_arc_loop.py -k "kickoff or completion or next_chapter_brief" -v`
Expected: FAIL with `ImportError: cannot import name 'build_arc_kickoff_questions'`

- [ ] **Step 3: Write minimal implementation**

Append to `backend/app/services/arc_loop.py`:

```python
async def build_arc_kickoff_questions(
    *, idea: str, background: str, db: object, project_id: object = None,
) -> list[str]:
    """生成补全初始设定的几个问题。失败返回 []（跳过补全，不阻断）。"""
    prompt = (
        "作者要用以下点子和背景开写一部网文。请提出 2-4 个最关键的、"
        "补全初始设定必须先问清楚的问题（如金手指、主角动机、当前最大威胁）。"
        "只输出 JSON：{\"questions\": [\"...\"]}。\n\n"
        f"【点子】{idea}\n【背景设定】{background}"
    )
    try:
        parsed = await run_structured_prompt(
            "arc_kickoff", prompt, db, project_id=project_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("build_arc_kickoff_questions failed; skipping: %s", exc)
        return []
    if isinstance(parsed, dict) and isinstance(parsed.get("questions"), list):
        return [str(q) for q in parsed["questions"] if str(q).strip()]
    return []


async def build_arc_completion_suggestions(
    *, background: str, running_outline: str, db: object, project_id: object = None,
) -> list[str]:
    """弧写满后，根据背景设定给几个下一弧的开场建议。失败返回 []。"""
    prompt = (
        "一个弧（一段连贯故事）刚写完。请根据背景设定与已发生的故事，"
        "给作者 3 个「下一段可以怎么走」的开场建议（每个一句话，互不雷同，"
        "符合已建立的设定，不要剧透式规划长线）。只输出 JSON："
        "{\"suggestions\": [\"...\"]}。\n\n"
        f"【背景设定】{background}\n【本弧已发生】{running_outline}"
    )
    try:
        parsed = await run_structured_prompt(
            "arc_suggest", prompt, db, project_id=project_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("build_arc_completion_suggestions failed: %s", exc)
        return []
    if isinstance(parsed, dict) and isinstance(parsed.get("suggestions"), list):
        return [str(s) for s in parsed["suggestions"] if str(s).strip()]
    return []


def build_next_chapter_brief(state: ArcState, *, arc_beats: list[dict] | None = None) -> str:
    """组装下一章 brief（喂给 /api/generate/chapter 的 chapter_outline 文本）。

    纯函数：本弧标题 + 到目前故事线 + 作者下一步方向 + 本章 beat（若有）。
    """
    next_chapter_idx = state.chapters_written + 1
    beat = ""
    for b in (arc_beats or []):
        try:
            if int(b.get("chapter")) == next_chapter_idx:
                beat = str(b.get("beat") or "")
                break
        except (TypeError, ValueError):
            continue
    parts = [
        f"【本弧】{state.title}",
        f"【本弧到目前的故事线】\n{state.running_outline}" if state.running_outline else "",
        f"【作者指定的下一步方向】{state.next_direction}" if state.next_direction else "",
        f"【本章（第{next_chapter_idx}章）大纲 beat】{beat}" if beat else "",
        "请据此写这一章。保持与上文连贯，不要引入本弧之外的长线伏笔。",
    ]
    return "\n".join(p for p in parts if p)
```

Note: `build_next_chapter_brief` uses keyword-only `arc_beats` — the test calls it `build_next_chapter_brief(state, arc_beats=[...])`. Update the Step-1 test to match keyword form (already written with `arc_beats=`).

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/ai-write/backend && .venv/bin/python -m pytest tests/services/test_arc_loop.py -v`
Expected: PASS (all arc_loop tests)

- [ ] **Step 5: Commit**

```bash
cd /root/ai-write && git add backend/app/services/arc_loop.py backend/tests/services/test_arc_loop.py
git commit -m "feat(arc): kickoff questions, completion suggestions, next-chapter brief"
```

---

## Task 5: task_type fallbacks（arc_outline/arc_kickoff/arc_suggest）

**Files:**
- Modify: `backend/app/services/prompt_registry.py:520`
- Test: `backend/tests/services/test_arc_loop.py`

- [ ] **Step 1: Write the failing test**

```python
def test_arc_task_type_fallbacks_registered() -> None:
    from app.services.prompt_registry import _TASK_TYPE_FALLBACK

    assert _TASK_TYPE_FALLBACK.get("arc_outline") == "outline_volume"
    assert _TASK_TYPE_FALLBACK.get("arc_kickoff") == "critic"
    assert _TASK_TYPE_FALLBACK.get("arc_suggest") == "critic"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/ai-write/backend && .venv/bin/python -m pytest tests/services/test_arc_loop.py::test_arc_task_type_fallbacks_registered -v`
Expected: FAIL with `AssertionError: assert None == 'outline_volume'`

- [ ] **Step 3: Write minimal implementation**

In `backend/app/services/prompt_registry.py`, find `_TASK_TYPE_FALLBACK` (~line 520) and add three entries (after the existing logic_critic/drafter entries from subproject B):

```python
    # Incremental arc writing loop (subproject A): degrade to existing prompts.
    "arc_outline": "outline_volume",
    "arc_kickoff": "critic",
    "arc_suggest": "critic",
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/ai-write/backend && .venv/bin/python -m pytest tests/services/test_arc_loop.py::test_arc_task_type_fallbacks_registered -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /root/ai-write && git add backend/app/services/prompt_registry.py backend/tests/services/test_arc_loop.py
git commit -m "feat(arc): register arc_outline/arc_kickoff/arc_suggest task_type fallbacks"
```

---

## Task 6: arc API — `/start` + `/current`

**Files:**
- Create: `backend/app/api/arc.py`
- Modify: `backend/app/main.py`
- Test: `backend/tests/api/test_arc_api.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/api/test_arc_api.py
from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_start_arc_creates_volume_and_outline(auth_client, monkeypatch):
    import app.api.arc as arc_mod

    # mock 弧大纲生成（不打真 LLM）
    async def fake_outline(**kwargs):
        return {"available": True, "title": "边境小城御敌",
                "beats": [{"chapter": 1, "beat": "主角穿越遇挑衅"}]}

    monkeypatch.setattr(arc_mod, "generate_arc_outline", fake_outline)

    resp = await auth_client.post("/api/projects", json={"title": "弧测试", "genre": "玄幻"})
    assert resp.status_code == 201
    pid = resp.json()["id"]

    try:
        r = await auth_client.post(f"/api/arc/{pid}/start", json={
            "idea": "玄幻穿越边境小城",
            "background": "功法体系XX 战力YY",
            "core_setup": "主角有大敌",
            "opening_scene": "有人上门找茬",
            "target_chapters": 20,
        })
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["arc"]["title"] == "边境小城御敌"
        assert body["arc"]["status"] == "active"
        assert body["arc"]["chapters_written"] == 0
        assert body["volume_idx"] == 1

        # current 返回刚建的弧
        c = await auth_client.get(f"/api/arc/{pid}/current")
        assert c.status_code == 200
        assert c.json()["arc"]["title"] == "边境小城御敌"
    finally:
        await auth_client.delete(f"/api/projects/{pid}?purge=true")


@pytest.mark.asyncio
async def test_current_returns_null_for_non_arc_project(auth_client):
    resp = await auth_client.post("/api/projects", json={"title": "非弧", "genre": "x"})
    pid = resp.json()["id"]
    try:
        c = await auth_client.get(f"/api/arc/{pid}/current")
        assert c.status_code == 200
        assert c.json()["arc"] is None      # 旧项目无弧 → null，前端回退旧 wizard
    finally:
        await auth_client.delete(f"/api/projects/{pid}?purge=true")


@pytest.mark.asyncio
async def test_start_arc_rolls_back_on_outline_failure(auth_client, monkeypatch):
    import app.api.arc as arc_mod

    async def failed_outline(**kwargs):
        return {"available": False}

    monkeypatch.setattr(arc_mod, "generate_arc_outline", failed_outline)

    resp = await auth_client.post("/api/projects", json={"title": "弧失败", "genre": "x"})
    pid = resp.json()["id"]
    try:
        r = await auth_client.post(f"/api/arc/{pid}/start", json={
            "idea": "x", "background": "y", "core_setup": "z", "opening_scene": "w",
        })
        assert r.status_code == 502        # 大纲失败 → 不建半截 Volume
        # current 仍为 null（无残留）
        c = await auth_client.get(f"/api/arc/{pid}/current")
        assert c.json()["arc"] is None
    finally:
        await auth_client.delete(f"/api/projects/{pid}?purge=true")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/ai-write/backend && .venv/bin/python -m pytest tests/api/test_arc_api.py -k "start_arc or non_arc" -v`
Expected: FAIL (404 — route not registered / module missing)

- [ ] **Step 3: Write minimal implementation**

Create `backend/app/api/arc.py`:

```python
"""/api/arc — 弧式增量创作循环编排端点（子项目 A）。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.project import Outline, Project, Volume
from app.services.arc_loop import (
    ArcState,
    advance_arc_state,
    build_arc_completion_suggestions,
    build_next_chapter_brief,
    clamp_target_chapters,
    generate_arc_outline,
    parse_arc_state,
    serialize_arc_state,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/arc", tags=["arc"])


class StartArcBody(BaseModel):
    idea: str
    background: str = ""
    core_setup: str = ""
    opening_scene: str = ""
    target_chapters: int = 20


def _arc_dict(state: ArcState) -> dict:
    return {
        "title": state.title,
        "core_setup": state.core_setup,
        "opening_scene": state.opening_scene,
        "target_chapters": state.target_chapters,
        "status": state.status,
        "chapters_written": state.chapters_written,
        "running_outline": state.running_outline,
        "next_direction": state.next_direction,
        "suggestions": state.suggestions,
    }


async def _load_current_arc(db: AsyncSession, project_id: str):
    """返回 (volume, outline, ArcState) 三元组中最高 volume_idx 的活跃弧；无则 (None,None,None)。"""
    result = await db.execute(
        select(Outline).where(
            Outline.project_id == project_id,
            Outline.level == "volume",
        )
    )
    best = None
    for o in result.scalars().all():
        st = parse_arc_state(o.content_json)
        if st is None:
            continue
        vidx = int((o.content_json or {}).get("volume_idx") or 0)
        if best is None or vidx > best[0]:
            best = (vidx, o, st)
    if best is None:
        return None, None, None
    return best[0], best[1], best[2]


@router.post("/{project_id}/start", status_code=201)
async def start_arc(
    project_id: str,
    body: StartArcBody,
    db: AsyncSession = Depends(get_db),
) -> dict:
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    target = clamp_target_chapters(body.target_chapters)
    outline = await generate_arc_outline(
        idea=body.idea, background=body.background, core_setup=body.core_setup,
        opening_scene=body.opening_scene, target_chapters=target,
        db=db, project_id=project_id,
    )
    if not outline.get("available"):
        raise HTTPException(status_code=502, detail="Arc outline generation failed")

    volume_idx = 1
    volume = Volume(
        project_id=project_id,
        title=outline.get("title") or "第一弧",
        volume_idx=volume_idx,
        summary=body.core_setup,
    )
    db.add(volume)
    await db.flush()

    state = ArcState(
        title=outline.get("title") or "第一弧",
        core_setup=body.core_setup,
        opening_scene=body.opening_scene,
        target_chapters=target,
        status="active",
        chapters_written=0,
        running_outline="",
        next_direction=body.opening_scene or None,
    )
    content_json = serialize_arc_state(state, volume_idx=volume_idx)
    content_json["beats"] = outline.get("beats", [])
    ol = Outline(
        project_id=project_id, level="volume", content_json=content_json,
        is_confirmed=1,
    )
    db.add(ol)
    await db.flush()

    return {"volume_idx": volume_idx, "arc": _arc_dict(state)}


@router.get("/{project_id}/current")
async def current_arc(project_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    vidx, _ol, state = await _load_current_arc(db, project_id)
    if state is None:
        return {"arc": None}
    return {"volume_idx": vidx, "arc": _arc_dict(state)}
```

Register in `backend/app/main.py` after the other routers (near line 287):

```python
from app.api import arc as arc_api
app.include_router(arc_api.router)
```

(Put the import with the other `from app.api import ...` imports at the top, matching the file's existing import style, and the `include_router` line in the registration block.)

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/ai-write/backend && .venv/bin/python -m pytest tests/api/test_arc_api.py -k "start_arc or non_arc or rolls_back" -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
cd /root/ai-write && git add backend/app/api/arc.py backend/app/main.py backend/tests/api/test_arc_api.py
git commit -m "feat(arc): /start + /current endpoints (Volume+Outline arc, rollback on failure)"
```

---

## Task 7: arc API — `/next-direction` + `/chapter-brief`

**Files:**
- Modify: `backend/app/api/arc.py`
- Test: `backend/tests/api/test_arc_api.py`

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_next_direction_and_chapter_brief(auth_client, monkeypatch):
    import app.api.arc as arc_mod

    async def fake_outline(**kwargs):
        return {"available": True, "title": "边境御敌",
                "beats": [{"chapter": 1, "beat": "穿越"}, {"chapter": 2, "beat": "对峙"}]}

    monkeypatch.setattr(arc_mod, "generate_arc_outline", fake_outline)

    resp = await auth_client.post("/api/projects", json={"title": "弧方向", "genre": "x"})
    pid = resp.json()["id"]
    try:
        await auth_client.post(f"/api/arc/{pid}/start", json={
            "idea": "i", "background": "b", "core_setup": "c",
            "opening_scene": "o", "target_chapters": 20,
        })

        # 模拟已写 1 章 → awaiting_direction（直接打 chapter-written 推进）
        w = await auth_client.post(f"/api/arc/{pid}/chapter-written", json={
            "chapter_summary": "第1章：主角穿越遇挑衅。",
        })
        assert w.status_code == 200, w.text
        assert w.json()["arc"]["status"] == "awaiting_direction"
        assert w.json()["arc"]["chapters_written"] == 1

        # 作者给下一步方向
        d = await auth_client.post(f"/api/arc/{pid}/next-direction", json={
            "direction": "主角发现跑不了，狐假虎威",
        })
        assert d.status_code == 200
        assert d.json()["arc"]["status"] == "active"
        assert d.json()["arc"]["next_direction"] == "主角发现跑不了，狐假虎威"

        # chapter-brief 组装下一章 brief
        b = await auth_client.get(f"/api/arc/{pid}/chapter-brief")
        assert b.status_code == 200
        brief = b.json()["brief"]
        assert "边境御敌" in brief
        assert "狐假虎威" in brief
        assert "第1章：主角穿越" in brief
    finally:
        await auth_client.delete(f"/api/projects/{pid}?purge=true")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/ai-write/backend && .venv/bin/python -m pytest tests/api/test_arc_api.py -k next_direction -v`
Expected: FAIL (404 — endpoints not defined)

- [ ] **Step 3: Write minimal implementation**

Append to `backend/app/api/arc.py`:

```python
class ChapterWrittenBody(BaseModel):
    chapter_summary: str = ""


class NextDirectionBody(BaseModel):
    direction: str


async def _persist_arc(db: AsyncSession, outline: Outline, state: ArcState, volume_idx: int) -> None:
    content_json = serialize_arc_state(state, volume_idx=volume_idx)
    # 保留 beats（不在 ArcState 里）
    existing = outline.content_json or {}
    if "beats" in existing:
        content_json["beats"] = existing["beats"]
    outline.content_json = content_json
    # SQLAlchemy JSON 列就地改不脏标记，显式 flag
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(outline, "content_json")
    await db.flush()


@router.post("/{project_id}/chapter-written")
async def chapter_written(
    project_id: str,
    body: ChapterWrittenBody,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """章节写完后推进弧状态（chapters_written+1，更新 running_outline）。"""
    vidx, ol, state = await _load_current_arc(db, project_id)
    if state is None:
        raise HTTPException(status_code=404, detail="No active arc")
    new_state = advance_arc_state(
        state, event="chapter_written",
        running_outline_append=body.chapter_summary,
    )
    await _persist_arc(db, ol, new_state, vidx)
    return {"volume_idx": vidx, "arc": _arc_dict(new_state)}


@router.post("/{project_id}/next-direction")
async def next_direction(
    project_id: str,
    body: NextDirectionBody,
    db: AsyncSession = Depends(get_db),
) -> dict:
    vidx, ol, state = await _load_current_arc(db, project_id)
    if state is None:
        raise HTTPException(status_code=404, detail="No active arc")
    new_state = advance_arc_state(state, event="set_direction", next_direction=body.direction)
    await _persist_arc(db, ol, new_state, vidx)
    return {"volume_idx": vidx, "arc": _arc_dict(new_state)}


@router.get("/{project_id}/chapter-brief")
async def chapter_brief(project_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    vidx, ol, state = await _load_current_arc(db, project_id)
    if state is None:
        raise HTTPException(status_code=404, detail="No active arc")
    beats = (ol.content_json or {}).get("beats", [])
    brief = build_next_chapter_brief(state, arc_beats=beats)
    return {"volume_idx": vidx, "brief": brief, "next_chapter_idx": state.chapters_written + 1}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/ai-write/backend && .venv/bin/python -m pytest tests/api/test_arc_api.py -k next_direction -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /root/ai-write && git add backend/app/api/arc.py backend/tests/api/test_arc_api.py
git commit -m "feat(arc): /chapter-written + /next-direction + /chapter-brief endpoints"
```

---

## Task 8: arc API — `/complete` + `/next-arc`

**Files:**
- Modify: `backend/app/api/arc.py`
- Test: `backend/tests/api/test_arc_api.py`

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_complete_and_next_arc(auth_client, monkeypatch):
    import app.api.arc as arc_mod

    async def fake_outline(**kwargs):
        return {"available": True, "title": "弧一", "beats": []}

    async def fake_suggest(**kwargs):
        return ["进城拜师", "仇家追杀", "捡到秘籍"]

    monkeypatch.setattr(arc_mod, "generate_arc_outline", fake_outline)
    monkeypatch.setattr(arc_mod, "build_arc_completion_suggestions", fake_suggest)

    resp = await auth_client.post("/api/projects", json={"title": "弧完结", "genre": "x"})
    pid = resp.json()["id"]
    try:
        await auth_client.post(f"/api/arc/{pid}/start", json={
            "idea": "i", "background": "b", "core_setup": "c",
            "opening_scene": "o", "target_chapters": 2,
        })
        # 写满 2 章 → completed
        await auth_client.post(f"/api/arc/{pid}/chapter-written", json={"chapter_summary": "第1章"})
        w2 = await auth_client.post(f"/api/arc/{pid}/chapter-written", json={"chapter_summary": "第2章"})
        assert w2.json()["arc"]["status"] == "completed"

        # complete：生成下一弧建议
        comp = await auth_client.post(f"/api/arc/{pid}/complete")
        assert comp.status_code == 200, comp.text
        assert len(comp.json()["arc"]["suggestions"]) == 3

        # next-arc：建第二弧（volume_idx=2）
        n = await auth_client.post(f"/api/arc/{pid}/next-arc", json={
            "idea": "i2", "background": "b", "core_setup": "新威胁",
            "opening_scene": "进城遇贵人", "target_chapters": 20,
        })
        assert n.status_code == 201, n.text
        assert n.json()["volume_idx"] == 2
        assert n.json()["arc"]["status"] == "active"
    finally:
        await auth_client.delete(f"/api/projects/{pid}?purge=true")


@pytest.mark.asyncio
async def test_next_arc_blocked_when_current_not_completed(auth_client, monkeypatch):
    import app.api.arc as arc_mod

    async def fake_outline(**kwargs):
        return {"available": True, "title": "弧一", "beats": []}

    monkeypatch.setattr(arc_mod, "generate_arc_outline", fake_outline)

    resp = await auth_client.post("/api/projects", json={"title": "弧守卫", "genre": "x"})
    pid = resp.json()["id"]
    try:
        await auth_client.post(f"/api/arc/{pid}/start", json={
            "idea": "i", "background": "b", "core_setup": "c",
            "opening_scene": "o", "target_chapters": 20,
        })
        # 当前弧还 active，不许开下一弧
        n = await auth_client.post(f"/api/arc/{pid}/next-arc", json={
            "idea": "i2", "background": "b", "core_setup": "c2", "opening_scene": "o2",
        })
        assert n.status_code == 409
    finally:
        await auth_client.delete(f"/api/projects/{pid}?purge=true")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/ai-write/backend && .venv/bin/python -m pytest tests/api/test_arc_api.py -k "complete_and_next or blocked_when" -v`
Expected: FAIL (404 — endpoints not defined)

- [ ] **Step 3: Write minimal implementation**

Append to `backend/app/api/arc.py`:

```python
@router.post("/{project_id}/complete")
async def complete_arc(project_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    vidx, ol, state = await _load_current_arc(db, project_id)
    if state is None:
        raise HTTPException(status_code=404, detail="No active arc")
    project = await db.get(Project, project_id)
    background = ""
    if project and isinstance(project.settings_json, dict):
        background = str(project.settings_json.get("background") or "")
    suggestions = await build_arc_completion_suggestions(
        background=background, running_outline=state.running_outline,
        db=db, project_id=project_id,
    )
    state.status = "completed"
    state.suggestions = suggestions
    await _persist_arc(db, ol, state, vidx)
    return {"volume_idx": vidx, "arc": _arc_dict(state)}


@router.post("/{project_id}/next-arc", status_code=201)
async def next_arc(
    project_id: str,
    body: StartArcBody,
    db: AsyncSession = Depends(get_db),
) -> dict:
    vidx, _ol, state = await _load_current_arc(db, project_id)
    if state is not None and state.status != "completed":
        raise HTTPException(status_code=409, detail="Current arc not completed")

    target = clamp_target_chapters(body.target_chapters)
    outline = await generate_arc_outline(
        idea=body.idea, background=body.background, core_setup=body.core_setup,
        opening_scene=body.opening_scene, target_chapters=target,
        db=db, project_id=project_id,
    )
    if not outline.get("available"):
        raise HTTPException(status_code=502, detail="Arc outline generation failed")

    new_idx = (vidx or 0) + 1
    volume = Volume(
        project_id=project_id, title=outline.get("title") or f"第{new_idx}弧",
        volume_idx=new_idx, summary=body.core_setup,
    )
    db.add(volume)
    await db.flush()

    new_state = ArcState(
        title=outline.get("title") or f"第{new_idx}弧",
        core_setup=body.core_setup, opening_scene=body.opening_scene,
        target_chapters=target, status="active", chapters_written=0,
        running_outline="", next_direction=body.opening_scene or None,
    )
    content_json = serialize_arc_state(new_state, volume_idx=new_idx)
    content_json["beats"] = outline.get("beats", [])
    db.add(Outline(project_id=project_id, level="volume",
                   content_json=content_json, is_confirmed=1))
    await db.flush()
    return {"volume_idx": new_idx, "arc": _arc_dict(new_state)}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/ai-write/backend && .venv/bin/python -m pytest tests/api/test_arc_api.py -v`
Expected: PASS (all arc API tests)

- [ ] **Step 5: Commit**

```bash
cd /root/ai-write && git add backend/app/api/arc.py backend/tests/api/test_arc_api.py
git commit -m "feat(arc): /complete (suggestions) + /next-arc (volume_idx+1, completed-guard)"
```

---

## Task 9: 全套回归 + 部署 + 文档

**Files:** CHANGELOG.md / README.md / handoff doc

- [ ] **Step 1: Full backend suite**

Run: `cd /root/ai-write/backend && .venv/bin/python -m pytest`
Expected: all green (666 baseline + new arc_loop/arc_api tests). Pre-existing-unrelated failures: confirm they also fail on `git stash`.

- [ ] **Step 2: Frontend typecheck (unaffected)**

Run: `cd /root/ai-write/frontend && npx tsc --noEmit`
Expected: zero output (this subproject is backend-only).

- [ ] **Step 3: Deploy**

Run: `cd /root/ai-write && docker compose up -d --build backend`
Then verify live: `curl -s http://127.0.0.1:8000/api/health` → `{"status":"ok"}` and confirm arc router registered (`curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8000/api/arc/<bogus-uuid>/current` with auth → 200 with `{"arc": null}` or 404, not 404-route-missing).

- [ ] **Step 4: Docs**

Update `CHANGELOG.md` (new entry under `[1.9.3]` or bump), `README.md` (add 弧式增量创作 to feature list), and write `docs/HANDOFF_2026-06-26_arc-loop.md` recording the design, endpoints, reuse of B, zero-migration arc storage, and the philosophy constraint.

- [ ] **Step 5: Commit**

```bash
cd /root/ai-write && git add CHANGELOG.md README.md docs/HANDOFF_2026-06-26_arc-loop.md docs/superpowers/plans/2026-06-26-incremental-arc-writing-loop.md
git commit -m "docs(arc): record subproject A incremental arc writing loop"
```

---

## Self-Review (completed by plan author)

**Spec coverage:** kickoff 问题（Task 4）✓；小弧大纲生成 + 哲学约束（Task 3）✓；弧=Volume 复用 + _arc sidecar 零迁移（Task 1, 6）✓；逐章 brief 复用 B（Task 4 brief + 既有 `/api/generate/chapter`）✓；状态机 active/awaiting/completed（Task 2）✓；弧末建议 + 问下一弧（Task 4 + Task 8）✓；下一弧 volume_idx+1（Task 8）✓；旧项目 current=null 回退（Task 6）✓；next-arc completed 守卫 409（Task 8）✓；大纲失败回滚不建半截 Volume（Task 6）✓；task_type fallback（Task 5）✓；回归+部署+文档（Task 9）✓。

**Placeholder scan:** 无 TBD/TODO；每个 code step 含完整代码与精确命令。

**Type consistency:** `ArcState`(title/core_setup/opening_scene/target_chapters/status/chapters_written/running_outline/next_direction/suggestions)、`parse_arc_state`/`serialize_arc_state`/`advance_arc_state`/`clamp_target_chapters`/`generate_arc_outline`/`build_arc_outline_prompt`/`build_arc_kickoff_questions`/`build_arc_completion_suggestions`/`build_next_chapter_brief` 在 service 与 API 间签名一致。API 端点路径与测试 URL 逐一对齐。状态常量 `ARC_STATUS_ACTIVE/AWAITING/COMPLETED` 全程一致。`run_chapter_pipeline` 不改动（A 只产 brief，正文走既有 `/api/generate/chapter`）。
