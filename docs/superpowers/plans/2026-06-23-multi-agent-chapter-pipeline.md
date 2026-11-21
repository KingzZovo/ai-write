# 多智能体章节质量管线 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 在 `generate_chapter` 的质量环节前插入一个串行三角色管线（drafter→logic_critic→prose_polish），新增「逻辑与剧情核查」角色专查章内空间方向矛盾/画面重述/跨度突变，把终稿与精简报告 echo 回主流程而不污染上下文。

**Architecture:** 新增编排器 `chapter_pipeline.py` 与逻辑核查 `logic_critic.py` 两个模块。编排器串联：初稿 → logic_critic 隔离上下文核查 → drafter 定向改写（最多 2 轮、plateau 终止）→ 既有 `apply_chapter_quality_gate`（零改动，作第三棒）。任一棒失败降级、不丢整章。`CHAPTER_PIPELINE_ENABLED=0` 可一键回退到纯 quality_gate 老路径。

**Tech Stack:** Python / FastAPI / SQLAlchemy async / pytest + pytest-asyncio。LLM 调用走既有 `run_structured_prompt`（带 json_repair）与 `run_text_prompt`。

---

## 关键既有事实（实现者须知，已核对当前代码）

- `apply_chapter_quality_gate(*, text, db, project_id, chapter_id, target_word_count=None, skip_polish=False, ...) -> ChapterQualityGateResult`，定义于 `backend/app/services/chapter_quality_gate.py`。**本计划不改它的签名与内部**。
  - `ChapterQualityGateResult` 字段：`status`（`passed|skipped|improved_warning|rewrite_failed|blocked`）、`final_text`、`final_report`、`rewrite_rounds`、`warning_reason`、`to_safe_metadata()`、`final_report.to_safe_dict()`。
- `run_structured_prompt(task_type, user_content, db, extra_system="", project_id=None, chapter_id=None, **kwargs) -> dict`（`backend/app/services/prompt_registry.py:779`）。内部已带 json_repair + 一次 strict retry。无注册 prompt 时抛 `ValueError`。
- `run_text_prompt(task_type, user_content, db, extra_system="", project_id=None, chapter_id=None, **kwargs) -> GenerationResult`（`:705`），`result.text` 为正文字符串。
- `_TASK_TYPE_FALLBACK`（`prompt_registry.py:520`）把未注册 task_type 路由到既有 prompt。
- generate.py 调用点：`backend/app/api/generate.py:446` 处 `quality_gate_result = await apply_chapter_quality_gate(...)`，其上下文有局部变量 `full_text`（初稿）、`chapter_outline`（dict，:229）、`previous_text`（str，:230）、`target_words`、`req.project_id`、`req.chapter_id`、`req.skip_polish`。
- 测试惯例：`cd /root/ai-write/backend && .venv/bin/python -m pytest`。打 dev 真库的测试要带清理。`conftest.py` 在 import 前钉环境变量（已有 `CHAPTER_MAX_REWRITE_ROUNDS`）。
- 本计划所有新逻辑用 **mock LLM** 单测，不打真 LLM，不打真库（纯函数 + mock）。

---

## File Structure

| 文件 | 责任 | 新建/改动 |
|------|------|-----------|
| `backend/app/services/logic_critic.py` | LogicIssue/LogicCriticReport 数据结构、隔离 context 构造、结构化输出解析（含 unlocatable 标记）、`run_logic_critic` LLM 调用 + 降级 | 新建 |
| `backend/app/services/chapter_pipeline.py` | `ChapterPipelineResult`、定向改写 helper、`run_chapter_pipeline` 串行编排 + 逻辑回环 + plateau/封顶 + 开关降级 + echo 报告 | 新建 |
| `backend/app/services/prompt_registry.py` | `_TASK_TYPE_FALLBACK` 增 `logic_critic→critic`、`drafter→rewrite` | 改 1 处 |
| `backend/app/api/generate.py` | `:446` 调用点改调 `run_chapter_pipeline`；新增 `logic_critic_done` SSE 事件 | 改 1 段 |
| `backend/tests/conftest.py` | 钉 `LOGIC_CRITIC_MAX_ROUNDS=2`、`CHAPTER_PIPELINE_ENABLED=1`（测试确定性） | 改 |
| `backend/tests/services/test_logic_critic.py` | logic_critic 单测 | 新建 |
| `backend/tests/services/test_chapter_pipeline.py` | 编排器单测 | 新建 |

---

## Task 1: LogicIssue / LogicCriticReport 数据结构

**Files:**
- Create: `backend/app/services/logic_critic.py`
- Test: `backend/tests/services/test_logic_critic.py`

- [x] **Step 1: Write the failing test**

```python
# backend/tests/services/test_logic_critic.py
from __future__ import annotations


def test_logic_issue_and_report_shape() -> None:
    from app.services.logic_critic import LogicCriticReport, LogicIssue

    high = LogicIssue(
        dimension="spatial_direction",
        severity="high",
        quote="通道向地下深处倾斜延伸",
        problem="既写通向地面又写向地下延伸，方向矛盾",
        fix_hint="统一为逃向地面，删去往下跑",
        locatable=True,
    )
    low = LogicIssue(
        dimension="prop_state",
        severity="low",
        quote="手机还在手里",
        problem="",
        fix_hint="",
        locatable=False,
    )
    report = LogicCriticReport(available=True, clean=False, issues=[high, low])

    assert report.high_issues == [high]
    assert report.locatable_issues == [high]
    assert report.issue_count == 2
```

- [x] **Step 2: Run test to verify it fails**

Run: `cd /root/ai-write/backend && .venv/bin/python -m pytest tests/services/test_logic_critic.py::test_logic_issue_and_report_shape -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.logic_critic'`

- [x] **Step 3: Write minimal implementation**

```python
# backend/app/services/logic_critic.py
"""逻辑与剧情核查角色（章内语义级自洽审查）。

读完整章正文 + 本章大纲 + 紧邻前章末尾（隔离 context，不喂全书记忆），
专查现有 checker 漏掉的章内缺陷：空间方向矛盾、画面重述、跨度突变、
动作因果断裂、道具状态连续性。产出结构化 issue 清单供定向改写。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 五个检测维度（与 spec 一一对应）。
LOGIC_DIMENSIONS: tuple[str, ...] = (
    "spatial_direction",     # 空间方向一致性
    "scene_redescription",   # 画面重述/草稿叠写残留
    "span_jump",             # 空间/时间跨度突变
    "action_causality",      # 动作因果链断裂
    "prop_state",            # 道具/状态连续性
)


@dataclass(frozen=True)
class LogicIssue:
    dimension: str
    severity: str  # high|medium|low
    quote: str
    problem: str
    fix_hint: str
    locatable: bool = True


@dataclass
class LogicCriticReport:
    available: bool          # False = 核查不可用（LLM/解析失败）→ 降级
    clean: bool              # True = 无 issue
    issues: list[LogicIssue] = field(default_factory=list)

    @property
    def high_issues(self) -> list[LogicIssue]:
        return [i for i in self.issues if i.severity == "high"]

    @property
    def locatable_issues(self) -> list[LogicIssue]:
        return [i for i in self.issues if i.locatable]

    @property
    def issue_count(self) -> int:
        return len(self.issues)
```

