"""
Text Cleaning & Slicing Pipeline

Processes raw novel text through:
1. Format parsing (TXT/EPUB/HTML)
2. Non-content stripping (ads, navigation, copyright)
3. Chapter boundary detection (regex + LLM fallback)
4. Block slicing (≤1500 chars per block, paragraph-preserving)
5. Metadata annotation (sequence IDs, char counts)
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Common chapter heading patterns for Chinese novels
CHAPTER_PATTERNS = [
    re.compile(r"^第[一二三四五六七八九十百千零\d]+[章节回卷集部篇]", re.MULTILINE),
    re.compile(r"^Chapter\s+\d+", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^第\s*\d+\s*[章节回]", re.MULTILINE),
    re.compile(r"^[\d]+[、.．]\s*\S+", re.MULTILINE),
]

# Common noise patterns to strip
NOISE_PATTERNS = [
    re.compile(r"(本章未完.*?点击下一页继续)", re.DOTALL),
    re.compile(r"(手机用户请浏览.*?阅读)", re.DOTALL),
    re.compile(r"(请记住本书.*?网址)", re.DOTALL),
    re.compile(r"(最新章节.*?地址)", re.DOTALL),
    re.compile(r"(一秒记住.*?免费阅读)", re.DOTALL),
    re.compile(r"(温馨提示.*?书签)", re.DOTALL),
    re.compile(r"www\.\S+\.(com|net|org|cc|cn)", re.IGNORECASE),
    re.compile(r"https?://\S+"),
]

MAX_BLOCK_CHARS = 1500


@dataclass
class ChapterData:
    """A parsed chapter from a novel."""
    chapter_idx: int
    title: str
    content: str
    char_count: int = 0

    def __post_init__(self):
        self.char_count = len(self.content)


@dataclass
class TextBlock:
    """A text block after slicing."""
    chapter_idx: int
    block_idx: int
    chapter_title: str
    content: str
    char_count: int
    sequence_id: int  # global sequential ID


@dataclass
class ParseResult:
    """Result of parsing a novel text."""
    title: str = ""
    author: str = ""
    chapters: list[ChapterData] = field(default_factory=list)
    total_chars: int = 0


# =============================================================================
# Format Parsers
# =============================================================================


def parse_txt(text: str) -> ParseResult:
    """Parse a plain text novel into chapters."""
    text = _strip_noise(text)
    chapters = _detect_chapters(text)
    total = sum(ch.char_count for ch in chapters)
    return ParseResult(chapters=chapters, total_chars=total)


def parse_html(html: str) -> ParseResult:
    """Parse HTML content into chapters."""
    soup = BeautifulSoup(html, "lxml")

    # Remove script, style, nav elements
    for tag in soup.find_all(["script", "style", "nav", "header", "footer", "aside"]):
        tag.decompose()

    text = soup.get_text(separator="\n")
    text = _strip_noise(text)
    chapters = _detect_chapters(text)
    total = sum(ch.char_count for ch in chapters)

    title = ""
    title_tag = soup.find("title")
    if title_tag:
        title = title_tag.get_text(strip=True)

    return ParseResult(title=title, chapters=chapters, total_chars=total)


def parse_epub(file_data: bytes) -> ParseResult:
    """Parse an EPUB file into chapters."""
    import ebooklib
    from ebooklib import epub

    book = epub.read_epub(io.BytesIO(file_data))
    title = book.get_metadata("DC", "title")
    title_str = title[0][0] if title else ""
    author = book.get_metadata("DC", "creator")
    author_str = author[0][0] if author else ""

    all_text_parts: list[str] = []
    for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
        soup = BeautifulSoup(item.get_content(), "lxml")
        for tag in soup.find_all(["script", "style"]):
            tag.decompose()
        text = soup.get_text(separator="\n")
        if text.strip():
            all_text_parts.append(text)

    full_text = "\n\n".join(all_text_parts)
    full_text = _strip_noise(full_text)
    chapters = _detect_chapters(full_text)
    total = sum(ch.char_count for ch in chapters)

    return ParseResult(
        title=title_str,
        author=author_str,
        chapters=chapters,
        total_chars=total,
    )


# =============================================================================
# Chapter Detection
# =============================================================================


def _detect_chapters(text: str) -> list[ChapterData]:
    """Detect chapter boundaries using regex patterns."""
    # Find all chapter heading positions
    headings: list[tuple[int, str]] = []
    for pattern in CHAPTER_PATTERNS:
        for match in pattern.finditer(text):
            headings.append((match.start(), match.group().strip()))

    # Deduplicate and sort by position
    headings = sorted(set(headings), key=lambda x: x[0])

    if not headings:
        # No chapter headings found — treat entire text as one chapter
        clean = text.strip()
        if clean:
            return [ChapterData(chapter_idx=1, title="Chapter 1", content=clean)]
        return []

    chapters: list[ChapterData] = []
    for i, (pos, heading) in enumerate(headings):
        # Chapter content goes from this heading to the next
        start = pos
        end = headings[i + 1][0] if i + 1 < len(headings) else len(text)
        content = text[start:end].strip()

        # Extract title (first line) and body
        lines = content.split("\n", 1)
        title = lines[0].strip()
        body = lines[1].strip() if len(lines) > 1 else ""

        if body:
            chapters.append(ChapterData(
                chapter_idx=i + 1,
                title=title,
                content=body,
            ))

    return chapters


# =============================================================================
# Text Cleaning
# =============================================================================


def _strip_noise(text: str) -> str:
    """Remove ads, URLs, and other non-content noise."""
    for pattern in NOISE_PATTERNS:
        text = pattern.sub("", text)

    # Collapse excessive whitespace
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


# =============================================================================
# Block Slicing
# =============================================================================


def slice_chapters_to_blocks(
    chapters: list[ChapterData],
    max_chars: int = MAX_BLOCK_CHARS,
) -> list[TextBlock]:
    """
    Slice chapters into text blocks of ≤max_chars.
    Preserves paragraph boundaries where possible.
    """
    blocks: list[TextBlock] = []
    global_seq = 0

    for chapter in chapters:
        paragraphs = [p.strip() for p in chapter.content.split("\n") if p.strip()]
        current_block: list[str] = []
        current_len = 0
        block_idx = 0

        for para in paragraphs:
            para_len = len(para)

            if para_len > max_chars:
                # Flush current block
                if current_block:
                    content = "\n".join(current_block)
                    blocks.append(TextBlock(
                        chapter_idx=chapter.chapter_idx,
                        block_idx=block_idx,
                        chapter_title=chapter.title,
                        content=content,
                        char_count=len(content),
                        sequence_id=global_seq,
                    ))
                    global_seq += 1
                    block_idx += 1
                    current_block = []
                    current_len = 0

                # Split long paragraph
                for start in range(0, para_len, max_chars):
                    chunk = para[start:start + max_chars]
                    blocks.append(TextBlock(
                        chapter_idx=chapter.chapter_idx,
                        block_idx=block_idx,
                        chapter_title=chapter.title,
                        content=chunk,
                        char_count=len(chunk),
                        sequence_id=global_seq,
                    ))
                    global_seq += 1
                    block_idx += 1
                continue

            if current_len + para_len > max_chars and current_block:
                # Flush current block
                content = "\n".join(current_block)
                blocks.append(TextBlock(
                    chapter_idx=chapter.chapter_idx,
                    block_idx=block_idx,
                    chapter_title=chapter.title,
                    content=content,
                    char_count=len(content),
                    sequence_id=global_seq,
                ))
                global_seq += 1
                block_idx += 1
                current_block = []
                current_len = 0

            current_block.append(para)
            current_len += para_len

        # Flush remaining
        if current_block:
            content = "\n".join(current_block)
            blocks.append(TextBlock(
                chapter_idx=chapter.chapter_idx,
                block_idx=block_idx,
                chapter_title=chapter.title,
                content=content,
                char_count=len(content),
                sequence_id=global_seq,
            ))
            global_seq += 1

    return blocks


# =============================================================================
# Full Pipeline
# =============================================================================


def process_text_file(
    content: str | bytes,
    filename: str,
) -> tuple[ParseResult, list[TextBlock]]:
    """
    Run the full text processing pipeline on an uploaded file.

    Args:
        content: File content (str for TXT/HTML, bytes for EPUB)
        filename: Original filename for format detection

    Returns:
        Tuple of (ParseResult, list of TextBlocks)
    """
    ext = Path(filename).suffix.lower()

    if ext == ".epub":
        if isinstance(content, str):
            content = content.encode("utf-8")
        result = parse_epub(content)
    elif ext in (".html", ".htm"):
        if isinstance(content, bytes):
            content = content.decode("utf-8", errors="ignore")
        result = parse_html(content)
    else:
        # Default to TXT
        if isinstance(content, bytes):
            content = content.decode("utf-8", errors="ignore")
        result = parse_txt(content)

    blocks = slice_chapters_to_blocks(result.chapters)

    logger.info(
        "Processed %s: %d chapters, %d blocks, %d total chars",
        filename,
        len(result.chapters),
        len(blocks),
        result.total_chars,
    )

    return result, blocks
