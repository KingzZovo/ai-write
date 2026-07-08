# Global Prose Quality Internalization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the repeated user feedback about unnatural AI prose into reusable, cross-project writing rules, diagnostics, prompt injection, and manual audit UI so future projects inherit the lessons automatically.

**Architecture:** Create a single prose-quality rule catalog as the source of truth, then have the checker, quality gate, generation preflight prompt, and manual audit API/UI consume that catalog. Keep the current metrics, but add aggregate and generalized diagnostics so we catch families of problems rather than one-off bad phrases.

**Tech Stack:** Python 3, pytest, FastAPI, SQLAlchemy async sessions, TypeScript/React, existing `chinese_prose_mechanics_checker`, `chapter_quality_gate`, and `CheckerDashboard`.

---

## File Structure

- Create `docs/narrative_prose_quality_contract.md`
  - Cross-project prose-quality contract distilled from the user feedback.
  - Explains root causes, bad/good examples, and repair principles in reusable terms.
- Modify `docs/narrative_antimechanical_flow.md`
  - Link the new prose contract and clarify that it is the canonical style-quality source.
- Create `backend/app/services/prose_quality_rules.py`
  - Single source of truth for reusable rule definitions, bad/good examples, metric names, prompt instructions, and regex patterns.
- Modify `backend/app/services/chinese_prose_mechanics_checker.py`
  - Import reusable rule patterns from `prose_quality_rules.py`.
  - Add aggregate metrics for normal modern Chinese violations and duplicate/repeated reasoning.
- Modify `backend/app/services/narrative_quality_gates.py`
  - Render prompt guidance from the rule catalog instead of duplicating every lesson inline.
- Modify `backend/app/services/chapter_quality_gate.py`
  - Include the new aggregate metrics in scoring and rewrite payloads.
  - Add rule-catalog guidance to rewrite prompts.
- Modify `backend/app/services/chapter_generator.py`
  - Add rule-catalog markers to the runtime prompt snapshot.
- Modify `backend/app/api/quality.py`
  - Include `chinese_prose_mechanics` in `/api/chapters/{chapter_id}/check-quality`.
- Modify `frontend/src/components/panels/CheckerDashboard.tsx`
  - Surface prose-mechanics metrics in the existing manual quality checker.
- Modify `frontend/src/components/workspace/DesktopWorkspace.tsx`
  - Include the new aggregate metrics in quality-gate failure summaries.
- Test `backend/tests/test_prose_quality_rules.py`
- Test `backend/tests/test_chinese_prose_mechanics_checker.py`
- Test `backend/tests/test_chapter_quality_gate.py`
- Test `backend/tests/test_quality_api.py`

---

### Task 1: Codify the Lessons as a Cross-Project Contract

**Files:**
- Create: `docs/narrative_prose_quality_contract.md`
- Modify: `docs/narrative_antimechanical_flow.md`

- [x] **Step 1: Write the contract document**

Create `docs/narrative_prose_quality_contract.md` with this content:

```markdown
# 跨项目中文正文质量契约

> 本契约来自《神裔》第一章和前序多轮生成反馈，但不绑定任何项目、题材、角色或剧情。它约束所有项目的中文小说正文生成、修订和人工审核。

## 1. 核心目标

正文必须像正常现代中文小说，而不是 AI 为了显得“文学、冷硬、聪明、凝练”输出的压缩腔、坐标日志、设定说明书或回合制对白。

这不是词表补丁。每条规则都必须按“根因 → 识别 → 替代写法 → 检测指标”内化。

## 2. 五类根因

### 2.1 伪文学压缩腔

根因：模型把普通现代动作和对话压成半文言、舞台说明或假文学句。

坏例：
- “少年喊了声师傅。”
- “他把来意说得很低。”
- “值守员终于抬头，问他干什么的。”
- “声音被关门声挡住一半。”

好例：
- “他叫了一声：‘师傅。’”
- “值守员抬头：‘干什么？’”
- “‘车没了，手机快没电了。我想在门厅坐到天亮，不上楼，也不敲门。’”
- “门已经合上了。”

检测指标：
- `pseudo_literary_register_count`
- `plain_contemporary_violation_count`

### 2.2 正常汉语搭配缺失

根因：模型为了短、硬、深，故意省掉现代汉语里必须出现的词或使用抽象形容。

坏例：
- “门缝里透不出灯。”
- “叫车页面刷出来的价格更难看。”
- “没有任何可以借口留下来的声音。”
- “手机还活着。”

好例：
- “门缝里没有灯光。”
- “叫车价格超过余额。”
- “里面没有人声，也没有开门动静。”
- “手机还没关机。”

检测指标：
- `semantic_collocation_count`
- `abstract_evasion_count`
- `plain_contemporary_violation_count`

### 2.3 生活逻辑与资源链断裂

根因：模型只追求氛围或困境，不校验钱、手机、电量、交通、场地规则和人物行为是否互相支撑。

坏例：
- “十八岁住旅馆被说未成年不行。”
- “附近没车，路人立刻坐上同类出租车。”
- “只剩口袋零钱，却还准备用手机叫车。”
- “烤肠摊卖临期面包、热水和汤。”

好例：
- “前台看了眼身份证，说今晚满房。”
- “叫车页面没人接单，他看了眼余额，又退出去。”
- “烤肠摊只卖烤肠，水自己带。”

检测指标：
- `mundane_logic_violation_count`
- `resource_continuity_count`
- `scene_plausibility_count`
- `transport_logic_count`
- `shelter_cost_logic_count`

### 2.4 重复解释和金句式心理剖白

根因：模型把同一个压力、退路、心理结论反复换说法，造成凑字数和“金句堆叠”。

坏例：
- “所有能解释的退路都在变窄。”
- 同一场景连续两次解释“回去会被赶、站着会被问、手机快没电”。
- “寄在别人生活边缘的人”这类抽象判断连续堆叠。

好例：
- 只保留一次具体压力链：活动室锁着、值守棚看不清、手机只剩百分之五。
- 下一段让角色行动：去灯亮的门檐下问一句。

检测指标：
- `duplicate_explanation_span_count`
- `hardship_stack_count`
- `abstract_evasion_count`

### 2.5 对话过度聪明和回合制

根因：模型让所有角色都完美接话、押节奏、抖机灵，像带提词器。

坏例：
- 连续三组以上短问短答。
- “踩烂了，算你买 / 先问问它算谁卖。”
- 路人字正腔圆讨论“执行者、血裔、旧神、奥丁”。

好例：
- 对方不回答、岔开、抢白、只说一半。
- 底层人物护财就说“踩坏了照价赔”“别站锅前面”。
- 路人只抱怨封路、停电、绕路、查得紧。

检测指标：
- `dialogue_symmetry_risk_count`
- `perfect_comeback_run_count`
- `cheap_wit_count`
- `story_bible_leakage_count`

## 3. 生成和修订硬规则

1. 普通现代场景优先使用普通现代中文。
2. 发现问题时先归因到规则族，不先追加单词黑名单。
3. 每个新增坏例必须同时提供好例和检测方式。
4. 任何规则必须同时进入生成前提示、后置质量门禁、人工审核报告。
5. 项目名、角色名、章节名不得出现在通用规则里，除非作为测试 fixture 的局部文本。

## 4. 人工审核清单

人工审核正文时先看这五项：

1. 有没有半文言、压缩腔、故作深沉的现代句？
2. 钱、电量、交通、场地规则是否自洽？
3. 对话是不是太会接、太对称、太机智？
4. 是否重复解释同一个压力或心理？
5. 世界观是否通过路人、新闻、广告、公告生硬朗读？
```

- [x] **Step 2: Link it from the anti-mechanical flow doc**

Append this section to `docs/narrative_antimechanical_flow.md`:

```markdown
## 7. 与正文质量契约的关系

本文档描述“反机械化流程”，`docs/narrative_prose_quality_contract.md` 是跨项目中文正文质量的规则目录。实现时以正文质量契约为规则来源，再把规则注入生成前提示、质量门禁和人工审核界面。
```

- [x] **Step 3: Verify docs are present**

Run:

```bash
test -f docs/narrative_prose_quality_contract.md
rg -n "伪文学压缩腔|plain_contemporary_violation_count|正文质量契约" docs/narrative_prose_quality_contract.md docs/narrative_antimechanical_flow.md
```

Expected: command exits `0` and prints matches from both files.

- [x] **Step 4: Commit**

```bash
git add docs/narrative_prose_quality_contract.md docs/narrative_antimechanical_flow.md
git commit -m "docs: codify cross-project prose quality contract"
```

---

### Task 2: Create a Reusable Prose Rule Catalog

**Files:**
- Create: `backend/app/services/prose_quality_rules.py`
- Test: `backend/tests/test_prose_quality_rules.py`

- [x] **Step 1: Write the failing tests**

Create `backend/tests/test_prose_quality_rules.py`:

```python
from app.services.prose_quality_rules import (
    PROSE_QUALITY_RULES,
    metric_names,
    regex_patterns_for,
    render_prose_quality_prompt,
)


def test_rule_catalog_contains_cross_project_lessons() -> None:
    rule_ids = {rule.rule_id for rule in PROSE_QUALITY_RULES}

    assert "plain_contemporary_chinese" in rule_ids
    assert "semantic_collocation_completeness" in rule_ids
    assert "resource_and_scene_logic" in rule_ids
    assert "duplicate_explanation_control" in rule_ids
    assert "dialogue_human_friction" in rule_ids


def test_rule_catalog_exposes_metrics_without_project_names() -> None:
    names = metric_names()

    assert "pseudo_literary_register_count" in names
    assert "plain_contemporary_violation_count" in names
    assert "duplicate_explanation_span_count" in names
    joined = "\n".join(rule.prompt_instruction for rule in PROSE_QUALITY_RULES)
    assert "神裔" not in joined
    assert "雨夜借宿" not in joined


def test_plain_contemporary_patterns_are_generalized() -> None:
    patterns = regex_patterns_for("plain_contemporary_chinese")
    joined = "\n".join(patterns)

    assert "喊了声" in joined
    assert "来意" in joined
    assert "说得很低" in joined
    assert "声音被" in joined


def test_rendered_prompt_contains_bad_and_good_examples() -> None:
    prompt = render_prose_quality_prompt()

    assert "plain_contemporary_chinese" in prompt
    assert "伪文学压缩腔" in prompt
    assert "喊了声师傅" in prompt
    assert "他叫了一声" in prompt
    assert "先归因到规则族" in prompt
```