- [x] **Step 4: Run test to verify it passes**

Run: `cd /root/ai-write/backend && .venv/bin/python -m pytest tests/services/test_logic_critic.py::test_logic_issue_and_report_shape -v`
Expected: PASS

- [x] **Step 5: Commit**

```bash
cd /root/ai-write && git add backend/app/services/logic_critic.py backend/tests/services/test_logic_critic.py
git commit -m "feat(logic_critic): LogicIssue/LogicCriticReport data structures"
```

---

## Task 2: 隔离 context 构造 `build_logic_critic_user_content`

**Files:**
- Modify: `backend/app/services/logic_critic.py`
- Test: `backend/tests/services/test_logic_critic.py`

- [x] **Step 1: Write the failing test**

```python
def test_build_user_content_is_isolated() -> None:
    from app.services.logic_critic import build_logic_critic_user_content

    content = build_logic_critic_user_content(
        chapter_text="林照推开门，看见骨架。",
        chapter_outline={"title": "逃生", "summary": "主角逃离五楼"},
        prev_chapter_tail="上一章结尾：他走进楼道。",
    )
    # 必须含本章正文、本章大纲、前章末尾三块。
    assert "林照推开门" in content
    assert "逃生" in content or "主角逃离五楼" in content
    assert "他走进楼道" in content
    # 必须列出五个检测维度的中文说明，引导模型。
    assert "空间方向" in content
    assert "画面重述" in content or "重述" in content
    # 必须要求结构化 JSON 输出（含 clean 字段）。
    assert "clean" in content
    assert "issues" in content


def test_build_user_content_tolerates_missing_optionals() -> None:
    from app.services.logic_critic import build_logic_critic_user_content

    content = build_logic_critic_user_content(
        chapter_text="正文。",
        chapter_outline=None,
        prev_chapter_tail="",
    )
    assert "正文。" in content
    assert "clean" in content
```

- [x] **Step 2: Run test to verify it fails**

Run: `cd /root/ai-write/backend && .venv/bin/python -m pytest tests/services/test_logic_critic.py -k build_user_content -v`
Expected: FAIL with `ImportError: cannot import name 'build_logic_critic_user_content'`

- [x] **Step 3: Write minimal implementation**

Append to `backend/app/services/logic_critic.py`:

```python
import json

_DIMENSION_GUIDE = """\
逐项核查以下五类「章内」缺陷（只看本章是否自洽，不评价剧情好坏）：
1. spatial_direction 空间方向一致性：同一段移动里方向词（上/下/进/出/前/后）与目标是否自洽。
   例：既写「通向地面层出口」「继续向上跑」，又写「往下跑」「向地下深处延伸」=矛盾。
2. scene_redescription 画面重述/草稿叠写残留：同一对象或场景在相邻段落被高相似度重复描写；
   二次出现应只保留新增信息，不该重描整幅静态画面与同一动作。
3. span_jump 空间/时间跨度突变：位置/楼层/时间出现无过渡跳变（A 点直接到 C 点，缺 B 衔接）。
   例：前文「才下去半层」，后文直接「踩上三楼平台」，中间缺衔接。
4. action_causality 动作因果链断裂：某动作的前置条件在文中未出现就直接发生。
5. prop_state 道具/状态连续性：同一道具或身体状态在本章前后矛盾。
"""

_OUTPUT_CONTRACT = """\
只输出一个 JSON 对象，不要任何解释、Markdown、代码块围栏：
{
  "issues": [
    {
      "dimension": "spatial_direction|scene_redescription|span_jump|action_causality|prop_state",
      "severity": "high|medium|low",
      "quote": "原文中的精确片段（必须能在正文里逐字找到，用于定位）",
      "problem": "一句话说明矛盾",
      "fix_hint": "一句话给出修法"
    }
  ],
  "clean": true 或 false
}
没有任何缺陷时返回 {"issues": [], "clean": true}。
quote 必须从正文原样摘录，不得改写或臆造。"""


def build_logic_critic_user_content(
    *,
    chapter_text: str,
    chapter_outline: dict | None,
    prev_chapter_tail: str,
) -> str:
    outline_block = ""
    if chapter_outline:
        try:
            outline_block = json.dumps(chapter_outline, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            outline_block = str(chapter_outline)
    parts = [_DIMENSION_GUIDE]
    if outline_block:
        parts.append(f"【本章大纲】\n{outline_block}")
    if prev_chapter_tail and prev_chapter_tail.strip():
        parts.append(f"【紧邻前章末尾（仅供衔接判断）】\n{prev_chapter_tail.strip()}")
    parts.append(f"【本章正文（核查对象）】\n{chapter_text}")
    parts.append(_OUTPUT_CONTRACT)
    return "\n\n".join(parts)
```

- [x] **Step 4: Run test to verify it passes**

Run: `cd /root/ai-write/backend && .venv/bin/python -m pytest tests/services/test_logic_critic.py -k build_user_content -v`
Expected: PASS (2 passed)

- [x] **Step 5: Commit**

```bash
cd /root/ai-write && git add backend/app/services/logic_critic.py backend/tests/services/test_logic_critic.py
git commit -m "feat(logic_critic): isolated-context user-content builder"
```

---

## Task 3: 解析结构化输出 `parse_logic_critic_output`（含 unlocatable 标记）

**Files:**
- Modify: `backend/app/services/logic_critic.py`
- Test: `backend/tests/services/test_logic_critic.py`

- [x] **Step 1: Write the failing test**

```python
def test_parse_clean_output() -> None:
    from app.services.logic_critic import parse_logic_critic_output

    report = parse_logic_critic_output({"issues": [], "clean": True}, chapter_text="任意正文")
    assert report.available is True
    assert report.clean is True
    assert report.issues == []


def test_parse_marks_unlocatable_quote() -> None:
    from app.services.logic_critic import parse_logic_critic_output

    chapter = "台阶向上倾斜，通向地面层出口。他迈开步子往下跑。"
    parsed = {
        "clean": False,
        "issues": [
            {
                "dimension": "spatial_direction",
                "severity": "high",
                "quote": "他迈开步子往下跑",          # 在正文里 → locatable
                "problem": "方向矛盾",
                "fix_hint": "删去往下跑",
            },
            {
                "dimension": "span_jump",
                "severity": "high",
                "quote": "他乘电梯直达顶楼",            # 不在正文里 → unlocatable
                "problem": "臆造",
                "fix_hint": "x",
            },
        ],
    }
    report = parse_logic_critic_output(parsed, chapter_text=chapter)
    assert report.available is True
    assert report.clean is False
    assert len(report.issues) == 2
    locatable = report.locatable_issues
    assert len(locatable) == 1
    assert locatable[0].quote == "他迈开步子往下跑"


def test_parse_clean_false_but_no_issues_is_clean() -> None:
    from app.services.logic_critic import parse_logic_critic_output

    # 防御：模型说 clean=false 却给空 issues → 视为 clean（无可修项）。
    report = parse_logic_critic_output({"issues": [], "clean": False}, chapter_text="正文")
    assert report.clean is True


def test_parse_garbage_returns_unavailable() -> None:
    from app.services.logic_critic import parse_logic_critic_output

    # 非法/空结构 → 核查不可用（触发降级）。
    assert parse_logic_critic_output({}, chapter_text="正文").available is False
    assert parse_logic_critic_output(None, chapter_text="正文").available is False
```

