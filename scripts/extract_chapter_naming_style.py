"""PR-CHAPTER-NAMING: Learn chapter naming convention from reference books.

Process:
1. Load all reference_book_slices that start with "第X章" pattern.
2. For each, extract chapter title (handles single-line "第1章 XXX" and
   multi-line "第一章\\n中文章名\\nEnglish Title\\n正文" formats).
3. Take the title + first ~600 chars of content as a sample.
4. Batch the samples (~25 each) and ask LLM to learn the naming
   convention: principles, naming_patterns, title-to-content relations,
   and curated example_titles.
5. Aggregate batches into a single chapter_naming_style structure.
6. Write to style_profiles.config_json["chapter_naming_style"] for the
   target style profile (default: 江南综合写法
   d39058bb-a22c-4511-80f6-3649df8eca12).
"""
import asyncio
import json
import re
import sys
import os
from pathlib import Path
sys.path.insert(0, '/app')

from sqlalchemy import select, text, update
from app.db.session import async_session_factory
from app.models.project import StyleProfile
from app.services.prompt_registry import run_text_prompt

TARGET_PROFILE_ID = os.environ.get("TARGET_PROFILE_ID", "d39058bb-a22c-4511-80f6-3649df8eca12")
BOOK_IDS = [
    "24498b6b-2698-4900-b44b-b42806964e1b",  # 龙族
    "67fe33f9-60e8-459e-9720-8546c828eab7",  # 天之炽
    "c33c2f19-03b4-46a1-ab0a-ede66175a7fe",  # 天之炽②女武神
]
CHAPTER_HEAD_RE = re.compile(r"^第.{1,5}章(?:[\s　]+(.+))?$")
CONTENT_CHAR_LIMIT = 600
BATCH_SIZE = 25


async def extract_chapter_samples(db) -> list[dict]:
    """Pull 第X章 slices and parse into (chapter_no, chinese_title, english_title, content_sample)."""
    rows = await db.execute(text("""
        SELECT rb.title AS book, rbs.book_id, rbs.sequence_id, rbs.raw_text
        FROM reference_book_slices rbs
        JOIN reference_books rb ON rb.id = rbs.book_id
        WHERE rbs.book_id::text = ANY(:book_ids)
          AND rbs.raw_text ~ '^第.{1,5}章'
        ORDER BY rbs.book_id, rbs.sequence_id
    """), {"book_ids": BOOK_IDS})
    headers = list(rows.fetchall())
    print(f"  Found {len(headers)} chapter-header slices")

    samples: list[dict] = []
    for book, book_id, seq_id, raw in headers:
        lines = raw.splitlines()
        if not lines:
            continue
        first = lines[0].strip()
        m = CHAPTER_HEAD_RE.match(first)
        if not m:
            continue
        # Pattern A: "第1章 XXX" (single-line, 天之炽 style)
        single_line_title = (m.group(1) or "").strip()
        chinese_title = single_line_title
        english_title = ""
        content_lines: list[str] = []

        if chinese_title:
            content_lines = lines[1:]
        else:
            # Pattern B: multi-line, 龙族 style
            # line 2 = chinese, optionally line 3 = english (latin-only check)
            if len(lines) >= 2:
                line2 = lines[1].strip()
                # 跳过多余空行
                if not line2 and len(lines) >= 3:
                    line2 = lines[2].strip()
                if line2 and not line2.startswith("第") and len(line2) <= 30:
                    chinese_title = line2
            if len(lines) >= 3:
                # try line 3 as english if mostly latin
                line3_idx = 2
                if not lines[1].strip():
                    line3_idx = 3 if len(lines) > 3 else 2
                if line3_idx < len(lines):
                    line3 = lines[line3_idx].strip()
                    if line3 and re.match(r"^[\x20-\x7E&\.,'\"—–‘’“” ]+$", line3) and len(line3) <= 80:
                        english_title = line3
                        content_lines = lines[line3_idx + 1:]
                    else:
                        content_lines = lines[line3_idx:]
            elif chinese_title:
                content_lines = lines[2:]
        if not chinese_title:
            continue
        content = "\n".join(l for l in content_lines if l.strip())
        if len(content) > CONTENT_CHAR_LIMIT:
            content = content[:CONTENT_CHAR_LIMIT].rstrip() + "…"
        # 下一个 slice 拼接为补充（只在 content 太短时）
        if len(content) < 200:
            nxt = await db.execute(text("""
                SELECT raw_text FROM reference_book_slices
                WHERE book_id::text = :bid AND sequence_id = :sid
            """), {"bid": str(book_id), "sid": seq_id + 1})
            nxt_row = nxt.first()
            if nxt_row:
                extra = (nxt_row[0] or "").strip()
                # 别拼下一章的标题
                if not CHAPTER_HEAD_RE.match(extra.splitlines()[0] if extra else ""):
                    content += "\n" + extra
                    if len(content) > CONTENT_CHAR_LIMIT:
                        content = content[:CONTENT_CHAR_LIMIT].rstrip() + "…"
        samples.append({
            "book": book,
            "chinese_title": chinese_title,
            "english_title": english_title,
            "content_sample": content,
        })
    print(f"  Parsed {len(samples)} chapter samples with names")
    return samples


