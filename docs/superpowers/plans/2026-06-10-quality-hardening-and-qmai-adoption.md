# 质量加固 + QMAI 借鉴 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 锁定当前未提交工作、修复 8 个后端 + 4 个前端正确性 bug，并落地 4 项 QMAI 借鉴特性（去AI味规则库、定点修订、角色认知账本、伏笔债务分+黄金三章）。

**Architecture:** 项目是 FastAPI + Celery + SQLAlchemy(async) + Postgres 后端、Next.js(App Router) + zustand 前端的 AI 小说生成平台。本计划分 4 个 Phase 顺序执行：Phase 0 仓库卫生与语义提交（必须在主工作区执行，**不可用 worktree**，因为未提交改动就在主工作区）；Phase 1 后端修复；Phase 2 前端修复；Phase 3 QMAI 特性。

**Tech Stack:** Python 3 / FastAPI / Celery / SQLAlchemy async / pytest；TypeScript / Next.js / zustand。

**重要背景：**
- 当前分支是 `rescue/2026-05-17-baseline`（HEAD d6e417a），这是事实上的主线。本计划所有提交都落在该分支，不新建分支。
- `.worktrees/` 下有其他 worktree（outline-contract-gate 等），不要动它们。
- 前端有 `frontend/AGENTS.md` 警告：本项目的 Next.js 版本与训练数据可能不同，改前端前先读 `frontend/node_modules/next/dist/docs/` 里相关文档。
- 后端测试：`cd /root/ai-write/backend && python -m pytest -q`（约 450 个用例，收集正常）。
- 前端检查：`cd /root/ai-write/frontend && npx tsc --noEmit`（当前通过，必须保持通过）；eslint 现有 70 个历史 error，只要求**不新增**。

---

## Phase 0：仓库卫生与提交锁定

### Task 1: PROGRESS.md 出库 + 临时文件清理

**Files:**
- Modify: `/root/ai-write/.gitignore`
- Delete: `/root/ai-write/.tmp_eval_compensate_v5_ca2b9969.py`, `/root/ai-write/.tmp_eval_compensate_v7_21a75e05.py`
- Untrack: `/root/ai-write/PROGRESS.md`（3.9MB 自动维护日志，本次 diff 中 +38k 行，必须出库）

- [ ] **Step 1: 确认 PROGRESS.md 被跟踪、临时脚本未被跟踪**

```bash
cd /root/ai-write
git ls-files PROGRESS.md          # 预期输出 PROGRESS.md（被跟踪）
git ls-files .tmp_eval_compensate_v5_ca2b9969.py .tmp_eval_compensate_v7_21a75e05.py  # 预期无输出
ls backend/_p1.py backend/_p2.py 2>/dev/null && git ls-files backend/_p1.py backend/_p2.py  # 确认是否存在/被跟踪
```

- [ ] **Step 2: 出库与清理**

```bash
cd /root/ai-write
git rm --cached PROGRESS.md
rm -f .tmp_eval_compensate_v5_ca2b9969.py .tmp_eval_compensate_v7_21a75e05.py
# 若 backend/_p1.py/_p2.py 存在且未跟踪：mv 到 scripts/；若被跟踪：git mv backend/_p1.py scripts/
```

- [ ] **Step 3: 更新 .gitignore**（在现有内容后追加）

```gitignore
# auto-maintained operation logs & scratch
PROGRESS.md
tmp/
backend/tmp/
.tmp_*.py
.tmp_*.sse
.tmp_*.log
```

- [ ] **Step 4: 验证 git status 不再显示 PROGRESS.md 与 tmp 噪音，提交**

```bash
cd /root/ai-write
git status --short | head -30   # 预期：PROGRESS.md 显示为 D（staged 删除），tmp/ 不出现
git add .gitignore
git commit -m "chore: untrack PROGRESS.md and ignore scratch files"
```

### Task 2: 把 31 个未提交文件拆成语义提交

**Files:** 全部已修改文件（`git status --short` 查看）。**不要使用 `git add -A` 一把梭。**

- [ ] **Step 1: 逐文件看 diff 确认分组**

```bash
cd /root/ai-write
git status --short
git diff --stat
# 对每个文件 git diff <path> 浏览要点，确认归入下面哪一组
```

- [ ] **Step 2: 按以下分组依次提交**（若某文件 diff 内容跨组，归入主题最接近的一组即可，不要花时间做 hunk 级拆分）

```bash
# 组1 章节目标字数迁移（默认 4000 + legacy 50000 解析）
git add backend/app/models/project.py backend/app/schemas/project.py \
        backend/app/api/chapters.py backend/app/api/projects.py backend/app/api/volumes.py \
        backend/app/services/budget_allocator.py \
        backend/tests/services/test_budget_allocator.py backend/tests/services/test_regenerate_budget_flow.py
git commit -m "feat: chapter target word count migration with legacy 50k resolution"

# 组2 生成链路（readiness 门 + 场景预算 + 上下文）
git add backend/app/api/generate.py backend/app/tasks/knowledge_tasks.py \
        backend/app/services/chapter_generator.py backend/app/services/scene_orchestrator.py \
        backend/app/services/context_pack.py
git commit -m "feat: outline readiness gate and scene budget expansion in generation flow"

# 组3 评分/修订策略翻转
git add backend/app/services/auto_revise.py backend/app/services/chapter_evaluator.py \
        backend/app/services/narrative_contract.py backend/app/services/checkers/anti_ai_checker.py
git commit -m "feat: flip revise strategy to pre-generation internalization with score acceptance"

# 组4 model_router 可靠性
git add backend/app/services/model_router.py backend/tests/test_b1_tier_fallback.py
git commit -m "fix: model router call-log commit resilience and timeout plumbing"

# 组5 前端 readiness UI + 错误解析增强
git add frontend/
git commit -m "feat(frontend): outline readiness UI, structured error parsing, workspace tabs"

# 组6 兜底：剩余未提交文件（运行 git status 检查，逐个判断提交或忽略）
git status --short
```

- [ ] **Step 3: 提交后跑测试确认基线绿**

```bash
cd /root/ai-write/backend && python -m pytest -q 2>&1 | tail -5
# 预期：全部 pass（若有失败，失败项必须是历史已知失败，记录下来；不允许因为分组提交引入新失败——提交是整树快照，理论上不会）
cd /root/ai-write/frontend && npx tsc --noEmit   # 预期：无输出（通过）
```

---

## Phase 1：后端正确性修复（每个 bug 一个 task，TDD）

### Task 3 (B1): SSE 场景模式回退时清空已收集文本