- [x] **Step 2: Run test to verify it fails**

Run: `cd /root/ai-write/backend && .venv/bin/python -m pytest tests/services/test_logic_critic.py -k parse -v`
Expected: FAIL with `ImportError: cannot import name 'parse_logic_critic_output'`

- [x] **Step 3: Write minimal implementation**

Append to `backend/app/services/logic_critic.py`:

```python
_VALID_SEVERITY = {"high", "medium", "low"}


def parse_logic_critic_output(parsed: object, *, chapter_text: str) -> LogicCriticReport:
    """把 run_structured_prompt 的 dict 解析为 LogicCriticReport。

    - 非 dict 或缺 issues 键 → available=False（核查不可用，触发降级）。
    - issues 为空 → clean=True（不论模型给的 clean 字段）。
    - 每个 issue 的 quote 若不在正文中 → locatable=False（臆造，不参与定向改写）。
    """
    if not isinstance(parsed, dict) or "issues" not in parsed:
        return LogicCriticReport(available=False, clean=False, issues=[])

    raw_issues = parsed.get("issues")
    if not isinstance(raw_issues, list):
        return LogicCriticReport(available=False, clean=False, issues=[])

    issues: list[LogicIssue] = []
    for raw in raw_issues:
        if not isinstance(raw, dict):
            continue
        dimension = str(raw.get("dimension") or "").strip()
        if dimension not in LOGIC_DIMENSIONS:
            dimension = "action_causality"  # 兜底归一，避免丢弃可用诊断
        severity = str(raw.get("severity") or "medium").strip().lower()
        if severity not in _VALID_SEVERITY:
            severity = "medium"
        quote = str(raw.get("quote") or "").strip()
        locatable = bool(quote) and quote in chapter_text
        issues.append(
            LogicIssue(
                dimension=dimension,
                severity=severity,
                quote=quote,
                problem=str(raw.get("problem") or "").strip(),
                fix_hint=str(raw.get("fix_hint") or "").strip(),
                locatable=locatable,
            )
        )

    clean = len(issues) == 0
    return LogicCriticReport(available=True, clean=clean, issues=issues)
```

- [x] **Step 4: Run test to verify it passes**

Run: `cd /root/ai-write/backend && .venv/bin/python -m pytest tests/services/test_logic_critic.py -k parse -v`
Expected: PASS (4 passed)

- [x] **Step 5: Commit**

```bash
cd /root/ai-write && git add backend/app/services/logic_critic.py backend/tests/services/test_logic_critic.py
git commit -m "feat(logic_critic): structured-output parser with unlocatable-quote marking"
```

---

## Task 4: `run_logic_critic` LLM 调用 + 降级

**Files:**
- Modify: `backend/app/services/logic_critic.py`
- Test: `backend/tests/services/test_logic_critic.py`

- [x] **Step 1: Write the failing test**

```python
import pytest


@pytest.mark.asyncio
async def test_run_logic_critic_happy_path(monkeypatch) -> None:
    import app.services.logic_critic as lc

    async def fake_structured(task_type, user_content, db, **kwargs):
        assert task_type == "logic_critic"
        assert "本章正文" in user_content
        return {
            "clean": False,
            "issues": [
                {
                    "dimension": "spatial_direction",
                    "severity": "high",
                    "quote": "往下跑",
                    "problem": "方向矛盾",
                    "fix_hint": "删去",
                }
            ],
        }

    monkeypatch.setattr(lc, "run_structured_prompt", fake_structured)

    report = await lc.run_logic_critic(
        chapter_text="他往下跑。",
        chapter_outline=None,
        prev_chapter_tail="",
        db=object(),
        project_id="p",
        chapter_id="c",
    )
    assert report.available is True
    assert report.high_issues[0].dimension == "spatial_direction"


@pytest.mark.asyncio
async def test_run_logic_critic_degrades_on_exception(monkeypatch) -> None:
    import app.services.logic_critic as lc

    async def boom(*a, **k):
        raise RuntimeError("relay 503")

    monkeypatch.setattr(lc, "run_structured_prompt", boom)

    report = await lc.run_logic_critic(
        chapter_text="正文",
        chapter_outline=None,
        prev_chapter_tail="",
        db=object(),
        project_id="p",
        chapter_id="c",
    )
    # 异常 → 不可用 → 降级，绝不抛出。
    assert report.available is False


@pytest.mark.asyncio
async def test_run_logic_critic_skips_short_text(monkeypatch) -> None:
    import app.services.logic_critic as lc

    called = False

    async def tracker(*a, **k):
        nonlocal called
        called = True
        return {"issues": [], "clean": True}

    monkeypatch.setattr(lc, "run_structured_prompt", tracker)

    report = await lc.run_logic_critic(
        chapter_text="太短",
        chapter_outline=None,
        prev_chapter_tail="",
        db=object(),
        project_id="p",
        chapter_id="c",
    )
    # 超短稿跳过核查（无意义），不调 LLM，返回 clean+available。
    assert called is False
    assert report.available is True
    assert report.clean is True
```

- [x] **Step 2: Run test to verify it fails**

Run: `cd /root/ai-write/backend && .venv/bin/python -m pytest tests/services/test_logic_critic.py -k run_logic_critic -v`
Expected: FAIL with `AttributeError: module 'app.services.logic_critic' has no attribute 'run_logic_critic'`

- [x] **Step 3: Write minimal implementation**

Append to `backend/app/services/logic_critic.py` (add the import near the top imports too):

```python
import logging

from app.services.prompt_registry import run_structured_prompt

logger = logging.getLogger(__name__)

_MIN_LOGIC_CHARS = 200  # 短于此跳过核查（无意义，省一次限流调用）


async def run_logic_critic(
    *,
    chapter_text: str,
    chapter_outline: dict | None,
    prev_chapter_tail: str,
    db: object,
    project_id: object = None,
    chapter_id: object = None,
) -> LogicCriticReport:
    """跑一次逻辑核查。任何失败都降级为 available=False，绝不抛出。"""
    if not chapter_text or len(chapter_text.strip()) < _MIN_LOGIC_CHARS:
        # 超短稿无核查意义，视作干净（不消耗 LLM 调用）。
        return LogicCriticReport(available=True, clean=True, issues=[])

    user_content = build_logic_critic_user_content(
        chapter_text=chapter_text,
        chapter_outline=chapter_outline,
        prev_chapter_tail=prev_chapter_tail,
    )
    try:
        parsed = await run_structured_prompt(
            "logic_critic",
            user_content,
            db,
            project_id=project_id,
            chapter_id=chapter_id,
        )
    except Exception as exc:  # noqa: BLE001 — 任何 relay/路由/解析失败都降级
        logger.warning("logic_critic LLM call failed; degrading: %s", exc)
        return LogicCriticReport(available=False, clean=False, issues=[])

    return parse_logic_critic_output(parsed, chapter_text=chapter_text)
```