def build_batch_prompt(samples: list[dict]) -> str:
    blocks = []
    for i, s in enumerate(samples, 1):
        en = f"\n  English: {s['english_title']}" if s['english_title'] else ""
        blocks.append(
            f"[#{i}] 《{s['book']}》\n  章名：{s['chinese_title']}{en}\n  章节开头：{s['content_sample']}"
        )
    return (
        "以下是一部优秀青春幻想小说（江南的龙族 / 天之炽 系列）的章节样本，每个样本给了章名 + 本章开头 600 字。"
        "请仅就作者「如何给章节起名」这一点进行可复用的风格学习，不要分析其他文笔。\n\n"
        + "\n\n".join(blocks)
    )


FINAL_AGG_INSTRUCTIONS = """你是一位资深中文青春幻想小说编辑。请针对「章节命名风格」这一独立维度输出严格的 JSON（不要 markdown，不要 fence，不要额外说明），模式如下：

{
  "overall_principles": [4-7 条高层原则, 例: "章名优先用名词性意象而非概括句", "避免用人名/地名直陈", …],
  "naming_patterns": [
    {
      "name": "诗化意象句",
      "description": "用 8–18 字含机染于主题的诗化长句。常为纯名词性句，不动词。",
      "examples": ["每只象龟心中都有一处温暖的水坑", "龟龟镶金龙。。。"]
    },
    {"name": "中英双语饱和", …},
    {"name": "女主象征型名词", …},
    …
  ],
  "title_content_relations": [
    {
      "name": "关键意象提炼",
      "description": "章名选本章出现一次但记忆点最重的具象物体/场景，如“焕焕金貘”取自某场战心中一次闪光"
    },
    {"name": "主题反语", "description": "章名说 A 但本章发生 B、汇演为 B，用反差加深记忆"},
    …
  ],
  "avoid_patterns": [4-6 条，指明不要怎么起名：如 "避免 'XX的恍惚' / 'XX之争' 这类老套路", "避免 谌语集句中的四字词装饱棒"],
  "length_distribution": {"short_2_4字": "30%", "medium_5_8字": "40%", "long_9_18字": "25%", "with_english": "5%"},
  "example_titles": [
    {"title": "每只象龟心中都有一处温暖的水坑", "english": "", "technique": "诗化意象句", "content_summary": "人物 X 的“象龟一生所求不过水坑”独白与本章谈话主题呼应"},
    …选 12-20 个高质量示例
  ]
}”

输入带 N 个批次学习结果，请去重 / 合并为一份统一的 chapter_naming_style 输出。"""


BATCH_INSTRUCTIONS = """你是一位资深中文青春幻想小说编辑。请仅针对「章节命名风格」在下面样本上进行运算思考，输出一份以下结构的严格 JSON（不要 markdown / 不要 fence / 不要额外说明）：

{
  "observations": [本批样本你看到的命名规律 5-10 条],
  "naming_patterns_in_batch": [{"name": "...", "description": "...", "sample_titles": ["...", "..."]}],
  "title_content_relations_in_batch": [{"name": "...", "description": "...", "example": "章名XXX vs 本章YYY"}],
  "good_titles": [{"title": "...", "english": "", "technique": "", "content_summary": "一句话概括本章与该章名的关联"}]
}

只输出这一个 JSON 对象，不要其他。"""


async def run_batch(samples: list[dict], batch_idx: int, db) -> dict:
    user_text = build_batch_prompt(samples)
    full_user = f"{BATCH_INSTRUCTIONS}\n\n=== 样本 (本批 {len(samples)} 个) ===\n{user_text}"
    print(f"  [batch {batch_idx}] sending {len(samples)} samples to LLM…")
    result = await run_text_prompt(
        "generation", full_user, db,
        messages=[
            {"role": "system", "content": "你是资深中文小说编辑，清醒、准确、只输出要求的 JSON。"},
            {"role": "user", "content": full_user},
        ],
    )
    text_out = (result.text if hasattr(result, "text") else str(result)) or ""
    # 提取 JSON
    text_out = text_out.strip()
    if text_out.startswith("```"):
        # strip fences
        text_out = re.sub(r"^```[a-z]*\s*|\s*```$", "", text_out, flags=re.S)
    # find first { to last }
    start = text_out.find("{")
    end = text_out.rfind("}")
    if start < 0 or end < 0:
        print(f"  [batch {batch_idx}] FAILED to find JSON, raw len={len(text_out)}")
        return {}
    payload = text_out[start:end+1]
    try:
        return json.loads(payload)
    except Exception as e:
        print(f"  [batch {batch_idx}] JSON parse failed: {e}")
        # try to clean trailing commas
        cleaned = re.sub(r",(\s*[\}\]])", r"\1", payload)
        try:
            return json.loads(cleaned)
        except Exception as e2:
            print(f"  [batch {batch_idx}] cleaned parse also failed: {e2}")
            return {}


