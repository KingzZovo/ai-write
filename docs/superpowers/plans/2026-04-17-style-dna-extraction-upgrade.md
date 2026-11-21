# Style DNA Extraction Upgrade Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade style detection so text-based and book-based detection both use a dedicated “风格 DNA” LLM analysis, while keeping the existing `StyleProfile` schema and generation pipeline stable.

**Architecture:** Keep `StyleProfile` as the integration surface, but store the full DNA report under `config_json` and distill it into `rules_json`, `anti_ai_rules`, `tone_keywords`, and `sample_passages`. Replace the generic style-analysis prompt with a constrained JSON-first style DNA prompt, and make `/api/styles/detect` call the LLM path instead of only statistical analysis.

**Tech Stack:** FastAPI, SQLAlchemy, PromptRegistry, Next.js 16, React 19, TypeScript

---

## File Structure

**Create:**

- `backend/tests/test_style_dna_detection.py`

**Modify:**

- `backend/app/services/style_detection.py`
- `backend/app/api/styles.py`
- `backend/app/services/prompt_registry.py`
- `frontend/src/app/styles/page.tsx`

### Task 1: Add Failing Tests For Rich Style DNA Mapping

**Files:**

- Create: `backend/tests/test_style_dna_detection.py`
- Modify: `backend/app/services/style_detection.py`

- [ ] **Step 1: Write the failing tests**

```python
from app.services.style_detection import features_to_rules


def test_features_to_rules_consumes_style_dna_fields():
    features = {
        "sentence_distribution": {"short_pct": 0.6, "long_pct": 0.1},
        "dialogue_ratio": 0.35,
        "description_density": 1.8,
        "ai_markers": [],
    }
    llm_analysis = {
        "style_labels": ["口语推进", "判断感强"],
        "distilled_rules": [
            "多用短句推进，不要把判断埋进长解释里。",
            "每隔一两段给读者一个明确态度。"
        ],
        "forbidden_patterns": [
            {"pattern": "空泛升华", "reason": "会冲淡原文判断力"}
        ],
        "writing_patterns": {
            "opening": "先抛判断再展开",
            "progression": "一层比一层更狠",
            "closing": "收在一句态度鲜明的话上",
        },
    }

    rules, anti_ai = features_to_rules(features, llm_analysis)

    assert any("先抛判断再展开" in item["rule"] for item in rules)
    assert any(item["pattern"] == "空泛升华" for item in anti_ai)
```

```python
import pytest


@pytest.mark.asyncio
async def test_detect_style_endpoint_stores_llm_analysis(auth_client, monkeypatch):
    from app.services import style_detection

    async def fake_detect_style_with_llm(text: str) -> dict:
        return {
            "persona_summary": "像一个会边分析边下判断的作者",
            "style_labels": ["口语推进", "判断感强"],
            "distilled_rules": ["先下判断，再补原因。"],
            "representative_quotes": ["说白了，这事没那么复杂。"],
        }

    monkeypatch.setattr(style_detection, "detect_style_with_llm", fake_detect_style_with_llm)

    resp = await auth_client.post("/api/styles/detect", json={"text": "甲" * 300, "name": "测试写法"})
    assert resp.status_code == 201
    payload = resp.json()
    assert "config_json" in payload
    assert "llm_analysis" in payload["config_json"]
    assert payload["tone_keywords"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
cd /root/ai-write
backend/.venv/bin/python -m pytest backend/tests/test_style_dna_detection.py -q
```

Expected:

- `features_to_rules` does not yet consume `distilled_rules`, `forbidden_patterns`, or `writing_patterns`
- `/api/styles/detect` does not yet call `detect_style_with_llm`

- [ ] **Step 3: Commit the red tests**

```bash
git add backend/tests/test_style_dna_detection.py
git commit -m "test: cover style dna mapping behavior"
```

### Task 2: Introduce A Dedicated Style DNA Prompt

**Files:**

- Modify: `backend/app/services/style_detection.py`
- Modify: `backend/app/services/prompt_registry.py`

- [ ] **Step 1: Add a dedicated built-in prompt asset**

```python
{
    "task_type": "style_dna_extraction",
    "name": "风格DNA提取",
    "description": "从样本文本中提取可迁移的语言习惯与禁忌",
    "mode": "structured",
    "system_prompt": "...严格输出 JSON，不输出 Markdown...",
}
```

The fallback prompt should enforce:

- verbatim phrases and representative quotes must be exact extracts
- derived conclusions and verbatim evidence are separate fields
- insufficient evidence must return fewer items, not invented items
- output keys must be stable JSON keys

- [ ] **Step 2: Replace the hardcoded prompt entry point**

```python
from app.services.prompt_loader import load_prompt

prompt = await load_prompt("style_dna_extraction", fallback=STYLE_DNA_PROMPT)
result = await router.generate(
    task_type="extraction",
    messages=[
        {"role": "system", "content": "你是风格逆向工程师，只输出 JSON。"},
        {"role": "user", "content": prompt + sample},
    ],
    max_tokens=1400,
)
```

- [ ] **Step 3: Add a richer fallback schema**

```python
STYLE_DNA_PROMPT = """
你是一位风格逆向工程师。任务不是概括写了什么，而是拆解这个人怎么写。

只输出 JSON，字段如下：
{
  "persona_summary": "string",
  "language_features": {
    "sentence_rhythm": "string",
    "word_choice": "string",
    "punctuation_habits": "string",
    "paragraph_rhythm": "string",
    "emotion_expression": "string"
  },
  "verbatim_phrase_groups": {
    "transition": ["string"],
    "judgment": ["string"],
    "self_mocking": ["string"],
    "emotion": ["string"],
    "reader_closeness": ["string"]
  },
  "writing_patterns": {
    "opening": "string",
    "progression": "string",
    "knowledge_injection": "string",
    "rhythm_breaks": "string",
    "closing": "string"
  },
  "forbidden_patterns": [
    {"pattern": "string", "reason": "string"}
  ],
  "style_labels": ["string"],
  "distilled_rules": ["string"],
  "representative_quotes": ["string"]
}
"""
```