Note: `run_structured_prompt` is patched per-test via `monkeypatch.setattr(lc, "run_structured_prompt", ...)`, which works because it is imported into the module namespace here.

- [x] **Step 4: Run test to verify it passes**

Run: `cd /root/ai-write/backend && .venv/bin/python -m pytest tests/services/test_logic_critic.py -v`
Expected: PASS (all logic_critic tests green)

- [x] **Step 5: Commit**

```bash
cd /root/ai-write && git add backend/app/services/logic_critic.py backend/tests/services/test_logic_critic.py
git commit -m "feat(logic_critic): run_logic_critic with rate-limit degradation + short-text skip"
```

---

## Task 5: task_type fallback 注册（logic_critic→critic, drafter→rewrite）

**Files:**
- Modify: `backend/app/services/prompt_registry.py:520`
- Test: `backend/tests/services/test_logic_critic.py`

- [x] **Step 1: Write the failing test**

```python
def test_task_type_fallbacks_registered() -> None:
    from app.services.prompt_registry import _TASK_TYPE_FALLBACK

    # 未注册 PromptAsset 时，新角色应回退到既有 prompt，开箱即用。
    assert _TASK_TYPE_FALLBACK.get("logic_critic") == "critic"
    assert _TASK_TYPE_FALLBACK.get("drafter") == "rewrite"
```

- [x] **Step 2: Run test to verify it fails**

Run: `cd /root/ai-write/backend && .venv/bin/python -m pytest tests/services/test_logic_critic.py::test_task_type_fallbacks_registered -v`
Expected: FAIL with `AssertionError: assert None == 'critic'`

- [x] **Step 3: Write minimal implementation**

In `backend/app/services/prompt_registry.py`, locate the `_TASK_TYPE_FALLBACK` dict (around line 520) and add two entries:

```python
_TASK_TYPE_FALLBACK: dict[str, str] = {
    "critic_hard": "critic",
    "critic_soft": "critic",
    "consistency_llm_check": "critic",
    "rag_query_rewrite": "extraction",
    "characters_extraction": "extraction",
    "world_rules_extraction": "extraction",
    "relationships_extraction": "extraction",
    # Multi-agent chapter pipeline (subproject B): degrade to existing
    # prompts when no dedicated PromptAsset is configured.
    "logic_critic": "critic",
    "drafter": "rewrite",
}
```

- [x] **Step 4: Run test to verify it passes**

Run: `cd /root/ai-write/backend && .venv/bin/python -m pytest tests/services/test_logic_critic.py::test_task_type_fallbacks_registered -v`
Expected: PASS

- [x] **Step 5: Commit**

```bash
cd /root/ai-write && git add backend/app/services/prompt_registry.py backend/tests/services/test_logic_critic.py
git commit -m "feat(pipeline): register logic_critic/drafter task_type fallbacks"
```

---

## Task 6: 定向改写 helper `apply_targeted_logic_rewrite`

**Files:**
- Create: `backend/app/services/chapter_pipeline.py`
- Test: `backend/tests/services/test_chapter_pipeline.py`

- [x] **Step 1: Write the failing test**

```python
# backend/tests/services/test_chapter_pipeline.py
from __future__ import annotations

import pytest

from app.services.logic_critic import LogicIssue


def test_build_targeted_rewrite_content_lists_only_locatable() -> None:
    from app.services.chapter_pipeline import build_targeted_rewrite_content

    issues = [
        LogicIssue("spatial_direction", "high", "往下跑", "方向矛盾", "删去往下跑", True),
        LogicIssue("span_jump", "high", "臆造片段", "x", "y", False),  # 不该出现
    ]
    content = build_targeted_rewrite_content("原文正文……往下跑……", issues)
    assert "往下跑" in content
    assert "删去往下跑" in content
    assert "臆造片段" not in content   # unlocatable 不进改写指令
    assert "原文正文" in content        # 含被改全文


@pytest.mark.asyncio
async def test_apply_targeted_rewrite_returns_text(monkeypatch) -> None:
    import app.services.chapter_pipeline as cp
    from types import SimpleNamespace

    async def fake_text_prompt(task_type, user_content, db, **kwargs):
        assert task_type == "drafter"
        return SimpleNamespace(text="改写后的正文")

    monkeypatch.setattr(cp, "run_text_prompt", fake_text_prompt)

    issues = [LogicIssue("spatial_direction", "high", "往下跑", "矛盾", "删", True)]
    out = await cp.apply_targeted_logic_rewrite(
        text="原文往下跑", issues=issues, db=object(), project_id="p", chapter_id="c"
    )
    assert out == "改写后的正文"


@pytest.mark.asyncio
async def test_apply_targeted_rewrite_degrades_to_none(monkeypatch) -> None:
    import app.services.chapter_pipeline as cp

    async def boom(*a, **k):
        raise RuntimeError("relay down")

    monkeypatch.setattr(cp, "run_text_prompt", boom)

    issues = [LogicIssue("spatial_direction", "high", "往下跑", "矛盾", "删", True)]
    out = await cp.apply_targeted_logic_rewrite(
        text="原文", issues=issues, db=object(), project_id="p", chapter_id="c"
    )
    assert out is None  # 失败返回 None（保留上一稿）
```

- [x] **Step 2: Run test to verify it fails**

Run: `cd /root/ai-write/backend && .venv/bin/python -m pytest tests/services/test_chapter_pipeline.py -k targeted -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.chapter_pipeline'`

- [x] **Step 3: Write minimal implementation**

```python
# backend/app/services/chapter_pipeline.py
"""串行三角色章节质量管线编排器（子项目 B）。

drafter（已出初稿）→ logic_critic 隔离核查 → drafter 定向改写（最多 N 轮、
plateau 终止）→ apply_chapter_quality_gate（第三棒，零改动）。任一棒失败降级，
不丢整章。CHAPTER_PIPELINE_ENABLED=0 一键回退纯 quality_gate 老路径。
"""

from __future__ import annotations

import logging
import os

from app.services.logic_critic import LogicIssue
from app.services.prompt_registry import run_text_prompt

logger = logging.getLogger(__name__)


def build_targeted_rewrite_content(text: str, issues: list[LogicIssue]) -> str:
    """构造定向改写指令：只列 locatable issue，要求只动命中处。"""
    locatable = [i for i in issues if i.locatable]
    lines = [
        "下面这段中文小说正文存在若干「章内逻辑/空间」缺陷。",
        "只修复下列明确点名的问题，逐字定位到引用片段附近改写；",
        "不要改写其他段落，不要改变事件顺序、人物关系与核心信息，字数不要明显缩短。",
        "不要输出解释、分析、标题、Markdown 或代码块，只输出修订后的完整正文。",
        "",
        "【待修复问题】",
    ]
    for idx, issue in enumerate(locatable, 1):
        lines.append(
            f"{idx}. [{issue.dimension}] 引用：「{issue.quote}」｜问题：{issue.problem}｜修法：{issue.fix_hint}"
        )
    lines.append("")
    lines.append("【待修订正文】")
    lines.append(text)
    return "\n".join(lines)


async def apply_targeted_logic_rewrite(
    *,
    text: str,
    issues: list[LogicIssue],
    db: object,
    project_id: object = None,
    chapter_id: object = None,
) -> str | None:
    """调 drafter 做定向改写。失败返回 None（调用方保留上一稿）。"""
    if not any(i.locatable for i in issues):
        return None
    user_content = build_targeted_rewrite_content(text, issues)
    try:
        result = await run_text_prompt(
            "drafter",
            user_content,
            db,
            project_id=project_id,
            chapter_id=chapter_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("targeted logic rewrite failed; keeping prior draft: %s", exc)
        return None
    candidate = (result.text or "").strip()
    return candidate or None
```

