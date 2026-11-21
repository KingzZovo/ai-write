"""Bind a summary to its source and output without guessing legacy provenance."""
import hashlib
from typing import Any


def summary_source_hash(title: str | None, content: str | None, summary: str | None) -> str:
    # Include the output too: an outline sync or another writer must not reuse
    # the source stamp while replacing the summary with an unrelated proposal.
    title, content, summary = title or "", content or "", summary or ""
    framed = f"{len(title)}:{title}{len(content)}:{content}{summary}"
    return hashlib.sha256(framed.encode("utf-8")).hexdigest()


def summary_is_current(chapter: Any) -> bool:
    return bool((chapter.content_text or "").strip() and (chapter.summary or "").strip()
                and chapter.summary_source_hash == summary_source_hash(
        chapter.title, chapter.content_text, chapter.summary
    ))


def current_summary_sql():
    """Verified summary expression; hash in PostgreSQL without returning prose.

    PostgreSQL length counts Unicode characters, matching Python len. The
    framing is identical to summary_source_hash, including the summary output.
    """
    from sqlalchemy import case, func
    from app.models.project import Chapter

    title = func.coalesce(Chapter.title, "")
    content = func.coalesce(Chapter.content_text, "")
    summary = func.coalesce(Chapter.summary, "")
    framed = func.concat(func.length(title), ":", title, func.length(content), ":", content, summary)
    fingerprint = func.encode(func.sha256(func.convert_to(framed, "UTF8")), "hex")
    return case((
        (Chapter.summary_source_hash == fingerprint)
        & (func.btrim(summary) != "") & (func.btrim(content) != ""),
        Chapter.summary,
    ), else_=None)