- [ ] **Step 4: Run the tests**

Run:

```bash
cd /root/ai-write
backend/.venv/bin/python -m pytest backend/tests/test_style_dna_detection.py -q
```

Expected: still FAIL, because mapping and endpoint wiring are not done yet

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/style_detection.py backend/app/services/prompt_registry.py
git commit -m "feat: add dedicated style dna extraction prompt"
```

### Task 3: Distill The DNA Report Into Existing StyleProfile Fields

**Files:**

- Modify: `backend/app/services/style_detection.py`

- [ ] **Step 1: Extend `features_to_rules` to consume the new report**

```python
writing_patterns = llm_analysis.get("writing_patterns", {})
for label, key in [
    ("开篇方式", "opening"),
    ("论述推进", "progression"),
    ("知识引入", "knowledge_injection"),
    ("收束方式", "closing"),
]:
    value = writing_patterns.get(key)
    if value:
        rules.append({"rule": f"{label}：{value}", "weight": 0.82, "category": "style_dna"})
```

```python
for item in llm_analysis.get("distilled_rules", [])[:8]:
    rules.append({"rule": item[:150], "weight": 0.88, "category": "style_dna"})

for item in llm_analysis.get("forbidden_patterns", [])[:8]:
    if isinstance(item, dict) and item.get("pattern"):
        anti_ai.append({
            "pattern": item["pattern"],
            "replacement": item.get("reason", ""),
            "autoRewrite": False,
        })
```

- [ ] **Step 2: Preserve the full style DNA report for future UI**

The return value from `detect_style_with_llm` should remain the full structured JSON so API layers can store it under `config_json`.

- [ ] **Step 3: Run the tests**

Run:

```bash
cd /root/ai-write
backend/.venv/bin/python -m pytest backend/tests/test_style_dna_detection.py -q
```

Expected:

- `test_features_to_rules_consumes_style_dna_fields` passes
- endpoint test still fails because `/api/styles/detect` is not wired yet

- [ ] **Step 4: Commit**

```bash
git add backend/app/services/style_detection.py
git commit -m "feat: map style dna reports into style profile fields"
```

### Task 4: Wire `/api/styles/detect` To Use LLM Analysis

**Files:**

- Modify: `backend/app/api/styles.py`

- [ ] **Step 1: Make plain text detection use both layers**

```python
from app.services.style_detection import (
    detect_style_features,
    detect_style_with_llm,
    features_to_rules,
)

features = detect_style_features(body.text)
llm_analysis = await detect_style_with_llm(body.text[:5000])
rules, anti_ai = features_to_rules(features, llm_analysis)
```

- [ ] **Step 2: Store rich results in the profile**

```python
profile = StyleProfile(
    name=body.name or "自动检测的写法",
    description=f"从 {len(body.text)} 字文本中自动提取的写作风格",
    source_book="text_detection",
    rules_json=rules,
    anti_ai_rules=anti_ai,
    tone_keywords=list(dict.fromkeys(
        (llm_analysis.get("style_labels", []) if isinstance(llm_analysis.get("style_labels"), list) else [])
        + features.get("style_labels", [])
    )),
    sample_passages=(llm_analysis.get("representative_quotes", [])[:2] if isinstance(llm_analysis.get("representative_quotes"), list) else [])
    or [body.text[:500]],
    config_json={
        "detection_features": features,
        "llm_analysis": llm_analysis,
    },
)
```

- [ ] **Step 3: Re-run the tests**

Run:

```bash
cd /root/ai-write
backend/.venv/bin/python -m pytest backend/tests/test_style_dna_detection.py -q
```

Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add backend/app/api/styles.py
git commit -m "feat: use style dna analysis in text style detection"
```

### Task 5: Tighten The Frontend Copy Without Changing The Flow

**Files:**

- Modify: `frontend/src/app/styles/page.tsx`

- [ ] **Step 1: Update the modal copy so it matches the stronger detector**

```tsx
<p className="text-xs text-gray-500">
  粘贴一段参考小说文本（至少200字）。系统会同时分析语言节奏、常用表达、写法推进方式和禁忌，生成更完整的写法档案。
</p>
```

- [ ] **Step 2: Keep the UI surface unchanged**

Do not add a new complex report UI in this pass. The richer analysis should first live in `config_json`.

- [ ] **Step 3: Run frontend lint**

Run:

```bash
cd /root/ai-write/frontend
npm run lint
```

Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add frontend/src/app/styles/page.tsx
git commit -m "chore: align style detect copy with dna analysis"
```

### Task 6: Final Verification

**Files:**

- Test: `backend/tests/test_style_dna_detection.py`

- [ ] **Step 1: Run backend verification**

```bash
cd /root/ai-write
backend/.venv/bin/python -m pytest backend/tests/test_style_dna_detection.py -q
```

Expected: PASS

- [ ] **Step 2: Run frontend lint**

```bash
cd /root/ai-write/frontend
npm run lint
```

Expected: PASS

- [ ] **Step 3: Manual smoke**

Manual checklist:

- 从“写法”页粘贴文本，成功生成档案
- 结果 `rules_json`、`tone_keywords` 非空
- `config_json.llm_analysis` 中有更完整的 DNA 报告
- 试写结果比旧版更像“有具体写法的人”

- [ ] **Step 4: Commit final verification**

```bash
git add .
git commit -m "test: verify style dna extraction upgrade"
```