- [x] **Step 4: Run test to verify it passes**

Run: `cd /root/ai-write/backend && .venv/bin/python -m pytest tests/services/test_chapter_pipeline.py -k targeted -v`
Expected: PASS (3 passed)

- [x] **Step 5: Commit**

```bash
cd /root/ai-write && git add backend/app/services/chapter_pipeline.py backend/tests/services/test_chapter_pipeline.py
git commit -m "feat(pipeline): targeted logic-rewrite helper (locatable-only, degrades to None)"
```

---

## Task 7: `ChapterPipelineResult` + echo 报告

**Files:**
- Modify: `backend/app/services/chapter_pipeline.py`
- Test: `backend/tests/services/test_chapter_pipeline.py`

- [x] **Step 1: Write the failing test**

```python
def test_pipeline_result_echo_report() -> None:
    from app.services.chapter_pipeline import ChapterPipelineResult
    from types import SimpleNamespace

    qg = SimpleNamespace(status="passed", final_text="终稿", to_safe_metadata=lambda: {"status": "passed"})
    res = ChapterPipelineResult(
        final_text="终稿",
        quality_gate_result=qg,
        logic_rounds=1,
        logic_issues_remaining=0,
        logic_available=True,
    )
    echo = res.to_echo_report()
    # echo 只含约定字段，不含中间稿/角色推理。
    assert echo == {
        "logic_rounds": 1,
        "logic_issues_remaining": 0,
        "logic_available": True,
        "prose_gate_status": "passed",
    }
    assert "intermediate_text" not in echo
    assert "issues" not in echo
```

- [x] **Step 2: Run test to verify it fails**

Run: `cd /root/ai-write/backend && .venv/bin/python -m pytest tests/services/test_chapter_pipeline.py -k echo -v`
Expected: FAIL with `ImportError: cannot import name 'ChapterPipelineResult'`

- [x] **Step 3: Write minimal implementation**

Append to `backend/app/services/chapter_pipeline.py` (add `dataclass` import + `TYPE_CHECKING`):

```python
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.services.chapter_quality_gate import ChapterQualityGateResult


@dataclass
class ChapterPipelineResult:
    final_text: str
    quality_gate_result: "ChapterQualityGateResult | Any"
    logic_rounds: int
    logic_issues_remaining: int
    logic_available: bool

    def to_echo_report(self) -> dict:
        """不污染主流程的精简报告：只回约定字段。"""
        return {
            "logic_rounds": self.logic_rounds,
            "logic_issues_remaining": self.logic_issues_remaining,
            "logic_available": self.logic_available,
            "prose_gate_status": getattr(self.quality_gate_result, "status", "unknown"),
        }
```

- [x] **Step 4: Run test to verify it passes**

Run: `cd /root/ai-write/backend && .venv/bin/python -m pytest tests/services/test_chapter_pipeline.py -k echo -v`
Expected: PASS

- [x] **Step 5: Commit**

```bash
cd /root/ai-write && git add backend/app/services/chapter_pipeline.py backend/tests/services/test_chapter_pipeline.py
git commit -m "feat(pipeline): ChapterPipelineResult + sanitized echo report"
```

---

## Task 8: `run_chapter_pipeline` 编排 — 开关降级路径

**Files:**
- Modify: `backend/app/services/chapter_pipeline.py`
- Test: `backend/tests/services/test_chapter_pipeline.py`

- [x] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_pipeline_disabled_delegates_to_quality_gate(monkeypatch) -> None:
    import app.services.chapter_pipeline as cp
    from types import SimpleNamespace

    monkeypatch.setenv("CHAPTER_PIPELINE_ENABLED", "0")

    qg = SimpleNamespace(status="passed", final_text="QG终稿",
                         to_safe_metadata=lambda: {"status": "passed"})
    seen = {}

    async def fake_gate(**kwargs):
        seen.update(kwargs)
        return qg

    monkeypatch.setattr(cp, "apply_chapter_quality_gate", fake_gate)

    # logic_critic 不应被调用（开关关闭）。
    async def must_not_call(*a, **k):
        raise AssertionError("logic_critic must not run when pipeline disabled")

    monkeypatch.setattr(cp, "run_logic_critic", must_not_call)

    res = await cp.run_chapter_pipeline(
        text="初稿正文", db=object(), project_id="p", chapter_id="c",
        target_word_count=3000, chapter_outline=None, prev_chapter_tail="",
    )
    assert res.final_text == "QG终稿"
    assert res.logic_available is False
    assert res.logic_rounds == 0
    assert seen["text"] == "初稿正文"
    assert seen["target_word_count"] == 3000
```

- [x] **Step 2: Run test to verify it fails**

Run: `cd /root/ai-write/backend && .venv/bin/python -m pytest tests/services/test_chapter_pipeline.py -k disabled -v`
Expected: FAIL with `AttributeError: ... has no attribute 'run_chapter_pipeline'`

- [x] **Step 3: Write minimal implementation**

Append to `backend/app/services/chapter_pipeline.py` (add imports for the gate + critic into module namespace so tests can monkeypatch them):

```python
from app.services.chapter_quality_gate import apply_chapter_quality_gate
from app.services.logic_critic import run_logic_critic


def _pipeline_enabled() -> bool:
    return os.getenv("CHAPTER_PIPELINE_ENABLED", "1") != "0"


def _max_logic_rounds() -> int:
    return int(os.getenv("LOGIC_CRITIC_MAX_ROUNDS", "2"))