**Files:**
- Modify: `backend/app/api/generate.py:393-404`（`except Exception as scene_err:` 分支）
- Test: `backend/tests/api/test_generate_sse_fallback.py`（新建；先看 `backend/tests/` 现有 conftest 与 api 测试怎么拿 app/client fixture，复用之）

**Bug:** SSE 场景流式中途失败时，`collected_text` 里已有部分场景文本；回退到单发生成后 `collected_text.append(generated_text)` 直接追加，`full_text = "".join(collected_text)`（line 419）把**半截场景 + 完整全文**拼在一起存库 → 章节内容重复/损坏。Celery 路径（knowledge_tasks.py）同场景有 `collected.clear()`，API 路径漏了。

- [ ] **Step 1: 写失败测试**（按现有 conftest 调整 fixture 用法；核心断言如下）

```python
# backend/tests/api/test_generate_sse_fallback.py
"""SSE scene-mode fallback must discard partial scene text (regression for duplicated chapter content)."""
import pytest

@pytest.mark.anyio
async def test_scene_fallback_discards_partial_text(monkeypatch):
    from app.api import generate as gen_mod

    async def fake_stream(*a, **k):
        yield "部分场景文本A"
        yield "部分场景文本B"
        raise RuntimeError("scene_planner_failed: unparseable_output")

    # monkeypatch SceneOrchestrator.orchestrate_chapter_stream -> fake_stream
    # monkeypatch 单发生成器返回 "完整回退正文"
    # 调用 SSE endpoint（或提取出的 event_stream 辅助函数），收集最终保存的 full_text
    # 断言: full_text == "完整回退正文"，不包含 "部分场景文本A"
```

注：`event_stream` 是 endpoint 内联生成器，若 monkeypatch 端到端太重，允许把「场景流式 + 回退」这段逻辑提取成模块级辅助 `async def _stream_scene_with_fallback(...) -> AsyncIterator[str]`（返回 SSE data 行，内部维护 collected_text），对辅助函数做单测。提取时保持 endpoint 行为不变。

- [ ] **Step 2: 跑测试确认失败**

```bash
cd /root/ai-write/backend && python -m pytest tests/api/test_generate_sse_fallback.py -v
# 预期 FAIL：full_text 含 "部分场景文本A"
```

- [ ] **Step 3: 修复** — 在 `except Exception as scene_err:` 分支里、调用单发生成器之前：

```python
                except Exception as scene_err:
                    logger.warning(
                        "SceneOrchestrator failed (falling back to ChapterGenerator): %s",
                        scene_err,
                    )
                    # Discard partial scene chunks: the single-shot fallback
                    # regenerates the full chapter, so keeping them would
                    # duplicate content in the saved full_text.
                    collected_text.clear()
                    yield f"data: {json.dumps({'event': 'fallback_restart'}, ensure_ascii=False)}\n\n"
                    generated_text = await _run_single_shot_generator()
```

（`fallback_restart` 事件让前端有机会清空已渲染的半截文本；前端处理在 Task 12 一并做。）

- [ ] **Step 4: 跑测试确认通过 + 全量回归**

```bash
cd /root/ai-write/backend && python -m pytest tests/api/test_generate_sse_fallback.py -v && python -m pytest -q 2>&1 | tail -3
```

- [ ] **Step 5: Commit** — `git commit -m "fix: discard partial scene text when SSE falls back to single-shot generator"`

### Task 4 (B2): model_router 消费 retry_attempts

**Files:**
- Modify: `backend/app/services/model_router.py:263-334`（`OpenAIProvider.generate`）
- Test: `backend/tests/test_model_router_retry.py`（新建）

**Bug:** 调用方（`chapter_generator.py:71`、`api/generate.py:80`、`knowledge_tasks.py:43-62`）层层传入 `retry_attempts`，但 `OpenAIProvider.generate` 只 pop `stream`/`request_timeout`，`call_with_retry(attempts=1 if task_type=="evaluation" else 4)` 硬编码 → 环境变量 `SYNC_SINGLE_SHOT_LLM_RETRY_ATTEMPTS=1` 完全失效，单发路径最坏 840s×4 重试。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_model_router_retry.py
"""retry_attempts kwarg must control call_with_retry attempts (was silently ignored)."""
import pytest
from unittest.mock import AsyncMock, patch

@pytest.mark.anyio
async def test_retry_attempts_kwarg_is_honored():
    from app.services.model_router import OpenAIProvider
    provider = OpenAIProvider(api_key="test")
    captured = {}

    async def fake_call_with_retry(fn, label="", attempts=4):
        captured["attempts"] = attempts
        class R:  # minimal GenerationResult stand-in
            text = "ok"
        return R()

    with patch("app.services.llm_retry.call_with_retry", side_effect=fake_call_with_retry):
        await provider.generate(
            messages=[{"role": "user", "content": "hi"}],
            model="m", task_type="chapter", retry_attempts=2, stream=False,
        )
    assert captured["attempts"] == 2
```

（`call_with_retry` 是函数内局部 import：`from app.services.llm_retry import call_with_retry`，patch 目标是 `app.services.llm_retry.call_with_retry`。fake 的返回值如不满足类型，可 patch `_one_attempt` 路径或放宽断言——以实际可行为准，核心是断言 attempts==2。）

- [ ] **Step 2: 跑测试确认失败**（预期 captured["attempts"] == 4）

- [ ] **Step 3: 修复** — `OpenAIProvider.generate` 开头与结尾：

```python
        stream_mode = kw.pop("stream", True)
        request_timeout = kw.pop("request_timeout", None)
        retry_attempts = kw.pop("retry_attempts", None)
        ...
        attempts = 1 if task_type == "evaluation" else 4
        if retry_attempts is not None and int(retry_attempts) > 0:
            attempts = int(retry_attempts)
        return await call_with_retry(
            _one_attempt, label=f"openai_chat[{task_type}:{model}]",
            attempts=attempts,
        )