- [x] **Step 2: Run tests to verify they fail**

Run:

```bash
backend/.venv/bin/pytest backend/tests/test_prose_quality_rules.py -q
```

Expected: FAIL because `app.services.prose_quality_rules` does not exist.

- [x] **Step 3: Implement the rule catalog**

Create `backend/app/services/prose_quality_rules.py`:

```python
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProseQualityRule:
    rule_id: str
    metric_names: tuple[str, ...]
    title: str
    root_cause: str
    bad_examples: tuple[str, ...]
    good_examples: tuple[str, ...]
    regex_patterns: tuple[str, ...]
    prompt_instruction: str


PROSE_QUALITY_RULES: tuple[ProseQualityRule, ...] = (
    ProseQualityRule(
        rule_id="plain_contemporary_chinese",
        metric_names=("pseudo_literary_register_count", "plain_contemporary_violation_count"),
        title="伪文学压缩腔",
        root_cause="普通现代场景被写成半文言、舞台说明或假文学压缩句。",
        bad_examples=(
            "少年喊了声师傅。",
            "他把来意说得很低。",
            "值守员终于抬头，问他干什么的。",
            "声音被关门声挡住一半。",
        ),
        good_examples=(
            "他叫了一声：“师傅。”",
            "值守员抬头：“干什么？”",
            "“车没了，手机快没电了。我想在门厅坐到天亮，不上楼，也不敲门。”",
            "门已经合上了。",
        ),
        regex_patterns=(
            r"喊了声(?:师傅|老板|阿姨|叔|大叔|婶|哥|姐|同学|老师|保安)",
            r"(?:把)?来意(?:说|讲|交代)得很(?:低|轻)",
            r"(?:把话|话|声音)?说得很低",
            r"终于抬头",
            r"声音被.{0,12}(?:挡住|盖住|拉散|压住)",
            r"把声音.{0,8}压得很低",
            r"声音.{0,8}压得很低",
        ),
        prompt_instruction=(
            "plain_contemporary_chinese：普通现代场景必须用完整、自然、当代的中文表达。"
            "不要写半文言、伪文学、为了显得干练而硬压缩的句子。"
            "发现坏例时先归因到规则族，再按好例方向改写。"
        ),
    ),
    ProseQualityRule(
        rule_id="semantic_collocation_completeness",
        metric_names=("semantic_collocation_count", "abstract_evasion_count", "plain_contemporary_violation_count"),
        title="正常汉语搭配缺失",
        root_cause="为了短、硬、深而省略现代汉语必须出现的词，或用抽象形容替代具体事实。",
        bad_examples=(
            "门缝里透不出灯。",
            "叫车页面刷出来的价格更难看。",
            "没有任何可以借口留下来的声音。",
            "手机还活着。",
        ),
        good_examples=(
            "门缝里没有灯光。",
            "叫车价格超过余额。",
            "里面没有人声，也没有开门动静。",
            "手机还没关机。",
        ),
        regex_patterns=(
            r"透不出灯(?!光)",
            r"(?:价格|车费|房费|房价|账单|金额|价钱).{0,14}(?:更)?难看",
            r"(?:可以|能|任何).{0,12}借口.{0,12}(?:声音|留下)",
            r"手机.{0,8}还活着",
        ),
        prompt_instruction=(
            "semantic_collocation_completeness：现代汉语搭配必须完整自然。"
            "写灯光、人声、余额、没关机等具体说法，不用抽象或故作深沉的搭配。"
        ),
    ),
    ProseQualityRule(
        rule_id="resource_and_scene_logic",
        metric_names=(
            "mundane_logic_violation_count",
            "resource_continuity_count",
            "scene_plausibility_count",
            "transport_logic_count",
            "shelter_cost_logic_count",
        ),
        title="生活逻辑与资源链断裂",
        root_cause="困境、氛围和行动没有经过钱、电量、交通、场地规则和人物行为的现实校验。",
        bad_examples=(
            "十八岁住旅馆被说未成年不行。",
            "附近没车，路人立刻坐上同类出租车。",
            "只剩口袋零钱，却还准备用手机叫车。",
            "烤肠摊卖临期面包、热水和汤。",
        ),
        good_examples=(
            "前台看了眼身份证，说今晚满房。",
            "叫车页面没人接单，他看了眼余额，又退出去。",
            "烤肠摊只卖烤肠，水自己带。",
        ),
        regex_patterns=(),
        prompt_instruction=(
            "resource_and_scene_logic：钱、手机、电量、交通、住宿和场地规则必须互相支撑。"
            "普通生活场景先按真实经验校验，再写氛围。"
        ),
    ),
    ProseQualityRule(
        rule_id="duplicate_explanation_control",
        metric_names=("duplicate_explanation_span_count", "hardship_stack_count", "abstract_evasion_count"),
        title="重复解释和金句式心理剖白",
        root_cause="同一压力、退路或心理结论被反复换说法，造成凑字数和金句堆叠。",
        bad_examples=(
            "所有能解释的退路都在变窄。",
            "同一场景连续两次解释回去会被赶、站着会被问、手机快没电。",
        ),
        good_examples=(
            "只保留一次具体压力链：活动室锁着、值守棚看不清、手机只剩百分之五。",
            "下一段让角色行动：去灯亮的门檐下问一句。",
        ),
        regex_patterns=(
            r"所有能解释的退路都在变窄",
            r"退路.{0,8}变窄",
            r"寄在别人生活边缘",
        ),
        prompt_instruction=(
            "duplicate_explanation_control：同一压力链只解释一次。"
            "心理剖白和金句不能连续堆叠，重复时改成行动推进。"
        ),
    ),
    ProseQualityRule(
        rule_id="dialogue_human_friction",
        metric_names=(
            "dialogue_symmetry_risk_count",
            "perfect_comeback_run_count",
            "cheap_wit_count",
            "story_bible_leakage_count",
        ),
        title="对话过度聪明和回合制",
        root_cause="角色完美接话、押节奏、抖机灵，或让路人朗读世界观设定。",
        bad_examples=(
            "踩烂了，算你买 / 先问问它算谁卖。",
            "路人讨论执行者、血裔、旧神、奥丁。",
        ),
        good_examples=(
            "踩坏了照价赔。",
            "路人只抱怨封路、停电、绕路、查得紧。",
        ),
        regex_patterns=(),
        prompt_instruction=(
            "dialogue_human_friction：角色不能完美接梗或连续短问短答。"
            "紧张场景要允许无视、抢白、答非所问、说半截和信息掉地。"
        ),
    ),
)


def rule_by_id(rule_id: str) -> ProseQualityRule:
    for rule in PROSE_QUALITY_RULES:
        if rule.rule_id == rule_id:
            return rule
    raise KeyError(rule_id)


def metric_names() -> tuple[str, ...]:
    names: list[str] = []
    for rule in PROSE_QUALITY_RULES:
        for name in rule.metric_names:
            if name not in names:
                names.append(name)
    return tuple(names)


def regex_patterns_for(rule_id: str) -> tuple[str, ...]:
    return rule_by_id(rule_id).regex_patterns


def render_prose_quality_prompt() -> str:
    lines = ["【cross_project_prose_quality_contract｜跨项目中文正文质量契约】"]
    lines.append("发现问题时先归因到规则族，不要只追加单词黑名单。")
    for rule in PROSE_QUALITY_RULES:
        lines.append(f"- {rule.rule_id}｜{rule.title}：{rule.prompt_instruction}")
        lines.append("  坏例：" + " / ".join(rule.bad_examples))
        lines.append("  好例：" + " / ".join(rule.good_examples))
    return "\n".join(lines)
```