async def run_chapter_pipeline(
    *,
    text: str,
    db: object,
    project_id: object = None,
    chapter_id: object = None,
    target_word_count: int | None = None,
    chapter_outline: dict | None = None,
    prev_chapter_tail: str = "",
    skip_polish: bool = False,
) -> ChapterPipelineResult:
    """串行三角色管线。开关关闭时等价于直调 apply_chapter_quality_gate。"""
    if not _pipeline_enabled():
        qg = await apply_chapter_quality_gate(
            text=text,
            db=db,
            project_id=project_id,
            chapter_id=chapter_id,
            target_word_count=target_word_count,
            skip_polish=skip_polish,
        )
        return ChapterPipelineResult(
            final_text=qg.final_text,
            quality_gate_result=qg,
            logic_rounds=0,
            logic_issues_remaining=0,
            logic_available=False,
        )

    # 完整管线在 Task 9 实现；此处先占位最小可跑路径（仅第三棒），
    # Task 9 会插入 logic 回环。
    qg = await apply_chapter_quality_gate(
        text=text,
        db=db,
        project_id=project_id,
        chapter_id=chapter_id,
        target_word_count=target_word_count,
        skip_polish=skip_polish,
    )
    return ChapterPipelineResult(
        final_text=qg.final_text,
        quality_gate_result=qg,
        logic_rounds=0,
        logic_issues_remaining=0,
        logic_available=False,
    )
```

Note: `apply_chapter_quality_gate` and `run_logic_critic` are imported at module level so `monkeypatch.setattr(cp, "...", ...)` works in tests.

- [x] **Step 4: Run test to verify it passes**

Run: `cd /root/ai-write/backend && .venv/bin/python -m pytest tests/services/test_chapter_pipeline.py -k disabled -v`
Expected: PASS

- [x] **Step 5: Commit**

```bash
cd /root/ai-write && git add backend/app/services/chapter_pipeline.py backend/tests/services/test_chapter_pipeline.py
git commit -m "feat(pipeline): run_chapter_pipeline skeleton with CHAPTER_PIPELINE_ENABLED bypass"
```

---

## Task 9: `run_chapter_pipeline` 逻辑回环（clean 快路径 / 定向改写 / plateau / 封顶 / 降级）

**Files:**
- Modify: `backend/app/services/chapter_pipeline.py`
- Test: `backend/tests/services/test_chapter_pipeline.py`

- [x] **Step 1: Write the failing tests**

```python
def _qg(final_text="终稿", status="passed"):
    from types import SimpleNamespace
    return SimpleNamespace(status=status, final_text=final_text,
                           to_safe_metadata=lambda: {"status": status})


@pytest.mark.asyncio
async def test_clean_draft_skips_rewrite(monkeypatch) -> None:
    import app.services.chapter_pipeline as cp
    from app.services.logic_critic import LogicCriticReport

    monkeypatch.setenv("CHAPTER_PIPELINE_ENABLED", "1")
    monkeypatch.setenv("LOGIC_CRITIC_MAX_ROUNDS", "2")

    async def clean_critic(**k):
        return LogicCriticReport(available=True, clean=True, issues=[])

    rewrite_calls = 0

    async def count_rewrite(**k):
        nonlocal rewrite_calls
        rewrite_calls += 1
        return "不该被调用"

    async def gate(**k):
        return _qg(final_text=k["text"])

    monkeypatch.setattr(cp, "run_logic_critic", clean_critic)
    monkeypatch.setattr(cp, "apply_targeted_logic_rewrite", count_rewrite)
    monkeypatch.setattr(cp, "apply_chapter_quality_gate", gate)

    res = await cp.run_chapter_pipeline(
        text="x" * 500, db=object(), project_id="p", chapter_id="c",
    )
    assert rewrite_calls == 0           # clean → 0 次改写
    assert res.logic_rounds == 0
    assert res.logic_issues_remaining == 0
    assert res.logic_available is True


@pytest.mark.asyncio
async def test_high_issue_rewrites_then_verifies_clean(monkeypatch) -> None:
    import app.services.chapter_pipeline as cp
    from app.services.logic_critic import LogicCriticReport, LogicIssue

    monkeypatch.setenv("CHAPTER_PIPELINE_ENABLED", "1")
    monkeypatch.setenv("LOGIC_CRITIC_MAX_ROUNDS", "2")

    issue = LogicIssue("spatial_direction", "high", "往下跑", "矛盾", "删", True)
    seq = [
        LogicCriticReport(available=True, clean=False, issues=[issue]),  # round1
        LogicCriticReport(available=True, clean=True, issues=[]),        # round2 verify
    ]

    async def critic(**k):
        return seq.pop(0)

    async def rewrite(**k):
        return "改写后正文" + "y" * 500

    async def gate(**k):
        return _qg(final_text=k["text"])

    monkeypatch.setattr(cp, "run_logic_critic", critic)
    monkeypatch.setattr(cp, "apply_targeted_logic_rewrite", rewrite)
    monkeypatch.setattr(cp, "apply_chapter_quality_gate", gate)

    res = await cp.run_chapter_pipeline(
        text="x" * 500, db=object(), project_id="p", chapter_id="c",
    )
    assert res.logic_rounds == 1
    assert res.logic_issues_remaining == 0
    assert res.final_text.startswith("改写后正文")   # 第三棒收到改写稿


@pytest.mark.asyncio
async def test_plateau_stops_loop(monkeypatch) -> None:
    import app.services.chapter_pipeline as cp
    from app.services.logic_critic import LogicCriticReport, LogicIssue

    monkeypatch.setenv("CHAPTER_PIPELINE_ENABLED", "1")
    monkeypatch.setenv("LOGIC_CRITIC_MAX_ROUNDS", "3")

    issue = LogicIssue("span_jump", "high", "跨度", "突变", "补衔接", True)
    # 每轮都返回同样 1 个 high issue（无改善）。
    async def critic(**k):
        return LogicCriticReport(available=True, clean=False, issues=[issue])

    rewrite_calls = 0

    async def rewrite(**k):
        nonlocal rewrite_calls
        rewrite_calls += 1
        return "改" + "z" * 500

    async def gate(**k):
        return _qg(final_text=k["text"])

    monkeypatch.setattr(cp, "run_logic_critic", critic)
    monkeypatch.setattr(cp, "apply_targeted_logic_rewrite", rewrite)
    monkeypatch.setattr(cp, "apply_chapter_quality_gate", gate)

    res = await cp.run_chapter_pipeline(
        text="x" * 500, db=object(), project_id="p", chapter_id="c",
    )
    # round1 改写一次；round2 critic 发现没改善（issue 数 1 >= 1）→ plateau 停。
    assert rewrite_calls == 1
    assert res.logic_issues_remaining == 1


@pytest.mark.asyncio
async def test_critic_unavailable_degrades(monkeypatch) -> None:
    import app.services.chapter_pipeline as cp
    from app.services.logic_critic import LogicCriticReport

    monkeypatch.setenv("CHAPTER_PIPELINE_ENABLED", "1")

    async def down_critic(**k):
        return LogicCriticReport(available=False, clean=False, issues=[])

    async def rewrite(**k):
        raise AssertionError("must not rewrite when critic unavailable")

    async def gate(**k):
        return _qg(final_text=k["text"])

    monkeypatch.setattr(cp, "run_logic_critic", down_critic)
    monkeypatch.setattr(cp, "apply_targeted_logic_rewrite", rewrite)
    monkeypatch.setattr(cp, "apply_chapter_quality_gate", gate)

    res = await cp.run_chapter_pipeline(
        text="x" * 500, db=object(), project_id="p", chapter_id="c",
    )
    assert res.logic_available is False
    assert res.logic_rounds == 0
    assert res.final_text == "x" * 500   # 初稿原样进第三棒