```

同时检查 `AnthropicProvider.generate`（~line 179）：至少同样 `kw.pop("retry_attempts", None)`、`kw.pop("stream", None)`、`kw.pop("request_timeout", None)`，避免未知 kwarg 直传 SDK 报错。

- [ ] **Step 4: 测试通过 + 回归**：`python -m pytest tests/test_model_router_retry.py tests/test_b1_tier_fallback.py -v`
- [ ] **Step 5: Commit** — `git commit -m "fix: honor retry_attempts kwarg in model router providers"`

### Task 5 (B3): 评估调用超时可配置（45s → 默认 120s）

**Files:**
- Modify: `backend/app/services/model_router.py:283-291`
- Test: 追加到 `backend/tests/test_model_router_retry.py`

**Bug:** 非流式分支 `timeout = request_timeout if request_timeout is not None else 45`，评估任务输入 ~13K 中文字符，慢端点 45s 必超时 → 异常 → 返回全零 EvaluationResult（overall=0 假评分）。根目录两个 `.tmp_eval_compensate_*.py` 就是这个问题的人工补偿证据。

- [ ] **Step 1: 写失败测试**

```python
@pytest.mark.anyio
async def test_evaluation_timeout_env_override(monkeypatch):
    monkeypatch.setenv("EVALUATION_REQUEST_TIMEOUT", "200")
    from app.services.model_router import _resolve_nonstream_timeout
    assert _resolve_nonstream_timeout(None, "evaluation") == 200
    assert _resolve_nonstream_timeout(None, "evaluation") != 45
    assert _resolve_nonstream_timeout(30, "evaluation") == 30      # 显式传参优先
    assert _resolve_nonstream_timeout(None, "chapter") == 45       # 非评估保持 45
```

- [ ] **Step 2: 跑测试确认失败**（函数不存在 → ImportError）

- [ ] **Step 3: 实现** — model_router.py 模块级新增（放在 OpenAIProvider 之前）：

```python
def _resolve_nonstream_timeout(request_timeout, task_type: str) -> float:
    """Evaluation reads ~13K chars of Chinese prose; 45s was causing silent
    timeouts that surfaced as all-zero scores. Default is env-tunable."""
    if request_timeout is not None:
        return request_timeout
    if task_type == "evaluation":
        import os
        return float(os.getenv("EVALUATION_REQUEST_TIMEOUT", "120"))
    return 45
```

`_one_attempt` 内替换：`timeout = _resolve_nonstream_timeout(request_timeout, task_type)`。
并在 `/root/ai-write/.env.example` 追加一行 `EVALUATION_REQUEST_TIMEOUT=120`。

- [ ] **Step 4: 测试通过 + 回归**
- [ ] **Step 5: Commit** — `git commit -m "fix: make evaluation request timeout configurable (default 120s, was 45s)"`

### Task 6 (B4): 评审系统提示拼回违规分类法 + max_tokens 提额

**Files:**
- Modify: `backend/app/services/chapter_evaluator.py:22-38, 235`
- Test: `backend/tests/services/test_chapter_evaluator_prompt.py`（新建）

**Bug:** `EVALUATOR_CONTRACT_PROMPT`（narrative_contract.py 中的违规分类法）被 import 但不再注入系统提示 → 评审模型不知道 `violation_type` 的合法取值，而 `auto_revise`、`narrative_quality_gates.issue_violation_type()`、knowledge_tasks 的 issue_focus 提取全依赖这些标签。同时 `max_tokens=900` 对「5 维 × 12 条中文 issues」JSON 极易截断 → JSONDecodeError → 全零评分。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/services/test_chapter_evaluator_prompt.py
def test_system_prompt_contains_violation_taxonomy():
    from app.services.chapter_evaluator import EVALUATION_SYSTEM_PROMPT
    from app.services.narrative_contract import EVALUATOR_CONTRACT_PROMPT
    assert EVALUATOR_CONTRACT_PROMPT.strip()[:80] in EVALUATION_SYSTEM_PROMPT

def test_evaluation_max_tokens_not_truncating():
    import inspect
    from app.services import chapter_evaluator
    src = inspect.getsource(chapter_evaluator)
    assert "max_tokens=900" not in src
```

- [ ] **Step 2: 确认失败**
- [ ] **Step 3: 修复**

```python
EVALUATION_SYSTEM_PROMPT = """\
你是小说章节质量评审。只输出合法 JSON，不要输出解释、Markdown 或正文摘录。
按 5 个维度评分，每项 0-10：plot_coherence、character_consistency、style_adherence、narrative_pacing、foreshadow_handling。
issues 只列关键问题，最多 12 条；每条只写元数据和简短诊断，禁止引用/复述原文章句子。
JSON 格式必须是：
{...原有 JSON 模板不变...}

""" + EVALUATOR_CONTRACT_PROMPT
```

`max_tokens=900` → `max_tokens=2400`。

- [ ] **Step 4: 测试通过 + 回归**（注意检查是否有现存测试断言旧 prompt 内容，若有则同步更新断言）
- [ ] **Step 5: Commit** — `git commit -m "fix: restore violation taxonomy in evaluator prompt and raise max_tokens to 2400"`

### Task 7 (B5): Celery 章节路径走 legacy 字数解析

**Files:**
- Modify: `backend/app/tasks/knowledge_tasks.py:656`
- Test: 追加到现有 chapter_target_words 相关测试文件（`grep -rl chapter_target_words backend/tests/` 找到它）

**Bug:** `generated_chapter_target_words = params.get("target_words") or ch.target_word_count` 没有走 `resolve_chapter_target_word_count`：遗留 `target_word_count=50000` 的旧章节直接进 SceneOrchestrator，场景预算上限 20×1200=24000 < 0.85×50000 → `_scene_budget_reaches_target` 永远失败 → 永远落到模板 briefs（质量塌陷）。API 路径（generate.py:264）已正确解析，Celery 路径不一致。

- [ ] **Step 1: 写失败测试**（提取可测纯函数）

```python
def test_celery_chapter_target_words_resolves_legacy_50k():
    from app.tasks.knowledge_tasks import _resolve_task_chapter_target_words
    # legacy 50000 应解析为项目设置或默认 4000
    assert _resolve_task_chapter_target_words({"target_words": None}, 50000, {"target_chapter_words": 6000}) == 6000
    assert _resolve_task_chapter_target_words({}, 50000, {}) == 4000
    # 显式请求参数优先
    assert _resolve_task_chapter_target_words({"target_words": 3000}, 50000, {}) == 3000
```

- [ ] **Step 2: 确认失败**
- [ ] **Step 3: 实现** — knowledge_tasks.py 模块级新增：

```python
def _resolve_task_chapter_target_words(params, chapter_target_word_count, project_settings) -> int:
    from app.services.chapter_target_words import resolve_chapter_target_word_count
    return resolve_chapter_target_word_count(
        (params or {}).get("target_words") or chapter_target_word_count,
        (project_settings or {}).get("target_chapter_words"),
    )
```