async def run_aggregation(batches: list[dict], db) -> dict:
    user_text = json.dumps({"batches": batches}, ensure_ascii=False, indent=2)[:18000]
    full_user = f"{FINAL_AGG_INSTRUCTIONS}\n\n=== 批次输出汇总 ===\n{user_text}"
    print(f"  [aggregation] merging {len(batches)} batches…")
    result = await run_text_prompt(
        "generation", full_user, db,
        messages=[
            {"role": "system", "content": "你是资深中文小说编辑，只输出要求的 JSON。"},
            {"role": "user", "content": full_user},
        ],
    )
    text_out = ((result.text if hasattr(result, "text") else str(result)) or "").strip()
    if text_out.startswith("```"):
        text_out = re.sub(r"^```[a-z]*\s*|\s*```$", "", text_out, flags=re.S)
    start = text_out.find("{")
    end = text_out.rfind("}")
    if start < 0 or end < 0:
        print("  [aggregation] no JSON in response")
        return {}
    payload = text_out[start:end+1]
    try:
        return json.loads(payload)
    except Exception as e:
        print(f"  [aggregation] parse failed: {e}")
        cleaned = re.sub(r",(\s*[\}\]])", r"\1", payload)
        try:
            return json.loads(cleaned)
        except Exception as e2:
            print(f"  [aggregation] cleaned parse also failed: {e2}")
            return {}


async def main():
    print("=== PR-CHAPTER-NAMING extraction ===")
    async with async_session_factory() as db:
        # 1) Extract samples
        print("[1/4] extracting chapter samples from reference_book_slices…")
        samples = await extract_chapter_samples(db)
        if not samples:
            print("NO samples found, abort.")
            return

        # 2) Run batched LLM extraction
        print(f"[2/4] running batched LLM ({len(samples)} samples / batch={BATCH_SIZE})…")
        batches: list[dict] = []
        for i in range(0, len(samples), BATCH_SIZE):
            batch = samples[i:i + BATCH_SIZE]
            result = await run_batch(batch, i // BATCH_SIZE + 1, db)
            if result:
                batches.append(result)
        print(f"  Got {len(batches)} batch results")

        if not batches:
            print("NO batch results, abort.")
            return

        # 3) Aggregation
        print("[3/4] aggregating into final chapter_naming_style…")
        agg = await run_aggregation(batches, db)
        if not agg:
            # 如果聚合失败，退而用手工拼接
            print("  aggregation failed, falling back to merged batches")
            agg = {
                "overall_principles": [],
                "naming_patterns": [
                    p
                    for b in batches
                    for p in (b.get("naming_patterns_in_batch") or [])
                ][:10],
                "title_content_relations": [
                    r
                    for b in batches
                    for r in (b.get("title_content_relations_in_batch") or [])
                ][:8],
                "example_titles": [
                    t
                    for b in batches
                    for t in (b.get("good_titles") or [])
                ][:25],
            }

        # 4) Persist into style_profiles.config_json
        print("[4/4] writing chapter_naming_style into style_profile config_json…")
        prof = await db.get(StyleProfile, TARGET_PROFILE_ID)
        if not prof:
            print(f"profile {TARGET_PROFILE_ID} not found")
            return
        cfg = dict(prof.config_json or {})
        cfg["chapter_naming_style"] = agg
        cfg["chapter_naming_style"]["_meta"] = {
            "sample_count": len(samples),
            "batches": len(batches),
            "version": "v1",
        }
        prof.config_json = cfg
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(prof, "config_json")
        await db.commit()
        await db.refresh(prof)
        print(f"✅ wrote chapter_naming_style with {len(agg.get('example_titles') or [])} examples, {len(agg.get('naming_patterns') or [])} patterns")

        # save to disk too for visibility
        out_path = "/tmp/chapter_naming_style.json"
        Path(out_path).write_text(json.dumps(agg, ensure_ascii=False, indent=2))
        print(f"  also saved to {out_path}")

asyncio.run(main())