@pytest.mark.asyncio
async def test_max_rounds_cap(monkeypatch) -> None:
    import app.services.chapter_pipeline as cp
    from app.services.logic_critic import LogicCriticReport, LogicIssue

    monkeypatch.setenv("CHAPTER_PIPELINE_ENABLED", "1")
    monkeypatch.setenv("LOGIC_CRITIC_MAX_ROUNDS", "2")

    # 每轮 issue 数递减（绕过 plateau），强制走到封顶。
    reports = [
        LogicCriticReport(available=True, clean=False, issues=[
            LogicIssue("span_jump", "high", f"q{n}", "p", "f", True) for n in range(k)
        ]) for k in (3, 2, 1)
    ]

    async def critic(**k):
        return reports.pop(0)

    rewrite_calls = 0

    async def rewrite(**k):
        nonlocal rewrite_calls
        rewrite_calls += 1
        return "改" + "w" * 500

    async def gate(**k):
        return _qg(final_text=k["text"])

    monkeypatch.setattr(cp, "run_logic_critic", critic)
    monkeypatch.setattr(cp, "apply_targeted_logic_rewrite", rewrite)
    monkeypatch.setattr(cp, "apply_chapter_quality_gate", gate)

    res = await cp.run_chapter_pipeline(
        text="x" * 500, db=object(), project_id="p", chapter_id="c",
    )
    # MAX=2：round1 critic(3)+rewrite, round2 critic(2)+rewrite, 封顶停。
    assert rewrite_calls == 2
    assert res.logic_rounds == 2
```

- [x] **Step 2: Run test to verify it fails**

Run: `cd /root/ai-write/backend && .venv/bin/python -m pytest tests/services/test_chapter_pipeline.py -k "clean_draft or high_issue or plateau or unavailable or max_rounds" -v`
Expected: FAIL (current skeleton ignores logic loop; `logic_rounds`/`final_text` assertions fail)

- [x] **Step 3: Write the implementation**

Replace the body of `run_chapter_pipeline` (the part after the `if not _pipeline_enabled()` block) in `backend/app/services/chapter_pipeline.py` with the full logic loop:

```python
async def run_chapter_pipeline(
    *,
    text: str,
    db: object,
    project_id: object = None,
    chapter_id: object = None,
    target_word_count: int | None = None,
    chapter_outline: dict | None = None,
    prev_chapter_tail: str = "",
    skip_polish: bool = False,
) -> ChapterPipelineResult:
    """串行三角色管线。开关关闭时等价于直调 apply_chapter_quality_gate。"""
    if not _pipeline_enabled():
        qg = await apply_chapter_quality_gate(
            text=text, db=db, project_id=project_id, chapter_id=chapter_id,
            target_word_count=target_word_count, skip_polish=skip_polish,
        )
        return ChapterPipelineResult(
            final_text=qg.final_text, quality_gate_result=qg,
            logic_rounds=0, logic_issues_remaining=0, logic_available=False,
        )

    current_text = text
    logic_rounds = 0
    logic_available = True
    issues_remaining = 0
    prev_high = None
    max_rounds = _max_logic_rounds()

    for _round in range(1, max(0, max_rounds) + 1):
        report = await run_logic_critic(
            chapter_text=current_text,
            chapter_outline=chapter_outline,
            prev_chapter_tail=prev_chapter_tail,
            db=db, project_id=project_id, chapter_id=chapter_id,
        )
        if not report.available:
            logic_available = False
            break
        high = report.high_issues
        issues_remaining = len(high)
        if report.clean or not high:
            break
        cur = len(high)
        if prev_high is not None and cur >= prev_high:
            break  # plateau：无改善，停止空耗
        rewritten = await apply_targeted_logic_rewrite(
            text=current_text, issues=report.locatable_issues,
            db=db, project_id=project_id, chapter_id=chapter_id,
        )
        if rewritten is None:
            break  # 改写失败：保留上一稿
        current_text = rewritten
        logic_rounds += 1
        prev_high = cur

    qg = await apply_chapter_quality_gate(
        text=current_text, db=db, project_id=project_id, chapter_id=chapter_id,
        target_word_count=target_word_count, skip_polish=skip_polish,
    )
    return ChapterPipelineResult(
        final_text=qg.final_text,
        quality_gate_result=qg,
        logic_rounds=logic_rounds,
        logic_issues_remaining=issues_remaining,
        logic_available=logic_available,
    )
```

Remove the Task-8 placeholder duplicate (the second `apply_chapter_quality_gate` block that followed the comment "完整管线在 Task 9 实现"). There must be exactly one `run_chapter_pipeline` definition.

- [x] **Step 4: Run tests to verify they pass**

Run: `cd /root/ai-write/backend && .venv/bin/python -m pytest tests/services/test_chapter_pipeline.py -v`
Expected: PASS (all pipeline tests, including the Task-8 disabled test)

- [x] **Step 5: Commit**

```bash
cd /root/ai-write && git add backend/app/services/chapter_pipeline.py backend/tests/services/test_chapter_pipeline.py
git commit -m "feat(pipeline): logic loop — clean fast-path, targeted rewrite, plateau, round cap, degradation"
```

---

## Task 10: conftest 钉环境变量（测试确定性）

**Files:**
- Modify: `backend/tests/conftest.py`

- [x] **Step 1: Write the failing test**

```python
# Append to backend/tests/services/test_chapter_pipeline.py
def test_pipeline_env_defaults_pinned_for_tests() -> None:
    import os
    # conftest 必须在 import 前钉死，保证全套跑时确定（参照 CHAPTER_MAX_REWRITE_ROUNDS）。
    assert os.environ.get("LOGIC_CRITIC_MAX_ROUNDS") == "2"
    assert os.environ.get("CHAPTER_PIPELINE_ENABLED") == "1"
