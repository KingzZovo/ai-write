"""Filter word management — configurable Anti-AI word list with AI auto-detection."""

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.project import FilterWord

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/filter-words", tags=["filter-words"])

# Built-in seed words (inserted on first load if DB is empty)
BUILTIN_WORDS = [
    # AI 痕迹词
    {"word": "\u6b64\u5916", "category": "ai_trace", "severity": "high", "replacement": ""},
    {"word": "\u7136\u800c", "category": "ai_trace", "severity": "medium", "replacement": ""},
    {"word": "\u503c\u5f97\u6ce8\u610f\u7684\u662f", "category": "ai_trace", "severity": "high", "replacement": ""},
    {"word": "\u9700\u8981\u5f3a\u8c03\u7684\u662f", "category": "ai_trace", "severity": "high", "replacement": ""},
    {"word": "\u4e0d\u53ef\u5ffd\u89c6", "category": "ai_trace", "severity": "medium", "replacement": ""},
    {"word": "\u5f70\u663e", "category": "ai_trace", "severity": "high", "replacement": ""},
    {"word": "\u8be0\u91ca", "category": "ai_trace", "severity": "high", "replacement": ""},
    {"word": "\u8d4b\u80fd", "category": "ai_trace", "severity": "high", "replacement": ""},
    {"word": "\u6620\u5c04", "category": "ai_trace", "severity": "medium", "replacement": ""},
    {"word": "\u6298\u5c04", "category": "ai_trace", "severity": "medium", "replacement": ""},
    {"word": "\u4e0d\u7981", "category": "ai_trace", "severity": "high", "replacement": ""},
    {"word": "\u6cb9\u7136\u800c\u751f", "category": "ai_trace", "severity": "high", "replacement": ""},
    {"word": "\u5fc3\u6f6e\u6f8e\u6e43", "category": "ai_trace", "severity": "high", "replacement": ""},
    {"word": "\u7480\u74a8", "category": "ai_trace", "severity": "high", "replacement": ""},
    {"word": "\u7470\u4e3d", "category": "ai_trace", "severity": "high", "replacement": ""},
    {"word": "\u7184\u7184\u751f\u8f89", "category": "ai_trace", "severity": "high", "replacement": ""},
    {"word": "\u4eff\u4f5b", "category": "ai_trace", "severity": "medium", "replacement": ""},
    {"word": "\u5b9b\u5982", "category": "ai_trace", "severity": "medium", "replacement": ""},
    {"word": "\u72b9\u5982", "category": "ai_trace", "severity": "medium", "replacement": ""},
    {"word": "\u7f13\u7f13", "category": "ai_trace", "severity": "medium", "replacement": ""},
    {"word": "\u6df1\u6df1\u5730", "category": "ai_trace", "severity": "low", "replacement": ""},
    {"word": "\u9759\u9759\u5730", "category": "ai_trace", "severity": "low", "replacement": ""},
    {"word": "\u8fd9\u4e00\u523b", "category": "ai_trace", "severity": "medium", "replacement": ""},
    {"word": "\u4e0e\u6b64\u540c\u65f6", "category": "ai_trace", "severity": "high", "replacement": ""},
    {"word": "\u6beb\u65e0\u7591\u95ee", "category": "ai_trace", "severity": "medium", "replacement": ""},
    {"word": "\u4e0d\u8a00\u800c\u55bb", "category": "ai_trace", "severity": "medium", "replacement": ""},
    {"word": "\u65e0\u6cd5\u81ea\u62d4", "category": "cliche", "severity": "low", "replacement": ""},
    {"word": "\u8840\u6db2\u6cb8\u817e", "category": "cliche", "severity": "low", "replacement": ""},
    {"word": "\u7535\u5149\u706b\u77f3", "category": "cliche", "severity": "low", "replacement": ""},
    {"word": "\u5fc3\u5982\u6b7b\u7070", "category": "cliche", "severity": "low", "replacement": ""},
]