调用点改为 `generated_chapter_target_words = _resolve_task_chapter_target_words(params, ch.target_word_count, project_settings)`。注意：chapter 分支当前作用域里若没有 `project_settings`，参照该文件其他地方/或 generate.py 如何加载项目 settings_json（ch → volume → project），加载后传入；若加载成本过高，传 `{}`（仍能修掉 50000→4000 的主 bug）。

- [ ] **Step 4: 测试通过 + 回归**
- [ ] **Step 5: Commit** — `git commit -m "fix: resolve legacy 50k target word count in celery chapter generation path"`

### Task 8 (B6): 删除 should_stop_random_retry 三层死代码

**Files:**
- Modify: `backend/app/services/auto_revise.py:107-121`、`backend/app/api/generate.py:641,696`、`backend/app/tasks/knowledge_tasks.py:1056,1153`

**Bug:** `should_stop_random_retry` 本体已改为恒 `return False`，两个调用点又被 `if False and ...` 包住——同一逻辑三层禁用，调用方还在维护 `previous_blocking_violations` 状态喂给永不可达的分支。

- [ ] **Step 1: 全面定位**

```bash
cd /root/ai-write/backend && grep -rn "should_stop_random_retry\|previous_blocking_violations" app/ tests/
```

- [ ] **Step 2: 删除** — `auto_revise.py` 中函数本体与导出；两个调用文件中删除 import、`if False and ...` 整个分支；`previous_blocking_violations` 若仅为该分支服务则连同其维护代码一起删，若还有其他读取方则保留并注明。tests/ 中引用该函数的测试一并删除。
- [ ] **Step 3: 回归** — `python -m pytest -q 2>&1 | tail -3`
- [ ] **Step 4: Commit** — `git commit -m "refactor: remove triple-disabled should_stop_random_retry dead code"`

### Task 9 (B7): 场景列表截断保护结尾场景

**Files:**
- Modify: `backend/app/services/scene_orchestrator.py:491-496`
- Test: `backend/tests/services/test_scene_orchestrator_parse.py`（若已有 plan_scenes 相关测试文件则追加）

**Bug:** `for i, raw in enumerate(parsed[:max_scenes], ...)` 用 `_scene_count_for_target` 的**提示值**在校验前硬截断：planner 输出场景数超过 hint 时，尾部场景（通常含本章结局/钩子）被静默丢弃，且截断后总预算更容易不达标 → 回退模板。

- [ ] **Step 1: 写失败测试**

```python
def test_planner_overflow_keeps_tail_scene():
    from app.services.scene_orchestrator import _cap_parsed_scenes, MAX_SCENE_COUNT
    parsed = [{"goal": f"s{i}"} for i in range(MAX_SCENE_COUNT + 5)]
    capped = _cap_parsed_scenes(parsed)
    assert len(capped) == MAX_SCENE_COUNT
    assert capped[-1]["goal"] == f"s{MAX_SCENE_COUNT + 4}"  # 最后一个场景（结局/钩子）必须保留

def test_planner_within_cap_untouched():
    from app.services.scene_orchestrator import _cap_parsed_scenes
    parsed = [{"goal": f"s{i}"} for i in range(8)]
    assert _cap_parsed_scenes(parsed) == parsed   # 不再按 hint 截断
```

- [ ] **Step 2: 确认失败**
- [ ] **Step 3: 实现**

```python
def _cap_parsed_scenes(parsed: list) -> list:
    """Cap planner output at the hard MAX_SCENE_COUNT, never the soft hint.
    When overflowing, keep the final scene: it carries the chapter ending/hook."""
    if len(parsed) <= MAX_SCENE_COUNT:
        return parsed
    return parsed[: MAX_SCENE_COUNT - 1] + parsed[-1:]
```

调用点：`for i, raw in enumerate(_cap_parsed_scenes(parsed), start=1):`（`max_scenes = _scene_count_for_target(...)` 仅继续作为 planner prompt 的提示值使用，不再用于截断；若该变量在此处再无其他用途则删除本地赋值）。

- [ ] **Step 4: 测试通过 + 回归**
- [ ] **Step 5: Commit** — `git commit -m "fix: cap planner scenes at hard max and preserve ending scene"`

### Task 10 (B8): prompt 规则单源化（有界重构）

**Files:**
- Modify: `backend/app/services/narrative_quality_gates.py`（105-147 与 356-392 两处 CHINESE_PROSE_MECHANICS 内容重复；356 行空壳「八、runtime_prompt_snapshot」小节；标题/正文/docstring 版本号 v4.13/v4.12/v4.9 漂移）
- Test: `backend/tests/services/test_prompt_single_source.py`（新建）

**范围约束：** 这是有界清理，只做三件事：① 散文力学规则只保留一个常量、blueprint 引用它；② 删除空壳小节与重复的「八、」节；③ 版本号统一为一处常量。**不要**重构整个 blueprint 结构，不要动 ContextPack 注入逻辑。

- [ ] **Step 1: 写失败测试**

```python
def test_prose_mechanics_single_occurrence_in_blueprint():
    from app.services import narrative_quality_gates as nqg
    blueprint = nqg.build_chapter_blueprint_prompt_for_tests() if hasattr(nqg, "build_chapter_blueprint_prompt_for_tests") else None
    # 若无现成构建入口，直接对模块内 blueprint 模板字符串断言：
    # 散文力学规则的标志性短句（任选一句规则原文）在最终模板中只出现一次
```

（先 `grep -n "八、" backend/app/services/narrative_quality_gates.py` 与 `grep -c "<某条规则的标志短句>"` 确认重复形态，再写出可执行断言——以重复短句出现次数==1 为准。）

- [ ] **Step 2: 确认失败** → **Step 3: 单源化实现** → **Step 4: 测试通过 + 全量回归**（重点跑 `pytest -q -k "quality or prose or blueprint"`）
- [ ] **Step 5: Commit** — `git commit -m "refactor: single-source prose mechanics rules in chapter blueprint"`

---

## Phase 2：前端正确性修复

> 改前端前先读 `frontend/node_modules/next/dist/docs/` 中相关章节（项目 AGENTS.md 要求）。每个 task 完成后跑 `npx tsc --noEmit` 必须通过，`npx eslint src/<改动文件>` 不得新增 error。

### Task 11 (F1): apiSSE 错误回调与终态保证

**Files:**
- Modify: `frontend/src/lib/api.ts:66-127`

**Bug:** SSE 失败路径（非 200、body 空、网络错误、401）只 `console.error`，从不调用 `onDone` → 调用方 `setIsGenerating(false)` 永不执行，UI 永久卡「生成中」。