```

- [x] **Step 2: Run test to verify it fails**

Run: `cd /root/ai-write/backend && .venv/bin/python -m pytest tests/services/test_chapter_pipeline.py::test_pipeline_env_defaults_pinned_for_tests -v`
Expected: FAIL with `AssertionError: assert None == '2'`

- [x] **Step 3: Write minimal implementation**

In `backend/tests/conftest.py`, find the existing env-pin block (where `CHAPTER_MAX_REWRITE_ROUNDS` / auth creds are set with `os.environ.setdefault(...)` before `from app.main import app`) and add two lines in that same block:

```python
os.environ.setdefault("LOGIC_CRITIC_MAX_ROUNDS", "2")
os.environ.setdefault("CHAPTER_PIPELINE_ENABLED", "1")
```

(If `CHAPTER_MAX_REWRITE_ROUNDS` is not yet pinned there, also add `os.environ.setdefault("CHAPTER_MAX_REWRITE_ROUNDS", "2")` to keep the suite deterministic.)

Note: the Task-9 tests use `monkeypatch.setenv` which overrides these per-test; these defaults only govern tests that don't set them explicitly.

- [x] **Step 4: Run test to verify it passes**

Run: `cd /root/ai-write/backend && .venv/bin/python -m pytest tests/services/test_chapter_pipeline.py::test_pipeline_env_defaults_pinned_for_tests -v`
Expected: PASS

- [x] **Step 5: Commit**

```bash
cd /root/ai-write && git add backend/tests/conftest.py backend/tests/services/test_chapter_pipeline.py
git commit -m "test(pipeline): pin LOGIC_CRITIC_MAX_ROUNDS/CHAPTER_PIPELINE_ENABLED in conftest"
```

---

## Task 11: 接入 generate.py 调用点 + `logic_critic_done` SSE 事件

**Files:**
- Modify: `backend/app/api/generate.py:444-475`

- [x] **Step 1: Read the current call site**

Run: `cd /root/ai-write && sed -n '440,476p' backend/app/api/generate.py`
Confirm the block matches the "关键既有事实" description (the `async with async_session_factory() as quality_db:` wrapping `apply_chapter_quality_gate`).

- [x] **Step 2: Replace the gate call with the pipeline call**

In `backend/app/api/generate.py`, within the `if not req.skip_polish:` try-block, replace:

```python
                        from app.db.session import async_session_factory
                        async with async_session_factory() as quality_db:
                            quality_gate_result = await apply_chapter_quality_gate(
                                text=full_text,
                                db=quality_db,
                                project_id=req.project_id,
                                chapter_id=req.chapter_id,
                                target_word_count=target_words,
                                skip_polish=False,
                            )
                        quality_gate_meta = quality_gate_result.to_safe_metadata()
```

with:

```python
                        from app.db.session import async_session_factory
                        from app.services.chapter_pipeline import run_chapter_pipeline
                        async with async_session_factory() as quality_db:
                            pipeline_result = await run_chapter_pipeline(
                                text=full_text,
                                db=quality_db,
                                project_id=req.project_id,
                                chapter_id=req.chapter_id,
                                target_word_count=target_words,
                                chapter_outline=chapter_outline,
                                prev_chapter_tail=(previous_text or "")[-1500:],
                                skip_polish=False,
                            )
                        # logic_critic 报告作为独立可选事件（前端可选消费，不破坏既有事件）。
                        yield f"data: {json.dumps({'event': 'logic_critic_done', **pipeline_result.to_echo_report()}, ensure_ascii=False)}\n\n"
                        quality_gate_result = pipeline_result.quality_gate_result
                        quality_gate_meta = quality_gate_result.to_safe_metadata()
```

All downstream references to `quality_gate_result` / `quality_gate_meta` (the `if quality_gate_result.status != "passed":` branch and below) remain unchanged — `quality_gate_result` is still a `ChapterQualityGateResult`.

- [x] **Step 3: Verify import + syntax**

Run: `cd /root/ai-write/backend && .venv/bin/python -c "import ast; ast.parse(open('app/api/generate.py').read()); print('OK')"`
Expected: `OK`

Run: `cd /root/ai-write/backend && .venv/bin/python -c "from app.api import generate; print('import OK')"`
Expected: `import OK`

- [x] **Step 4: Run the chapter-generation API tests (regression on the call site)**

Run: `cd /root/ai-write/backend && .venv/bin/python -m pytest tests/ -k "generate or chapter_quality_gate" -v`
Expected: PASS (no regression; `CHAPTER_PIPELINE_ENABLED=1` default means pipeline wraps the gate, and with no logic_critic PromptAsset the critic degrades → behavior equals old gate path for tests that hit real routing; unit tests mock as needed)

- [x] **Step 5: Commit**

```bash
cd /root/ai-write && git add backend/app/api/generate.py
git commit -m "feat(generate): route chapter polish through run_chapter_pipeline + logic_critic_done SSE event"
```

---

## Task 12: 全套回归 + 交付验证

**Files:** none (verification only)

- [x] **Step 1: Full backend suite**

Run: `cd /root/ai-write/backend && .venv/bin/python -m pytest`
Expected: all green (previous baseline 642 + new logic_critic/pipeline tests). If any pre-existing-unrelated failure appears, confirm it also fails on `git stash` (do not attribute to this work).

- [x] **Step 2: Frontend typecheck (no new errors)**

Run: `cd /root/ai-write/frontend && npx tsc --noEmit`
Expected: zero output (no new TS errors; we added an optional SSE event, no frontend change required).

- [x] **Step 3: Confirm one-key rollback works**

Run: `cd /root/ai-write/backend && CHAPTER_PIPELINE_ENABLED=0 .venv/bin/python -m pytest tests/services/test_chapter_pipeline.py -k disabled -v`
Expected: PASS — pipeline delegates straight to `apply_chapter_quality_gate`, behavior identical to today.

- [x] **Step 4: Update project memory**

Append a one-line pointer + a short project memory note recording: subproject B (multi-agent chapter pipeline) landed, files `logic_critic.py` + `chapter_pipeline.py`, switch `CHAPTER_PIPELINE_ENABLED`, rounds `LOGIC_CRITIC_MAX_ROUNDS`, task_types `logic_critic`/`drafter` (with fallbacks), live container still runs old code until `docker compose up -d --build backend`.

- [x] **Step 5: Final commit**

```bash
cd /root/ai-write && git add -A
git commit -m "docs(pipeline): record subproject B completion in project memory"
```

---

## Self-Review (completed by plan author)

**Spec coverage:** 串行三角色（Task 6/8/9 编排）✓；逻辑核查 5 维度（Task 2 prompt + Task 3 dimension 归一）✓；issue 结构含 unlocatable（Task 3）✓；clean 快路径 / 定向改写只动命中处 / 2 轮封顶 / plateau / 尽力修不阻断（Task 9）✓；quality_gate 零改动作第三棒（Task 8/9 调用，未改其文件）✓；CHAPTER_PIPELINE_ENABLED 一键回退（Task 8 + Task 12 Step 3）✓；echo 契约只回约定字段（Task 7）✓；降级策略（Task 4 critic 降级 / Task 6 rewrite 降级 / Task 9 串接）✓；task_type 路由 + fallback（Task 5）✓；generate.py 接缝 + 新 SSE 事件（Task 11）✓；测试确定性（Task 10）✓；回归 + 前端兼容（Task 12）✓。

**Placeholder scan:** Task 8 故意留「占位最小路径」并在 Task 9 Step 3 明确要求删除重复定义——这是受控的两步实现，非遗留 placeholder。其余步骤均含完整代码与精确命令。

**Type consistency:** `LogicCriticReport`(available/clean/issues + high_issues/locatable_issues/issue_count)、`LogicIssue`(dimension/severity/quote/problem/fix_hint/locatable)、`ChapterPipelineResult`(final_text/quality_gate_result/logic_rounds/logic_issues_remaining/logic_available + to_echo_report)、函数名 `build_logic_critic_user_content`/`parse_logic_critic_output`/`run_logic_critic`/`build_targeted_rewrite_content`/`apply_targeted_logic_rewrite`/`run_chapter_pipeline` 在各 Task 间一致。`run_structured_prompt`/`run_text_prompt`/`apply_chapter_quality_gate` 签名与既有代码核对一致。