- [x] **Step 4: Run tests to verify they pass**

Run:

```bash
backend/.venv/bin/pytest backend/tests/test_prose_quality_rules.py -q
```

Expected: `4 passed`.

- [x] **Step 5: Commit**

```bash
git add backend/app/services/prose_quality_rules.py backend/tests/test_prose_quality_rules.py
git commit -m "feat: add reusable prose quality rule catalog"
```

---

### Task 3: Generalize the Checker Beyond One-Off Phrases

**Files:**
- Modify: `backend/app/services/chinese_prose_mechanics_checker.py`
- Test: `backend/tests/test_chinese_prose_mechanics_checker.py`

- [x] **Step 1: Add failing generalized checker tests**

Append these tests to `backend/tests/test_chinese_prose_mechanics_checker.py`:

```python
def test_detects_general_plain_contemporary_violations_not_only_original_phrase() -> None:
    text = """
    少年喊了声老板。门房终于抬头，问他找谁。
    他把来意讲得很轻，说手机快没电，想在门厅坐到天亮。
    门缝里透不出灯，也听不见任何可以借口留下来的声音。
    手机还活着，他就没有再问。
    """

    report = analyze_chinese_prose_mechanics(text)

    assert report.pseudo_literary_register_count >= 2
    assert report.semantic_collocation_count >= 2
    assert report.plain_contemporary_violation_count >= 4
    assert not report.passed


def test_allows_general_plain_contemporary_rewrite() -> None:
    text = """
    他叫了一声：“老板。”
    门房抬头：“找谁？”
    “车没了，手机快没电了。我想在门厅坐到天亮，不上楼，也不敲门。”
    门缝里没有灯光，也听不见人声。手机还没关机，他先把它收起来。
    """

    report = analyze_chinese_prose_mechanics(text)

    assert report.pseudo_literary_register_count == 0
    assert report.semantic_collocation_count == 0
    assert report.plain_contemporary_violation_count == 0
    assert report.passed


def test_detects_duplicate_explanation_spans() -> None:
    text = """
    回去，值守员多半会让他别挡门；继续站在这里，住户出来一次，他就要解释一次。手机已经只剩百分之五。
    他看了眼活动室的锁，又看了眼值守棚。回去可能被赶出来，继续站在这里，住户出来一次，他就要解释一次。
    """

    report = analyze_chinese_prose_mechanics(text)

    assert report.duplicate_explanation_span_count >= 1
    assert not report.passed
```

- [x] **Step 2: Run tests to verify they fail**

Run:

```bash
backend/.venv/bin/pytest \
  backend/tests/test_chinese_prose_mechanics_checker.py::test_detects_general_plain_contemporary_violations_not_only_original_phrase \
  backend/tests/test_chinese_prose_mechanics_checker.py::test_allows_general_plain_contemporary_rewrite \
  backend/tests/test_chinese_prose_mechanics_checker.py::test_detects_duplicate_explanation_spans \
  -q
```

Expected: FAIL because `plain_contemporary_violation_count` and `duplicate_explanation_span_count` are missing or not counted.

- [x] **Step 3: Import rule patterns and add report fields**

Modify `backend/app/services/chinese_prose_mechanics_checker.py`:

```python
from app.services.prose_quality_rules import regex_patterns_for
```

Add fields to `ChineseProseMechanicsReport`:

```python
plain_contemporary_violation_count: int = 0
duplicate_explanation_span_count: int = 0
```

Add both fields to `to_safe_dict()`:

```python
"plain_contemporary_violation_count": self.plain_contemporary_violation_count,
"duplicate_explanation_span_count": self.duplicate_explanation_span_count,
```

- [x] **Step 4: Replace local pseudo-literary patterns with catalog patterns**

Replace the current `PSEUDO_LITERARY_REGISTER_PATTERNS = [...]` block with:

```python
PSEUDO_LITERARY_REGISTER_PATTERNS = list(regex_patterns_for("plain_contemporary_chinese"))
```

Extend `SEMANTIC_COLLOCATION_PATTERNS` only if the listed catalog examples are not already covered:

```python
r"手机.{0,8}还活着",
```

- [x] **Step 5: Add duplicate explanation counting**

Add this helper near the other counting helpers:

```python
def _count_duplicate_explanation_spans(paragraphs: list[str]) -> int:
    pressure_patterns = [
        r"回去.{0,30}(?:赶|挡门|不让|赶出来).{0,50}继续站.{0,50}解释",
        r"继续站.{0,50}解释.{0,50}回去.{0,30}(?:赶|挡门|不让|赶出来)",
        r"退路.{0,12}变窄",
        r"寄在别人生活边缘",
    ]
    normalized_hits: list[str] = []
    for paragraph in paragraphs:
        compact = re.sub(r"\s+", "", paragraph or "")
        if not compact:
            continue
        for pattern in pressure_patterns:
            if re.search(pattern, compact):
                normalized_hits.append(re.sub(r"[，。！？；,.!?;：:、]+", "", compact[:80]))
                break
    if len(normalized_hits) < 2:
        return 0
    return len(normalized_hits) - len(set(normalized_hits[:1]))
```

If this helper over-counts in implementation, keep the three tests above as the boundary: the repeated pressure sample must fail, the good contemporary sample must pass.

- [x] **Step 6: Compute aggregate metrics and gate on them**

Inside `analyze_chinese_prose_mechanics()` after existing metric assignments:

```python
report.duplicate_explanation_span_count = _count_duplicate_explanation_spans(paragraphs)
report.plain_contemporary_violation_count = (
    report.pseudo_literary_register_count
    + report.semantic_collocation_count
    + report.awkward_register_count
    + report.mundane_register_count
    + report.abstract_evasion_count
)
```

Add both fields to the `report.passed = not (...)` expression:

```python
or report.plain_contemporary_violation_count
or report.duplicate_explanation_span_count
```

- [x] **Step 7: Run checker tests**

Run:

```bash
backend/.venv/bin/pytest backend/tests/test_chinese_prose_mechanics_checker.py -q
```

Expected: all tests in that file pass.

- [x] **Step 8: Commit**

```bash
git add backend/app/services/chinese_prose_mechanics_checker.py backend/tests/test_chinese_prose_mechanics_checker.py
git commit -m "feat: generalize prose mechanics diagnostics"
```

---

### Task 4: Inject the Rule Catalog into Generation and Rewrite Prompts

**Files:**
- Modify: `backend/app/services/narrative_quality_gates.py`
- Modify: `backend/app/services/chapter_quality_gate.py`
- Modify: `backend/app/services/chapter_generator.py`
- Test: `backend/tests/test_chinese_prose_mechanics_checker.py`
- Test: `backend/tests/test_chapter_quality_gate.py`

- [x] **Step 1: Add failing prompt coverage tests**

In `backend/tests/test_chinese_prose_mechanics_checker.py`, extend `test_generation_preflight_uses_soft_4000_word_target_without_filler_pressure`:

```python
assert "cross_project_prose_quality_contract" in prompt
assert "先归因到规则族" in prompt
assert "plain_contemporary_violation_count" in prompt
assert "duplicate_explanation_span_count" in prompt
```

In `backend/tests/test_chapter_quality_gate.py`, add this assertion to `test_quality_gate_rewrites_pseudo_literary_register`:

```python
assert "cross_project_prose_quality_contract" in kwargs["extra_system"]
assert "plain_contemporary_violation_count" in kwargs["user_content"]
assert "duplicate_explanation_span_count" in kwargs["user_content"]
```

- [x] **Step 2: Run prompt tests to verify they fail**

Run:

```bash
backend/.venv/bin/pytest \
  backend/tests/test_chinese_prose_mechanics_checker.py::test_generation_preflight_uses_soft_4000_word_target_without_filler_pressure \
  backend/tests/test_chapter_quality_gate.py::test_quality_gate_rewrites_pseudo_literary_register \
  -q
```

Expected: FAIL because the rule-catalog prompt and new aggregate metrics are not fully threaded through.

- [x] **Step 3: Render rule catalog in global generation prompt**

Modify `backend/app/services/narrative_quality_gates.py`:

```python
from app.services.prose_quality_rules import render_prose_quality_prompt
```

In `contract_hard_gate_prompt()`, after `lines.append(CHINESE_PROSE_MECHANICS_PROMPT)`, add:

```python
lines.append(render_prose_quality_prompt())
```

In `preflight_scene_blueprint_prompt()`, add this line near the prose mechanics section:

```python
{render_prose_quality_prompt()}
```

- [x] **Step 4: Thread new metrics through generation preflight**

Modify `backend/app/services/chinese_prose_mechanics_checker.py` inside `build_generation_preflight_prompt()`:

```python
plain_contemporary_violation = int(safe.get("plain_contemporary_violation_count") or 0)
duplicate_explanation_span = int(safe.get("duplicate_explanation_span_count") or 0)
```

Add both to the metrics line:

```python
f"plain_contemporary_violation={plain_contemporary_violation}, "
f"duplicate_explanation_span={duplicate_explanation_span}."
```

Append:

```python
"cross_project_prose_quality_contract 必须执行；发现问题时先归因到规则族，不要只追加单词黑名单。"
"plain_contemporary_violation_count 必须为 0；普通现代场景必须使用完整自然的现代中文。"
"duplicate_explanation_span_count 必须为 0；同一压力链、退路解释和心理判断只保留一次，重复时改成行动推进。"
```

- [x] **Step 5: Thread new metrics through quality gate**

Modify `_quality_penalty()` in `backend/app/services/chapter_quality_gate.py`:

```python
+ report.plain_contemporary_violation_count * 360
+ report.duplicate_explanation_span_count * 300
```

Modify `_build_rewrite_user_content()` payload:

```python
"plain_contemporary_violation_count": initial_report.plain_contemporary_violation_count,
"duplicate_explanation_span_count": initial_report.duplicate_explanation_span_count,
```

Add rewrite bullets:

```python
"- cross_project_prose_quality_contract：先按规则族修，不要只替换用户点名的一句话。同类伪文学压缩腔、语义搭配缺失、资源逻辑断裂和重复解释都要一起清理。\n"
"- duplicate_explanation_control：同一压力链只解释一次。重复的退路说明、心理金句和困境标签要删除或改成下一步行动。\n"
```

- [x] **Step 6: Add runtime marker**

Modify `backend/app/services/chapter_generator.py` marker tuple:

```python
"cross_project_prose_quality_contract",
"plain_contemporary_violation_count",
"duplicate_explanation_span_count",
```

- [x] **Step 7: Run prompt and gate tests**

Run:

```bash
backend/.venv/bin/pytest backend/tests/test_chinese_prose_mechanics_checker.py backend/tests/test_chapter_quality_gate.py -q
```

Expected: both files pass.

- [x] **Step 8: Commit**

```bash
git add \
  backend/app/services/narrative_quality_gates.py \
  backend/app/services/chinese_prose_mechanics_checker.py \
  backend/app/services/chapter_quality_gate.py \
  backend/app/services/chapter_generator.py \
  backend/tests/test_chinese_prose_mechanics_checker.py \
  backend/tests/test_chapter_quality_gate.py
git commit -m "feat: inject prose quality contract into generation prompts"
```

---

### Task 5: Expose the Internalized Rules for Manual Audit

**Files:**
- Modify: `backend/app/api/quality.py`
- Test: `backend/tests/test_quality_api.py`
- Modify: `frontend/src/components/panels/CheckerDashboard.tsx`
- Modify: `frontend/src/components/workspace/DesktopWorkspace.tsx`

- [x] **Step 1: Add backend API test**

Create or update `backend/tests/test_quality_api.py` with:

```python
from app.services.chinese_prose_mechanics_checker import analyze_chinese_prose_mechanics


def test_chinese_prose_mechanics_report_contains_manual_audit_metrics() -> None:
    text = "少年喊了声师傅。他把来意说得很低。门缝里透不出灯。"

    report = analyze_chinese_prose_mechanics(text).to_safe_dict()

    assert report["pseudo_literary_register_count"] >= 2
    assert report["semantic_collocation_count"] >= 1
    assert report["plain_contemporary_violation_count"] >= 3
    assert "duplicate_explanation_span_count" in report
```

This test verifies the report payload shape used by API/UI without requiring a DB fixture.

- [x] **Step 2: Run test to verify it fails or reports missing aggregate metrics**

Run:

```bash
backend/.venv/bin/pytest backend/tests/test_quality_api.py::test_chinese_prose_mechanics_report_contains_manual_audit_metrics -q
```

Expected: FAIL until Task 3 metrics are implemented; PASS after Task 3.

- [x] **Step 3: Include prose mechanics in check-quality API**

Modify `backend/app/api/quality.py` inside `check_quality()` after `result = await mgr.run_all(text, context)`:

```python
from app.services.chinese_prose_mechanics_checker import analyze_chinese_prose_mechanics

prose_report = analyze_chinese_prose_mechanics(text).to_safe_dict()
```

Add to the returned dict:

```python
"chinese_prose_mechanics": prose_report,
```

- [x] **Step 4: Update CheckerDashboard response type**

Modify `frontend/src/components/panels/CheckerDashboard.tsx`:

```ts
interface ChineseProseMechanics {
  passed: boolean
  pseudo_literary_register_count: number
  plain_contemporary_violation_count: number
  duplicate_explanation_span_count: number
  semantic_collocation_count: number
  resource_continuity_count: number
  scene_plausibility_count: number
  dialogue_symmetry_risk_count: number
  story_bible_leakage_count: number
}
```

Add state:

```ts
const [proseMechanics, setProseMechanics] = useState<ChineseProseMechanics | null>(null)
```

Update the fetch type and setter:

```ts
const data = await apiFetch<{
  checkers: CheckerResult[]
  overall_score: number
  chinese_prose_mechanics?: ChineseProseMechanics
}>(`/api/chapters/${chapterId}/check-quality`, { method: 'POST' })
setResults(data.checkers)
setProseMechanics(data.chinese_prose_mechanics ?? null)
```

- [x] **Step 5: Render the manual prose audit block**

In `CheckerDashboard.tsx`, render this block after the overall score:

```tsx
{proseMechanics && (
  <div className={`rounded-lg border p-3 ${
    proseMechanics.passed ? 'border-emerald-200 bg-emerald-50/50' : 'border-red-200 bg-red-50/50'
  }`}>
    <div className="text-xs font-semibold text-stone-800 mb-2">中文行文机械自检</div>
    <div className="grid grid-cols-2 gap-x-3 gap-y-1 text-[11px] text-stone-600">
      <span>伪文学压缩：{proseMechanics.pseudo_literary_register_count}</span>
      <span>现代汉语异常：{proseMechanics.plain_contemporary_violation_count}</span>
      <span>重复解释：{proseMechanics.duplicate_explanation_span_count}</span>
      <span>搭配异常：{proseMechanics.semantic_collocation_count}</span>
      <span>资源矛盾：{proseMechanics.resource_continuity_count}</span>
      <span>场景失真：{proseMechanics.scene_plausibility_count}</span>
      <span>对话对称：{proseMechanics.dialogue_symmetry_risk_count}</span>
      <span>设定泄漏：{proseMechanics.story_bible_leakage_count}</span>
    </div>
  </div>
)}
```

- [x] **Step 6: Improve generation failure summary**

Modify `frontend/src/components/workspace/DesktopWorkspace.tsx` quality failure summary:

```ts
`plain=${report.plain_contemporary_violation_count ?? 0}`,
`pseudo=${report.pseudo_literary_register_count ?? 0}`,
`repeat=${report.duplicate_explanation_span_count ?? 0}`,
```

Keep the existing `dialogue`, `duplicate`, `spatial`, and `bio` fields.

- [x] **Step 7: Run backend and frontend verification**

Run:

```bash
backend/.venv/bin/pytest backend/tests/test_quality_api.py backend/tests/test_chinese_prose_mechanics_checker.py -q
node ./node_modules/typescript/bin/tsc --noEmit
```

Expected: pytest passes and TypeScript exits `0`.

- [x] **Step 8: Commit**

```bash
git add \
  backend/app/api/quality.py \
  backend/tests/test_quality_api.py \
  frontend/src/components/panels/CheckerDashboard.tsx \
  frontend/src/components/workspace/DesktopWorkspace.tsx
git commit -m "feat: expose prose mechanics for manual audit"
```

---

### Task 6: Add Regression Coverage for the Actual Learned Experience

**Files:**
- Modify: `backend/tests/test_chinese_prose_mechanics_checker.py`
- Modify: `backend/tests/test_chapter_quality_gate.py`
- Modify: `docs/PROGRESS.md`

- [x] **Step 1: Add a regression test that proves the system is not single-phrase-only**

Append this to `backend/tests/test_chinese_prose_mechanics_checker.py`:

```python
def test_user_feedback_lessons_generalize_across_variants() -> None:
    bad_samples = [
        "少年喊了声阿姨。她终于抬头，问他干嘛。",
        "他把来意讲得很轻，说自己只想躲到天亮。",
        "门缝里透不出灯，也没有可以借口留下来的声音。",
        "手机还活着，他就继续往前。",
        "所有能解释的退路都在变窄。",
    ]

    for sample in bad_samples:
        report = analyze_chinese_prose_mechanics(sample)
        assert not report.passed, sample
        assert (
            report.plain_contemporary_violation_count
            + report.duplicate_explanation_span_count
            + report.semantic_collocation_count
            + report.abstract_evasion_count
        ) >= 1, sample
```

- [x] **Step 2: Add quality-gate prompt regression**

Append this to `backend/tests/test_chapter_quality_gate.py`:

```python
@pytest.mark.asyncio
async def test_quality_gate_tells_model_to_generalize_user_feedback(monkeypatch) -> None:
    bad = "少年喊了声老板。他把来意讲得很轻。门缝里透不出灯。"
    good = "他叫了一声：“老板。”\n“车没了，手机快没电了。我想在门厅坐到天亮。”\n门缝里没有灯光。"

    async def fake_run_text_prompt(*args, **kwargs):
        assert "先按规则族修" in kwargs["user_content"]
        assert "不要只替换用户点名的一句话" in kwargs["user_content"]
        assert "cross_project_prose_quality_contract" in kwargs["extra_system"]
        return SimpleNamespace(text=good)

    monkeypatch.setattr(cqg, "run_text_prompt", fake_run_text_prompt)

    result = await cqg.apply_chapter_quality_gate(
        text=bad,
        db=SimpleNamespace(),
        project_id="proj",
        chapter_id="ch",
    )

    assert result.status == "passed"
    assert result.initial_report.plain_contemporary_violation_count >= 2
    assert result.final_report.plain_contemporary_violation_count == 0
```

- [x] **Step 3: Run regression tests**

Run:

```bash
backend/.venv/bin/pytest \
  backend/tests/test_chinese_prose_mechanics_checker.py::test_user_feedback_lessons_generalize_across_variants \
  backend/tests/test_chapter_quality_gate.py::test_quality_gate_tells_model_to_generalize_user_feedback \
  -q
```

Expected: both pass after Tasks 2-4.

- [x] **Step 4: Update progress notes**

Append to `docs/PROGRESS.md`:

```markdown
## 2026-06-02 — 跨项目正文质量内化

- 新增 `docs/narrative_prose_quality_contract.md`，把《神裔》第一章反馈沉淀为跨项目规则：伪文学压缩腔、正常汉语搭配缺失、生活逻辑与资源链断裂、重复解释、对话过度聪明。
- 新增 `backend/app/services/prose_quality_rules.py` 作为规则单一来源，供生成前提示、质量门禁、检查器和人工审核共用。
- 新增聚合指标 `plain_contemporary_violation_count` 与 `duplicate_explanation_span_count`，防止只修用户点名句子。
- `/api/chapters/{chapter_id}/check-quality` 返回 `chinese_prose_mechanics`，前端检查面板可人工审核行文机械问题。
```

- [x] **Step 5: Commit**

```bash
git add \
  backend/tests/test_chinese_prose_mechanics_checker.py \
  backend/tests/test_chapter_quality_gate.py \
  docs/PROGRESS.md
git commit -m "test: lock prose quality lessons as regressions"
```

---

### Task 7: Final Verification

**Files:**
- Verify all modified files from Tasks 1-6.

- [x] **Step 1: Run backend tests**

Run:

```bash
backend/.venv/bin/pytest \
  backend/tests/test_prose_quality_rules.py \
  backend/tests/test_chinese_prose_mechanics_checker.py \
  backend/tests/test_chapter_quality_gate.py \
  backend/tests/test_quality_api.py \
  -q
```

Expected: all selected backend tests pass.

- [x] **Step 2: Run TypeScript check**

Run:

```bash
node ./node_modules/typescript/bin/tsc --noEmit
```

Expected: exits `0`.

- [x] **Step 3: Verify current first chapter still passes prose mechanics**

Run:

```bash
PYTHONPATH=backend backend/.venv/bin/python -c "from pathlib import Path; from app.services.chinese_prose_mechanics_checker import analyze_chinese_prose_mechanics, dumps_report; text=Path('backend/tmp/shenyi_ch1_manual.txt').read_text(encoding='utf-8'); report=analyze_chinese_prose_mechanics(text); print(dumps_report(report)); raise SystemExit(0 if report.passed else 1)"
```

Expected: JSON contains `"passed": true`, `plain_contemporary_violation_count: 0`, `duplicate_explanation_span_count: 0`.

- [x] **Step 4: Verify prompt injection markers**

Run:

```bash
PYTHONPATH=backend backend/.venv/bin/python -c "from app.services.narrative_quality_gates import contract_hard_gate_prompt; p=contract_hard_gate_prompt(); required=['cross_project_prose_quality_contract','plain_contemporary_chinese','plain_contemporary_violation_count','duplicate_explanation_span_count','先归因到规则族']; missing=[x for x in required if x not in p]; print({'missing': missing}); raise SystemExit(1 if missing else 0)"
```

Expected: `{'missing': []}`.

- [x] **Step 5: Commit final verification note if docs changed**

If no new files changed during verification, skip this commit. If verification required documentation edits, run:

```bash
git add docs/PROGRESS.md
git commit -m "docs: record prose quality verification"
```

---

## Self-Review

Spec coverage:
- Summarize experience: Task 1 and Task 6.
- Optimize into project: Tasks 2-5.
- Internalize rules: Tasks 2 and 4.
- Generalize beyond one-off phrases: Tasks 2, 3, and 6.
- Make all projects obey: Task 4 uses global generation/quality prompts, Task 5 exposes manual audit.
- Verification: Task 7.

No placeholder scan:
- The plan does not use `TBD`, `TODO`, or "write tests for the above" without concrete tests.
- Every implementation task includes exact file paths and commands.

Type consistency:
- New metric names are consistent across tasks:
  - `plain_contemporary_violation_count`
  - `duplicate_explanation_span_count`
  - existing `pseudo_literary_register_count`