- [ ] **Step 1: 重写 apiSSE**（完整替换 66-127 行）

```typescript
export function apiSSE(
  path: string,
  body: Record<string, unknown>,
  onChunk: (text: string) => void,
  onDone: () => void,
  onEvent?: (event: Record<string, unknown>) => void,
  onError?: (err: Error) => void,
) {
  const controller = new AbortController()
  let finished = false
  const finish = (err?: Error) => {
    if (finished) return
    finished = true
    if (err) {
      console.error('SSE error:', err)
      onError?.(err)
    }
    onDone()
  }

  fetch(`${API_BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify(body),
    signal: controller.signal,
  }).then(async (res) => {
    if (res.status === 401) {
      clearToken()
      if (typeof window !== 'undefined') {
        window.location.href = '/login'
      }
      finish(new Error('Unauthorized'))
      return
    }
    if (!res.ok || !res.body) {
      throw new Error(`SSE connection failed (${res.status})`)
    }
    const reader = res.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''

    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      const lines = buffer.split('\n')
      buffer = lines.pop() || ''
      for (const line of lines) {
        if (line.startsWith('data: ')) {
          const data = line.slice(6)
          if (data === '[DONE]') {
            finish()
            return
          }
          try {
            const parsed = JSON.parse(data)
            if (typeof parsed.text === 'string') {
              onChunk(parsed.text)
            } else if (onEvent) {
              onEvent(parsed)
            }
          } catch {
            onChunk(data)
          }
        }
      }
    }
    finish()
  }).catch((err) => {
    if (err.name === 'AbortError') {
      finish()           // 用户主动取消也要让 UI 收尾
      return
    }
    finish(err instanceof Error ? err : new Error(String(err)))
  })

  return controller
}
```

- [ ] **Step 2: 更新调用方** — `grep -rn "apiSSE(" frontend/src/`（DesktopWorkspace 三处等），为每处补 `onError` 参数：显示错误（沿用该组件现有错误展示方式，如 alert/状态条）并复位 isGenerating 等状态。注意 `onDone` 现在保证必达，调用方原有 onDone 里的复位逻辑天然兜底。
- [ ] **Step 3: 验证** — `npx tsc --noEmit` 通过；手动冒烟见 Phase 4 验证节。
- [ ] **Step 4: Commit** — `git commit -m "fix(frontend): guarantee SSE terminal callback and add onError"`

### Task 12 (F2): SSE AbortController 生命周期管理

**Files:**
- Modify: `frontend/src/components/workspace/DesktopWorkspace.tsx`（~513、695、906 三处 `apiSSE(` 调用；行号以 grep 实际结果为准）

**Bug:** 三处 `apiSSE(...)` 返回的 AbortController 被丢弃：组件卸载/切章/重新生成后旧流继续读取并向已卸载组件 setState，用户也无法取消。

- [ ] **Step 1: 加 ref 与清理**

```typescript
const sseControllerRef = useRef<AbortController | null>(null)

// 每处调用改为：
sseControllerRef.current?.abort()           // 打断上一个流
sseControllerRef.current = apiSSE(...)

// 组件卸载清理：
useEffect(() => {
  return () => { sseControllerRef.current?.abort() }
}, [])
```

- [ ] **Step 2: 处理 Task 3 新增的 `fallback_restart` 事件** — 在章节生成那处 `apiSSE` 的 `onEvent` 回调里：收到 `{event: 'fallback_restart'}` 时清空当前章节的已流入文本缓冲（找到 onChunk 累积文本的 state/store 操作，调用对应的 reset）。
- [ ] **Step 3: 验证** — `npx tsc --noEmit`；eslint 改动文件无新增 error。
- [ ] **Step 4: Commit** — `git commit -m "fix(frontend): abort stale SSE streams on unmount and regeneration"`

### Task 13 (F3): 轮询统一清理（usePolling hook）

**Files:**
- Create: `frontend/src/lib/usePolling.ts`
- Modify: `frontend/src/components/workspace/MobileWorkspace.tsx`（~267、393、438 三处 setInterval）、`frontend/src/app/vector/page.tsx:99-114`、`frontend/src/app/knowledge/page.tsx`（~163-175 批量测源轮询）

**Bug:** 多处 `setInterval` 只在任务终态时清除、无 unmount 清理（永久轮询 + 卸载后 setState）；vector 页缺 `failed` 终止条件且瞬时网络错误就静默停止轮询。

- [ ] **Step 1: 实现 hook**

```typescript
// frontend/src/lib/usePolling.ts
import { useEffect, useRef } from 'react'

/**
 * Run `fn` every `intervalMs` while `active` is true.
 * - cleans up on unmount / when active flips false
 * - transient errors do NOT stop polling; call stop() from fn on terminal states
 */
export function usePolling(
  fn: (stop: () => void) => void | Promise<void>,
  intervalMs: number,
  active: boolean,
) {
  const fnRef = useRef(fn)
  fnRef.current = fn

  useEffect(() => {
    if (!active) return
    let stopped = false
    const stop = () => { stopped = true; clearInterval(id) }
    const id = setInterval(() => {
      if (stopped) return
      void Promise.resolve(fnRef.current(stop)).catch(() => { /* transient; keep polling */ })
    }, intervalMs)
    return () => clearInterval(id)
  }, [intervalMs, active])
}
```

- [ ] **Step 2: 逐处替换** — 5 处轮询改用 hook；vector 页终止条件补 `status === 'failed'`；终态（completed/failed）里调用 `stop()`。knowledge 页批量测源的 600s 兜底 setTimeout 改为基于轮询开始时间戳在 fn 内判断超时后 stop。
- [ ] **Step 3: 验证** — `npx tsc --noEmit`；eslint 改动文件无新增 error。
- [ ] **Step 4: Commit** — `git commit -m "fix(frontend): unify task polling with cleanup-safe usePolling hook"`

### Task 14 (F4): apiFetch headers 合并顺序

**Files:**
- Modify: `frontend/src/lib/api.ts:27-40`

**Bug:** `fetch(url, { headers: {...}, cache: 'no-store', ...options })` 中 `...options` 最后展开：调用方一旦传 `headers` 会整体覆盖 Authorization/Content-Type（潜伏地雷，当前恰好无人传）。

- [ ] **Step 1: 修复**

```typescript
export async function apiFetch<T>(path: string, options?: RequestInit): Promise<T> {
  const headers: Record<string, string> = { ...authHeaders() }
  if (!(options?.body instanceof FormData)) {
    headers['Content-Type'] = 'application/json'
  }
  const { headers: optionHeaders, ...rest } = options ?? {}
  // PR-FIX-NO-STORE: bypass browser HTTP cache for all /api fetches.
  const res = await fetch(`${API_BASE}${path}`, {
    ...rest,
    cache: 'no-store',
    headers: { ...headers, ...(optionHeaders as Record<string, string> | undefined) },
  })
  // ...后续逻辑不变
```

- [ ] **Step 2: 验证 + Commit** — `npx tsc --noEmit`；`git commit -m "fix(frontend): merge caller headers without clobbering auth headers"`

---

## Phase 3：QMAI 借鉴落地

> 来源仓库 https://github.com/Mochocyang/QMAI （MIT 许可）。借鉴其规则思想与做法，引入的具体文案需注明出处注释 `# Adapted from QMAI (MIT, github.com/Mochocyang/QMAI)`。

### Task 15 (Q1): 去AI味规则库充实

**Files:**
- Create: `backend/app/services/prompts/anti_ai_rules_zh.py`（若 `app/services/prompts/` 不存在则新建包；先 `grep -rn "prose_quality\|rule catalog" backend/app/services/ | head` 看 7011bc9 提交建的规则目录在哪，**优先并入现有目录**）
- Modify: `backend/app/services/checkers/anti_ai_checker.py`（黑名单数据源）、章节生成 prompt 中的润色/质量契约注入点（`render_prose_quality_prompt` 所在文件，`grep -rn "render_prose_quality_prompt" backend/app/`）

- [ ] **Step 1: 拉取 QMAI 规则原文**

```bash
curl -sL https://raw.githubusercontent.com/Mochocyang/QMAI/master/QM-QUAI.md -o /tmp/qm-quai.md || \
curl -sL https://raw.githubusercontent.com/Mochocyang/QMAI/main/QM-QUAI.md -o /tmp/qm-quai.md
wc -l /tmp/qm-quai.md   # 预期拉到完整文件；若路径 404，去 github 页面找该文件实际路径
```

- [ ] **Step 2: 提炼为结构化规则**（与现有 anti_ai_checker 的黑名单去重合并）

```python
# backend/app/services/prompts/anti_ai_rules_zh.py
"""去AI味规则库（中文网文）。Adapted from QMAI (MIT, github.com/Mochocyang/QMAI)."""

# 1) 词级/句式黑名单：合并 QM-QUAI 清单与现有 anti_ai_checker 词表，按类别分组
AI_PHRASE_BLACKLIST: dict[str, list[str]] = {
    "陈词滥调": ["微微一愣", "眼中闪过一丝", "命运的齿轮开始转动", "空气突然安静下来", ...],
    "情绪直陈": ["心中一震", "内心五味杂陈", ...],
    # ... 按 QM-QUAI 实际内容补全
}

# 2) 情绪→动作 改写对照 few-shot（注入润色 prompt 用）
EMOTION_TO_ACTION_EXAMPLES: list[dict[str, str]] = [
    {"bad": "他心里充满悲伤。", "good": "他把杯子往旁边挪了挪，半天没说话。\"你早就想好了吧？\""},
    # ... 从 QM-QUAI 摘 4-6 组最有代表性的
]

# 3) 对白原则（注入生成/润色 prompt 的紧凑条目）
DIALOGUE_PRINCIPLES: list[str] = ["能短就短", "悲伤不总是哭", ...]

# 4) 禁止事项（防过度去AI味）
ANTI_OVERCORRECTION: list[str] = ["禁止把爽文改得过于文艺", "禁止为去AI味故意造错别字", "允许文字有毛边", ...]

def render_anti_ai_prompt_block(max_chars: int = 1800) -> str:
    """渲染注入生成/润色 prompt 的紧凑规则块（预算内截断）。"""
    ...
```

- [ ] **Step 3: 接入两个消费方** — ① `anti_ai_checker.py` 的检测词表改为 import `AI_PHRASE_BLACKLIST`（保留其原有词条，合并去重）；② 在 `render_prose_quality_prompt` 的输出里追加 `render_anti_ai_prompt_block()`（注意 Task 10 已单源化，只加这一处）。
- [ ] **Step 4: 测试**

```python
# backend/tests/services/test_anti_ai_rules.py
def test_blacklist_nonempty_and_checker_wired():
    from app.services.prompts.anti_ai_rules_zh import AI_PHRASE_BLACKLIST, render_anti_ai_prompt_block
    total = sum(len(v) for v in AI_PHRASE_BLACKLIST.values())
    assert total >= 40
    block = render_anti_ai_prompt_block()
    assert 0 < len(block) <= 1800
    # anti_ai_checker 能命中新词条
    from app.services.checkers.anti_ai_checker import AntiAIChecker  # 类名以实际为准
```

- [ ] **Step 5: 回归 + Commit** — `git commit -m "feat: enrich anti-AI prose rules from QMAI catalog (MIT-attributed)"`

### Task 16 (Q2): rewriteTarget 定点修订

**Files:**
- Modify: `backend/app/services/chapter_evaluator.py`（issue schema 增加 `quote` 字段）
- Modify: `backend/app/services/auto_revise.py`（新增定点修订路径）
- Test: `backend/tests/services/test_targeted_revision.py`（新建）

**设计（借鉴 QMAI 的 rewriteTarget）：** 评审 issue 增加 `quote`：从原文摘取的 10~40 字**连续**片段用于定位。修订时按 quote 在原文定位所在段落，取「该段 ±1 段」做定点改写，改写结果拼回原文；quote 缺失或定位失败的 issue 仍走现有整章修订。收益：省 token、不破坏已写好的段落。

- [ ] **Step 1: 评审 prompt 放开 quote** — `EVALUATION_SYSTEM_PROMPT` 中 issue 对象增加 `"quote": ""` 字段，并把「禁止引用/复述原文章句子」改为「除 quote 字段（10-40字连续原文片段，用于定位）外，禁止引用/复述原文」。`max_tokens` 已在 Task 6 提到 2400，够用。

- [ ] **Step 2: 写定位与切片的失败测试**

```python
# backend/tests/services/test_targeted_revision.py
def test_locate_span_by_quote():
    from app.services.auto_revise import locate_revision_span
    text = "第一段。\n\n第二段有一个错误描写在这里。\n\n第三段。\n\n第四段。"
    span = locate_revision_span(text, "错误描写在这里")
    assert span is not None
    start, end = span
    assert "第一段" in text[start:end]      # ±1 段上下文
    assert "第三段" in text[start:end]
    assert "第四段" not in text[start:end]

def test_locate_span_quote_missing_returns_none():
    from app.services.auto_revise import locate_revision_span
    assert locate_revision_span("正文内容", "不存在的片段") is None

def test_splice_revised_span():
    from app.services.auto_revise import splice_revision
    text = "AAA\n\nBBB\n\nCCC"
    out = splice_revision(text, (5, 8), "DDD")   # 替换 BBB
    assert out == "AAA\n\nDDD\n\nCCC"
```

- [ ] **Step 3: 确认失败 → 实现纯函数**

```python
# auto_revise.py
def locate_revision_span(text: str, quote: str) -> tuple[int, int] | None:
    """Find the paragraph containing `quote` and widen to ±1 paragraph.
    Returns (start, end) char offsets in `text`, or None if not found."""
    if not quote or not text:
        return None
    idx = text.find(quote.strip())
    if idx < 0:
        return None
    paras: list[tuple[int, int]] = []
    cursor = 0
    for part in text.split("\n\n"):
        paras.append((cursor, cursor + len(part)))
        cursor += len(part) + 2
    hit = next((i for i, (s, e) in enumerate(paras) if s <= idx < e), None)
    if hit is None:
        return None
    start = paras[max(0, hit - 1)][0]
    end = paras[min(len(paras) - 1, hit + 1)][1]
    return (start, end)

def splice_revision(text: str, span: tuple[int, int], revised: str) -> str:
    start, end = span
    return text[:start] + revised + text[end:]
```

- [ ] **Step 4: 接入修订流程** — 在 auto_revise 的修订入口（看现有整章修订函数怎么被 generate.py/knowledge_tasks.py 调用）增加定点路径：issues 按 quote 分组 → 可定位的 issues 按 span 合并重叠区间 → 每个区间一次 LLM 定点改写（prompt：给出原文区间 + 该区间相关 issues + 「只重写这一区间，保持上下文衔接，字数±20%」）→ splice 回原文；不可定位的 issues 若仍有阻断级，落回整章修订。**保持现有整章修订函数可用**（作为回退）。
- [ ] **Step 5: 测试通过 + 回归 + Commit** — `git commit -m "feat: targeted span revision driven by evaluator quote field (QMAI rewriteTarget)"`

### Task 17 (Q3): 角色认知账本（knows / doesNotKnow / readerKnows）

**Files:**
- Create: `backend/app/services/character_cognition.py`
- Modify: `backend/app/models/project.py`（新增模型）+ 新增 alembic migration（参照既有迁移如 a1001909 的写法，`ls backend/alembic/versions/ | tail`）
- Modify: `backend/app/services/context_pack.py`（注入认知边界节）
- Modify: 章节定稿后的记忆摄取点（`grep -rn "summary\|memory" backend/app/tasks/knowledge_tasks.py | head` 找到章节完成后更新记忆的位置）
- Test: `backend/tests/services/test_character_cognition.py`

**设计（借鉴 QMAI character-cognition）：** 每项目维护认知账本：每个角色 `knows`/`does_not_know` 两个字符串列表，外加全局 `reader_knows`（读者已知-角色未知=信息差/悬念）。章节定稿后用 LLM 结构化抽取本章认知变化并合并（角色得知某信息后自动从 does_not_know 移除）；生成下一章时序列化注入上下文；评审 prompt 注入账本并新增 `cognition_violation` 违规类型（角色提前知道不该知道的事）。

- [ ] **Step 1: 数据模型**

```python
# models/project.py 追加
class CharacterCognition(Base):
    """Per-character knowledge ledger. character_name='__reader__' stores reader-known facts."""
    __tablename__ = "character_cognitions"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)   # 类型写法参照同文件其他模型
    project_id = Column(UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False)
    character_name = Column(String(128), nullable=False)
    knows = Column(JSONB, nullable=False, default=list)
    does_not_know = Column(JSONB, nullable=False, default=list)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    __table_args__ = (UniqueConstraint("project_id", "character_name"),)
```

alembic migration 用 `alembic revision -m "add character_cognitions"`，内容参照最近一个 migration 的风格手写。

- [ ] **Step 2: 写纯逻辑失败测试**

```python
def test_apply_cognition_changes_moves_known_items():
    from app.services.character_cognition import apply_changes
    ledger = {"林冲": {"knows": [], "does_not_know": ["高俅设局"]}, "__reader__": {"knows": ["高俅设局"], "does_not_know": []}}
    changes = [{"character": "林冲", "learns": "高俅设局"}]
    out = apply_changes(ledger, changes)
    assert "高俅设局" in out["林冲"]["knows"]
    assert "高俅设局" not in out["林冲"]["does_not_know"]

def test_serialize_ledger_budget_capped():
    from app.services.character_cognition import serialize_for_prompt
    ledger = {"甲": {"knows": [f"事实{i}" for i in range(100)], "does_not_know": []}}
    text = serialize_for_prompt(ledger, max_chars=600)
    assert len(text) <= 600
```

- [ ] **Step 3: 实现 service**

```python
# backend/app/services/character_cognition.py
"""角色认知账本：knows/does_not_know/reader_knows。
Adapted from QMAI character-cognition (MIT, github.com/Mochocyang/QMAI)."""

EXTRACTION_PROMPT = """\
从本章正文中抽取认知变化，只输出 JSON 数组，每项：
{"character": "角色名或__reader__", "learns": "得知的信息（一句话）"} 或
{"character": "角色名", "still_unknown": "该角色仍不知道的关键信息（一句话）"}
只记录影响后续剧情的关键信息差，最多 10 条。"""

def apply_changes(ledger: dict, changes: list[dict]) -> dict: ...
def serialize_for_prompt(ledger: dict, max_chars: int = 1200) -> str:
    """输出形如：『林冲知道：…；林冲不知道：…；读者已知：…』的紧凑块。"""
async def load_ledger(db, project_id) -> dict: ...
async def save_ledger(db, project_id, ledger: dict) -> None: ...
async def extract_and_update(db, project_id, chapter_text: str) -> dict:
    """章节定稿后调用：LLM 抽取(task_type='evaluation' 路由) → apply_changes → save."""
```

- [ ] **Step 4: 三处接线** — ① 章节定稿/摘要摄取处调用 `extract_and_update`（失败只 warning 不阻断保存）；② `context_pack.py` 增加「人物认知边界」节（`serialize_for_prompt`，预算 ≤1200 字符，挂接现有 budget_allocator 的节预算机制）；③ `chapter_evaluator` 的 user prompt 增加「当前认知账本」节 + `EVALUATOR_CONTRACT_PROMPT`（narrative_contract.py）的违规类型清单里加 `cognition_violation（角色知道了不该知道的信息）`。
- [ ] **Step 5: 测试通过 + 跑 migration + 回归 + Commit**

```bash
cd /root/ai-write/backend && alembic upgrade head && python -m pytest -q 2>&1 | tail -3
git commit -m "feat: character cognition ledger (knows/doesNotKnow/readerKnows) wired into context and evaluation"
```

### Task 18 (Q4): 伏笔债务分 + 黄金三章约束

**Files:**
- Modify: `backend/app/services/foreshadow_manager.py`（债务分函数）
- Modify: 伏笔相关 API（`grep -rn "foreshadow" backend/app/api/ | head` 找到暴露点，加 debt 字段）
- Modify: `backend/app/services/narrative_quality_gates.py` 或 chapter_generator 的 preflight（黄金三章注入点）
- Test: `backend/tests/services/test_foreshadow_debt.py`、`backend/tests/services/test_golden_opening.py`

- [ ] **Step 1: 写伏笔债务分失败测试**

```python
def test_debt_score_critical_and_warning():
    from app.services.foreshadow_manager import compute_debt_score
    foreshadows = [
        {"status": "planted",  "planted_chapter": 1, "last_advanced_chapter": None},   # 当前第10章：critical
        {"status": "advanced", "planted_chapter": 1, "last_advanced_chapter": 2},      # 停滞8章：接近 warning
        {"status": "resolved", "planted_chapter": 1, "last_advanced_chapter": 5},
    ]
    r = compute_debt_score(foreshadows, current_chapter_idx=10)
    assert r["score"] == 100 - 15   # 1 critical(-15)；第二条停滞 10-2=8 <10 不警告
    assert len(r["criticals"]) == 1 and len(r["warnings"]) == 0

def test_debt_score_floor_zero():
    from app.services.foreshadow_manager import compute_debt_score
    many = [{"status": "planted", "planted_chapter": 1, "last_advanced_chapter": None}] * 10
    assert compute_debt_score(many, current_chapter_idx=50)["score"] == 0
```

（字段名 `status/planted_chapter/last_advanced_chapter` 以 foreshadow_manager 现有数据结构为准，先读该文件再对齐测试。）

- [ ] **Step 2: 实现**

```python
# foreshadow_manager.py
CRITICAL_STALL_CHAPTERS = 5    # planted 后 N 章未推进
WARNING_STALL_CHAPTERS = 10    # advanced 后 N 章停滞
UNRESOLVED_SOFT_CAP = 5

def compute_debt_score(foreshadows: list[dict], current_chapter_idx: int) -> dict:
    """伏笔债务健康分（满分100）。Adapted from QMAI foreshadowing debt (MIT)."""
    criticals, warnings = [], []
    unresolved = 0
    for f in foreshadows:
        status = f.get("status")
        if status == "resolved":
            continue
        unresolved += 1
        anchor = f.get("last_advanced_chapter") or f.get("planted_chapter") or 0
        stall = current_chapter_idx - anchor
        if status == "planted" and stall >= CRITICAL_STALL_CHAPTERS:
            criticals.append(f)
        elif status == "advanced" and stall >= WARNING_STALL_CHAPTERS:
            warnings.append(f)
    score = 100 - 15 * len(criticals) - 5 * len(warnings) - 2 * max(0, unresolved - UNRESOLVED_SOFT_CAP)
    return {"score": max(0, score), "criticals": criticals, "warnings": warnings, "unresolved": unresolved}
```

接线：① 伏笔列表 API 响应附带 `debt` 对象；② 章节生成 preflight 中当 `score < 60` 时注入一行提示：「伏笔债务偏高（{score}分，{n}条超期未推进：{titles}），本章优先推进或回收既有伏笔，禁止新埋伏笔」。

- [ ] **Step 3: 黄金三章注入** — 在章节 blueprint 构建处（Task 10 单源化后的位置），当 `chapter_idx <= 3` 时追加：

```python
GOLDEN_OPENING_RULES = """\
【开篇硬约束（前三章适用）】
- 前300-500字内必须进入主体事件/危机/冲突，禁止铺垫式开场（天气、回忆、世界观说明）。
- 穿越/重生/背景设定只许一笔带过，禁止成段解释。
- 每个自然段必须推动事件或人物关系，删掉任何"可有可无"的段落。
- 本章结尾必须留下让读者非看下一章不可的钩子。
"""  # Adapted from QMAI golden-three-chapters (MIT)
```

测试：`test_golden_opening.py` 断言 chapter_idx=2 时 blueprint 包含「开篇硬约束」，chapter_idx=5 时不包含。

- [ ] **Step 4: 测试通过 + 回归 + Commit** — `git commit -m "feat: foreshadow debt score gating and golden-three-chapters opening constraints"`

---

## Phase 4：端到端验证

- [ ] **后端全量测试**：`cd /root/ai-write/backend && python -m pytest -q 2>&1 | tail -5` → 全绿（或仅 Phase 0 记录过的历史失败）。
- [ ] **前端检查**：`cd /root/ai-write/frontend && npx tsc --noEmit`（必须零输出）；`npx eslint src/lib src/components/workspace 2>&1 | tail -5` 与基线对比无新增 error。
- [ ] **生成链路冒烟**（服务在 docker-compose 中）：

```bash
cd /root/ai-write && docker compose up -d --build backend frontend 2>&1 | tail -3
# 用现有项目触发一次章节生成（scene 模式），观察：
# 1) SSE 正常出文；2) 人为断开 LLM（或选不可用模型）触发回退时，保存的章节内容不重复；
# 3) 评分日志 overall 非 0 且 issues 带 violation_type/quote；4) 认知账本表有数据写入。
docker compose logs backend --since 10m | grep -iE "evaluation|fallback|cognition" | tail -20
```

- [ ] **更新 CHANGELOG.md**（按既有格式加 v1.8.x 条目）并最终提交。

## 执行注意

- 顺序执行 Task 1→18，**每个 task 一个 commit**，commit 前该 task 的测试必须真实跑过并通过（verification-before-completion）。
- 任何 task 中发现计划与实际代码不符（行号漂移、函数名不同），以实际代码为准调整，但保持 task 的目标与验收断言不变；偏差大时停下来记录到本文件再继续。
- Phase 3 的 Task 15 依赖外网拉 QMAI 文件；若拉取失败，黑名单用本计划与调研报告中已列出的条目起步（≥40 条可由现有 anti_ai_checker 词表合并凑足），不阻塞。