class FilterWordCreate(BaseModel):
    word: str
    category: str = "custom"
    severity: str = "medium"
    replacement: str = ""


class FilterWordUpdate(BaseModel):
    category: str | None = None
    severity: str | None = None
    replacement: str | None = None
    enabled: bool | None = None


class FilterWordResponse(BaseModel):
    id: UUID
    word: str
    category: str
    severity: str
    replacement: str
    source: str
    enabled: int
    hit_count: int

    model_config = {"from_attributes": True}


class AnalyzeRequest(BaseModel):
    text: str


@router.get("")
async def list_filter_words(
    category: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """List all filter words, optionally filtered by category."""
    # Auto-seed builtin words if empty
    count_result = await db.execute(select(func.count(FilterWord.id)))
    if count_result.scalar() == 0:
        for w in BUILTIN_WORDS:
            db.add(FilterWord(
                word=w["word"], category=w["category"],
                severity=w["severity"], replacement=w["replacement"],
                source="builtin",
            ))
        await db.flush()

    query = select(FilterWord).order_by(FilterWord.category, FilterWord.word)
    if category:
        query = query.where(FilterWord.category == category)
    result = await db.execute(query)
    words = result.scalars().all()

    # Group by category
    categories: dict[str, int] = {}
    for w in words:
        categories[w.category] = categories.get(w.category, 0) + 1

    return {
        "words": [FilterWordResponse.model_validate(w) for w in words],
        "total": len(words),
        "categories": categories,
    }


@router.post("", status_code=201)
async def add_filter_word(
    body: FilterWordCreate,
    db: AsyncSession = Depends(get_db),
) -> FilterWordResponse:
    """Add a custom filter word."""
    existing = await db.execute(
        select(FilterWord).where(FilterWord.word == body.word)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail=f"'{body.word}' \u5df2\u5b58\u5728")

    word = FilterWord(
        word=body.word, category=body.category,
        severity=body.severity, replacement=body.replacement,
        source="user",
    )
    db.add(word)
    await db.flush()
    await db.refresh(word)
    return FilterWordResponse.model_validate(word)


class BatchImportRequest(BaseModel):
    """Import filter words. Supports:
    - Simple word list: ["word1", "word2"]
    - Legado purification rules: ["ruleName##regex##replacement", ...]
    - Object list: [{"word": "...", "category": "...", "replacement": "..."}]
    """
    words: list[str | dict]


@router.post("/import", status_code=201)
async def import_filter_words(
    body: BatchImportRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Batch import filter words. Supports plain words, legado rules, and object format."""
    imported = 0
    skipped = 0

    for item in body.words:
        word_text = ""
        category = "custom"
        replacement = ""
        severity = "medium"

        if isinstance(item, dict):
            word_text = item.get("word", "").strip()
            category = item.get("category", "custom")
            replacement = item.get("replacement", "")
            severity = item.get("severity", "medium")
        elif isinstance(item, str):
            item = item.strip()
            if not item:
                continue
            # Legado purification rule format: name##regex##replacement
            if "##" in item:
                parts = item.split("##")
                if len(parts) >= 2:
                    word_text = parts[1].strip()  # regex pattern
                    replacement = parts[2].strip() if len(parts) > 2 else ""
                    category = "legado_rule"
                else:
                    word_text = item
            else:
                word_text = item

        if not word_text or len(word_text) > 100:
            skipped += 1
            continue

        # Skip duplicates
        existing = await db.execute(
            select(FilterWord).where(FilterWord.word == word_text)
        )
        if existing.scalar_one_or_none():
            skipped += 1
            continue

        db.add(FilterWord(
            word=word_text, category=category,
            severity=severity, replacement=replacement,
            source="import",
        ))
        imported += 1

    await db.flush()
    return {"imported": imported, "skipped": skipped}


@router.put("/{word_id}")
async def update_filter_word(
    word_id: str,
    body: FilterWordUpdate,
    db: AsyncSession = Depends(get_db),
) -> FilterWordResponse:
    """Update a filter word."""
    word = await db.get(FilterWord, word_id)
    if not word:
        raise HTTPException(status_code=404, detail="Not found")
    if body.category is not None:
        word.category = body.category
    if body.severity is not None:
        word.severity = body.severity
    if body.replacement is not None:
        word.replacement = body.replacement
    if body.enabled is not None:
        word.enabled = 1 if body.enabled else 0
    await db.flush()
    await db.refresh(word)
    return FilterWordResponse.model_validate(word)


@router.delete("/{word_id}", status_code=204)
async def delete_filter_word(
    word_id: str,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a filter word."""
    word = await db.get(FilterWord, word_id)
    if not word:
        raise HTTPException(status_code=404, detail="Not found")
    await db.delete(word)


@router.post("/analyze")
async def analyze_text_for_ai_words(
    body: AnalyzeRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Scan text for filter word matches and return hits."""
    result = await db.execute(
        select(FilterWord).where(FilterWord.enabled == 1)
    )
    words = result.scalars().all()

    hits: list[dict] = []
    for w in words:
        count = body.text.count(w.word)
        if count > 0:
            hits.append({
                "word": w.word,
                "category": w.category,
                "severity": w.severity,
                "replacement": w.replacement,
                "count": count,
            })
            w.hit_count = (w.hit_count or 0) + count

    await db.flush()
    hits.sort(key=lambda h: h["count"], reverse=True)
    return {"hits": hits, "total_hits": sum(h["count"] for h in hits)}


@router.post("/ai-detect")
async def ai_detect_new_words(
    body: AnalyzeRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Use LLM to analyze text and discover new AI-characteristic words/phrases."""
    from app.services.model_router import get_model_router

    router = get_model_router()

    try:
        result = await router.generate(
            task_type="extraction",
            messages=[
                {"role": "system", "content": (
                    "\u4f60\u662f\u4e00\u4e2a AI \u5199\u4f5c\u75d5\u8ff9\u68c0\u6d4b\u4e13\u5bb6\u3002"
                    "\u5206\u6790\u4ee5\u4e0b\u5c0f\u8bf4\u6587\u672c\uff0c\u627e\u51fa\u5176\u4e2d\u5177\u6709"
                    " AI \u751f\u6210\u7279\u5f81\u7684\u8bcd\u8bed\u548c\u8868\u8fbe\u3002"
                    "\u8f93\u51fa JSON \u683c\u5f0f\uff1a"
                    '[{"word": "\u8bcd\u8bed", "reason": "\u539f\u56e0", '
                    '"severity": "high/medium/low", '
                    '"replacement": "\u66ff\u4ee3\u5efa\u8bae"}]'
                )},
                {"role": "user", "content": body.text[:3000]},
            ],
            max_tokens=1024,
        )

        import json
        text = result.text.strip()
        if text.startswith("```"):
            lines = text.split("\n")[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines)

        detected = json.loads(text)
        if not isinstance(detected, list):
            detected = []

        # Auto-add new words to DB
        added = 0
        for item in detected:
            word = item.get("word", "").strip()
            if not word or len(word) < 2:
                continue
            existing = await db.execute(
                select(FilterWord).where(FilterWord.word == word)
            )
            if existing.scalar_one_or_none():
                continue
            db.add(FilterWord(
                word=word,
                category="ai_trace",
                severity=item.get("severity", "medium"),
                replacement=item.get("replacement", ""),
                source="ai_detected",
            ))
            added += 1

        await db.flush()
        return {
            "detected": detected,
            "added_to_db": added,
            "message": f"\u53d1\u73b0 {len(detected)} \u4e2a AI \u7279\u5f81\u8bcd\uff0c\u65b0\u589e {added} \u4e2a",
        }

    except Exception as e:
        logger.warning("AI detection failed: %s", e)
        return {"detected": [], "added_to_db": 0, "message": f"\u68c0\u6d4b\u5931\u8d25: {e}"}
