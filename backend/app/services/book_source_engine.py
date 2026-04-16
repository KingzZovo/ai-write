"""
Legado Book Source Rule Engine

A Python interpreter for legado (阅读) book source JSON rules.
Supports the core rule types needed for novel content extraction:
- @css: CSS selectors (via parsel)
- @xpath: XPath selectors (via lxml)
- @json: JSONPath selectors
- @regex: Regular expressions
- @js: JavaScript snippets (via dukpy, limited)
- Plain text rules (default, interpreted as CSS or regex patterns)

Key legado BookSource JSON fields:
- bookSourceUrl: Base URL of the source site
- searchUrl: URL template for search (with {{key}}, {{page}} vars)
- ruleSearch: Rules to parse search results (bookList, name, author, bookUrl, etc.)
- ruleBookInfo: Rules to parse book detail page (name, author, intro, tocUrl, etc.)
- ruleToc: Rules to parse table of contents (chapterList, chapterName, chapterUrl)
- ruleContent: Rules to parse chapter content (content, replaceRegex, etc.)
- ruleExplore: Rules to parse explore/ranking pages (similar to ruleSearch)
- header: HTTP headers JSON string
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, quote_plus

import httpx
from parsel import Selector

logger = logging.getLogger(__name__)

# Rate limiting defaults
DEFAULT_REQUEST_DELAY = 1.0  # seconds between requests


@dataclass
class BookInfo:
    title: str = ""
    author: str = ""
    book_url: str = ""
    cover_url: str = ""
    intro: str = ""
    kind: str = ""  # genre/tags
    last_chapter: str = ""
    word_count: str = ""


@dataclass
class ChapterInfo:
    title: str = ""
    url: str = ""
    index: int = 0


@dataclass
class BookSourceConfig:
    """Parsed legado book source configuration."""
    source_url: str = ""
    source_name: str = ""
    search_url: str = ""
    explore_url: str = ""
    header: dict = field(default_factory=dict)
    charset: str = "utf-8"

    # Rule sections
    rule_search: dict = field(default_factory=dict)
    rule_book_info: dict = field(default_factory=dict)
    rule_toc: dict = field(default_factory=dict)
    rule_content: dict = field(default_factory=dict)
    rule_explore: dict = field(default_factory=dict)


class RuleParser:
    """Parses and applies legado-style rules to HTML/JSON content."""

    @staticmethod
    def apply_rule(content: str, rule: str, base_url: str = "") -> list[str]:
        """
        Apply a single rule to content and return matched results.

        Rule prefixes:
        - @css: CSS selector
        - @xpath: XPath expression
        - @json: JSONPath (simplified)
        - @regex: Regular expression
        - No prefix: try CSS first, then text match
        """
        if not rule or not content:
            return []

        rule = rule.strip()

        # Handle rule chains (separated by ||)
        if "||" in rule:
            parts = rule.split("||")
            for part in parts:
                result = RuleParser.apply_rule(content, part.strip(), base_url)
                if result:
                    return result
            return []

        # Handle rule combinations (separated by &&)
        if "&&" in rule and not rule.startswith("@regex:"):
            parts = rule.split("&&")
            results = []
            for part in parts:
                r = RuleParser.apply_rule(content, part.strip(), base_url)
                results.extend(r)
            return results

        # Dispatch by prefix
        if rule.startswith("@css:"):
            return RuleParser._css(content, rule[5:])
        elif rule.startswith("@xpath:"):
            return RuleParser._xpath(content, rule[7:])
        elif rule.startswith("@json:") or rule.startswith("$."):
            json_rule = rule[6:] if rule.startswith("@json:") else rule
            return RuleParser._jsonpath(content, json_rule)
        elif rule.startswith("@regex:"):
            return RuleParser._regex(content, rule[7:])
        elif rule.startswith("@js:"):
            return RuleParser._js(content, rule[4:])
        else:
            # Default: try as CSS selector
            return RuleParser._css(content, rule)

    @staticmethod
    def apply_rule_single(content: str, rule: str, base_url: str = "") -> str:
        """Apply a rule and return the first match as a string."""
        results = RuleParser.apply_rule(content, rule, base_url)
        return results[0] if results else ""

    @staticmethod
    def _css(html: str, selector: str) -> list[str]:
        """Apply CSS selector."""
        try:
            # Handle text extraction markers
            text_mode = False
            attr = None
            clean_sel = selector.strip()

            if clean_sel.endswith("@text"):
                text_mode = True
                clean_sel = clean_sel[:-5].strip()
            elif "@" in clean_sel:
                parts = clean_sel.rsplit("@", 1)
                clean_sel = parts[0].strip()
                attr = parts[1].strip()

            sel = Selector(text=html)
            elements = sel.css(clean_sel)

            results = []
            for el in elements:
                if attr:
                    val = el.attrib.get(attr, "")
                    if val:
                        results.append(val)
                elif text_mode:
                    text = el.css("::text").getall()
                    results.append(" ".join(text).strip())
                else:
                    # Get inner text by default
                    text = el.css("::text").getall()
                    if text:
                        results.append(" ".join(text).strip())
                    else:
                        results.append(el.get() or "")
            return results
        except Exception as e:
            logger.debug("CSS selector failed: %s — %s", selector, e)
            return []

    @staticmethod
    def _xpath(html: str, expr: str) -> list[str]:
        """Apply XPath expression."""
        try:
            sel = Selector(text=html)
            results = sel.xpath(expr).getall()
            return [r.strip() for r in results if r.strip()]
        except Exception as e:
            logger.debug("XPath failed: %s — %s", expr, e)
            return []

    @staticmethod
    def _jsonpath(content: str, path: str) -> list[str]:
        """Simple JSONPath implementation for common patterns."""
        try:
            data = json.loads(content) if isinstance(content, str) else content

            # Handle simple dot-notation paths like $.data.list[*].name
            parts = path.replace("$.", "").replace("[*]", ".*").split(".")
            results = _traverse_json(data, parts)
            return [str(r) for r in results if r]
        except Exception as e:
            logger.debug("JSONPath failed: %s — %s", path, e)
            return []

    @staticmethod
    def _regex(content: str, pattern: str) -> list[str]:
        """Apply regex pattern."""
        try:
            # Handle group extraction: pattern##group_index
            group_idx = 0
            if "##" in pattern:
                pattern, idx_str = pattern.rsplit("##", 1)
                group_idx = int(idx_str)

            matches = re.findall(pattern, content)
            if matches:
                if isinstance(matches[0], tuple):
                    return [m[group_idx] for m in matches]
                return matches
            return []
        except Exception as e:
            logger.debug("Regex failed: %s — %s", pattern, e)
            return []

    @staticmethod
    def _js(content: str, script: str) -> list[str]:
        """Execute JavaScript snippet (limited, via dukpy)."""
        try:
            import dukpy
            # Pass content as a dukpy variable to avoid injection via template literals
            result = dukpy.evaljs(
                f"var result = ''; var src = dukpy.content; {script}; result;",
                content=content,
            )
            if result:
                return [str(result)]
            return []
        except Exception as e:
            logger.debug("JS execution failed: %s", e)
            return []


class BookSourceEngine:
    """
    Interprets and executes legado book source rules.

    Workflow:
    1. parse_source(): Parse raw JSON into BookSourceConfig
    2. search(): Search for books using searchUrl + ruleSearch
    3. get_book_info(): Get book details using ruleBookInfo
    4. get_toc(): Get chapter list using ruleToc
    5. get_content(): Get chapter text using ruleContent
    6. explore(): Browse rankings using ruleExplore
    """

    def __init__(self):
        self.client = httpx.AsyncClient(
            timeout=30,
            follow_redirects=True,
            verify=False,  # Many book sources have expired/self-signed certs
            headers={
                "User-Agent": "Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36 Chrome/107.0 Mobile Safari/537.36",
            },
        )
        self.parser = RuleParser()

    def parse_source(self, source_json: dict) -> BookSourceConfig:
        """Parse a legado BookSource JSON into config."""
        config = BookSourceConfig(
            source_url=source_json.get("bookSourceUrl", ""),
            source_name=source_json.get("bookSourceName", ""),
            search_url=source_json.get("searchUrl", ""),
            explore_url=source_json.get("exploreUrl", ""),
            rule_search=source_json.get("ruleSearch", {}),
            rule_book_info=source_json.get("ruleBookInfo", {}),
            rule_toc=source_json.get("ruleToc", {}),
            rule_content=source_json.get("ruleContent", {}),
            rule_explore=source_json.get("ruleExplore", {}),
        )

        # Parse headers
        header_str = source_json.get("header", "")
        if header_str:
            try:
                config.header = json.loads(header_str)
            except (json.JSONDecodeError, TypeError):
                pass

        # Charset
        config.charset = source_json.get("charset", "utf-8") or "utf-8"

        return config

    async def search(
        self,
        config: BookSourceConfig,
        keyword: str,
        page: int = 1,
    ) -> list[BookInfo]:
        """Search for books using the source's search rules."""
        parsed = self._parse_url_template(config.search_url, config.source_url, key=keyword, page=page)
        if not parsed["url"]:
            return []

        html = await self._fetch_request(parsed, config)
        if not html:
            return []

        return self._parse_book_list(html, config.rule_search, config.source_url)

    def get_explore_categories(self, config: BookSourceConfig) -> list[dict]:
        """Get available explore categories (e.g., 三江推荐, 月票排行)."""
        if not config.explore_url:
            return []

        categories = []
        explore = config.explore_url.strip()

        # Format 1: JSON array [{title, url}, ...]
        if explore.startswith("["):
            try:
                items = json.loads(explore)
                for i, item in enumerate(items):
                    if isinstance(item, dict) and item.get("url"):
                        categories.append({
                            "index": i,
                            "title": item.get("title", f"分类{i+1}"),
                            "url": item["url"],
                        })
                return categories
            except (json.JSONDecodeError, TypeError):
                pass

        # Format 2: "title::url\ntitle2::url2"
        for i, line in enumerate(explore.split("\n")):
            line = line.strip()
            if not line:
                continue
            if "::" in line:
                title, url = line.split("::", 1)
                categories.append({"index": i, "title": title.strip(), "url": url.strip()})
            else:
                categories.append({"index": i, "title": f"默认{i+1}", "url": line})

        return categories

    async def explore(
        self,
        config: BookSourceConfig,
        page: int = 1,
        category_index: int = 0,
    ) -> list[BookInfo]:
        """Browse the explore/ranking page for a specific category."""
        categories = self.get_explore_categories(config)
        if not categories:
            return []

        idx = min(category_index, len(categories) - 1)
        raw_url = categories[idx]["url"]
        parsed = self._parse_url_template(raw_url, config.source_url, page=page)

        if not parsed["url"]:
            return []

        html = await self._fetch_request(parsed, config)
        if not html:
            return []

        return self._parse_book_list(html, config.rule_explore, config.source_url)

    async def get_book_info(
        self,
        config: BookSourceConfig,
        book_url: str,
    ) -> BookInfo:
        """Get book detail from its page."""
        full_url = urljoin(config.source_url, book_url)
        html = await self._fetch(full_url, config)
        if not html:
            return BookInfo(book_url=book_url)

        rules = config.rule_book_info
        info = BookInfo(
            title=self.parser.apply_rule_single(html, rules.get("name", "")),
            author=self.parser.apply_rule_single(html, rules.get("author", "")),
            book_url=book_url,
            intro=self.parser.apply_rule_single(html, rules.get("intro", "")),
            cover_url=self.parser.apply_rule_single(html, rules.get("coverUrl", "")),
            kind=self.parser.apply_rule_single(html, rules.get("kind", "")),
            last_chapter=self.parser.apply_rule_single(html, rules.get("lastChapter", "")),
            word_count=self.parser.apply_rule_single(html, rules.get("wordCount", "")),
        )
        return info

    async def get_toc(
        self,
        config: BookSourceConfig,
        book_url: str,
    ) -> list[ChapterInfo]:
        """Get chapter list (table of contents)."""
        # Get tocUrl from book info rules
        toc_url = book_url
        if config.rule_book_info.get("tocUrl"):
            full_url = urljoin(config.source_url, book_url)
            html = await self._fetch(full_url, config)
            if html:
                toc_url = self.parser.apply_rule_single(
                    html, config.rule_book_info["tocUrl"]
                ) or book_url

        full_url = urljoin(config.source_url, toc_url)
        html = await self._fetch(full_url, config)
        if not html:
            return []

        rules = config.rule_toc
        list_rule = rules.get("chapterList", "")
        name_rule = rules.get("chapterName", "")
        url_rule = rules.get("chapterUrl", "")

        if not list_rule:
            return []

        # Get chapter elements
        items = self.parser.apply_rule(html, list_rule)
        chapters: list[ChapterInfo] = []

        for idx, item_html in enumerate(items):
            name = self.parser.apply_rule_single(item_html, name_rule) if name_rule else f"Chapter {idx+1}"
            ch_url = self.parser.apply_rule_single(item_html, url_rule) if url_rule else ""

            if ch_url:
                ch_url = urljoin(full_url, ch_url)

            chapters.append(ChapterInfo(
                title=name.strip(),
                url=ch_url,
                index=idx,
            ))

        return chapters

    async def get_content(
        self,
        config: BookSourceConfig,
        chapter_url: str,
    ) -> str:
        """Get chapter text content."""
        full_url = urljoin(config.source_url, chapter_url)
        html = await self._fetch(full_url, config)
        if not html:
            return ""

        rules = config.rule_content
        content_rule = rules.get("content", "")
        replace_rules = rules.get("replaceRegex", "")

        content = self.parser.apply_rule_single(html, content_rule)

        # Apply replacement rules
        if replace_rules and content:
            content = self._apply_replace_regex(content, replace_rules)

        # Clean HTML tags if any remain
        content = re.sub(r"<br\s*/?>", "\n", content, flags=re.IGNORECASE)
        content = re.sub(r"<[^>]+>", "", content)
        content = re.sub(r"&nbsp;", " ", content)
        content = re.sub(r"\n{3,}", "\n\n", content)

        return content.strip()

    # =========================================================================
    # Internal helpers
    # =========================================================================

    def _parse_url_template(
        self,
        url_template: str,
        base_url: str,
        key: str = "",
        page: int = 1,
    ) -> dict:
        """Parse a legado URL template into url + method + body + charset.

        Legado formats:
          - Simple: "https://site.com/search?q={{key}}"
          - With config: '/search,{"method":"POST","body":"kw={{key}}","charset":"gbk"}'
          - @js: JavaScript (not supported, skip)
        """
        result: dict[str, Any] = {"url": "", "method": "GET", "body": "", "charset": "", "headers": {}}
        if not url_template:
            return result

        template = url_template.strip()

        # Skip @js: templates
        if template.startswith("@js:"):
            return result

        # Split URL from config JSON — find the first '{' after the URL part
        url_part = template
        config_json: dict = {}

        # Look for JSON config: url,{...} or url\n{...}
        for sep in [",{", "\n{"]:
            idx = template.find(sep)
            if idx > 0:
                url_part = template[:idx].strip()
                json_str = template[idx + 1:] if sep == ",{" else template[idx + 1:]
                try:
                    config_json = json.loads("{" + json_str if not json_str.startswith("{") else json_str)
                except (json.JSONDecodeError, TypeError):
                    pass
                break

        # Replace template variables in URL
        url_part = url_part.replace("{{key}}", quote_plus(key))
        url_part = url_part.replace("{{page}}", str(page))
        url_part = url_part.replace("{{Page}}", str(page))
        url_part = url_part.replace("searchKey", quote_plus(key))
        url_part = url_part.replace("searchPage", str(page))

        if not url_part.startswith("http"):
            url_part = urljoin(base_url, url_part)

        result["url"] = url_part
        result["method"] = config_json.get("method", "GET").upper()
        result["charset"] = config_json.get("charset", "")

        # Replace template variables in body
        body = config_json.get("body", "")
        if body:
            body = body.replace("{{key}}", key)  # Don't URL-encode body values
            body = body.replace("{{page}}", str(page))
            body = body.replace("searchKey", key)
            body = body.replace("searchPage", str(page))
        result["body"] = body

        if config_json.get("headers"):
            try:
                if isinstance(config_json["headers"], str):
                    result["headers"] = json.loads(config_json["headers"])
                else:
                    result["headers"] = config_json["headers"]
            except (json.JSONDecodeError, TypeError):
                pass

        return result

    async def _fetch_request(self, parsed: dict, config: BookSourceConfig) -> str:
        """Fetch URL content using parsed request config (supports GET and POST)."""
        url = parsed["url"]
        if not url:
            return ""

        try:
            headers = dict(config.header) if config.header else {}
            headers.update(parsed.get("headers", {}))
            charset = parsed.get("charset") or config.charset or "utf-8"

            if parsed["method"] == "POST" and parsed.get("body"):
                # Set Content-Type for form data if not already set
                if "Content-Type" not in headers and "content-type" not in headers:
                    headers["Content-Type"] = "application/x-www-form-urlencoded"
                response = await self.client.post(
                    url,
                    headers=headers,
                    content=parsed["body"].encode(charset, errors="ignore"),
                )
            else:
                response = await self.client.get(url, headers=headers)

            response.raise_for_status()

            if charset.lower() != "utf-8":
                return response.content.decode(charset, errors="ignore")
            return response.text
        except Exception as e:
            logger.warning("Failed to fetch %s: %s", url, e)
            return ""

    async def _fetch(self, url: str, config: BookSourceConfig) -> str:
        """Simple GET fetch for plain URLs."""
        return await self._fetch_request({"url": url, "method": "GET", "body": "", "charset": "", "headers": {}}, config)

    def _parse_book_list(
        self,
        html: str,
        rules: dict,
        base_url: str,
    ) -> list[BookInfo]:
        """Parse a book list (search results or explore results)."""
        list_rule = rules.get("bookList", "")
        if not list_rule:
            return []

        items = self.parser.apply_rule(html, list_rule)
        books: list[BookInfo] = []

        for item_html in items:
            book = BookInfo(
                title=self.parser.apply_rule_single(item_html, rules.get("name", "")),
                author=self.parser.apply_rule_single(item_html, rules.get("author", "")),
                book_url=self.parser.apply_rule_single(item_html, rules.get("bookUrl", "")),
                cover_url=self.parser.apply_rule_single(item_html, rules.get("coverUrl", "")),
                intro=self.parser.apply_rule_single(item_html, rules.get("intro", "")),
                kind=self.parser.apply_rule_single(item_html, rules.get("kind", "")),
                last_chapter=self.parser.apply_rule_single(item_html, rules.get("lastChapter", "")),
                word_count=self.parser.apply_rule_single(item_html, rules.get("wordCount", "")),
            )
            if book.book_url:
                book.book_url = urljoin(base_url, book.book_url)
            if book.title:
                books.append(book)

        return books

    def _apply_replace_regex(self, content: str, replace_rules: str) -> str:
        """Apply legado replaceRegex rules to content."""
        # Format: "pattern##replacement##flags" separated by \n
        rules = replace_rules.split("\n") if "\n" in replace_rules else [replace_rules]
        for rule in rules:
            rule = rule.strip()
            if not rule:
                continue
            parts = rule.split("##")
            pattern = parts[0] if parts else ""
            replacement = parts[1] if len(parts) > 1 else ""
            if pattern:
                try:
                    content = re.sub(pattern, replacement, content)
                except re.error:
                    pass
        return content

    async def close(self):
        """Close the HTTP client."""
        await self.client.aclose()


# =============================================================================
# JSON traversal helper
# =============================================================================


def _traverse_json(data: Any, path_parts: list[str]) -> list:
    """Simple JSON path traversal."""
    if not path_parts:
        return [data] if data is not None else []

    current = path_parts[0]
    remaining = path_parts[1:]

    if current == "*":
        # Wildcard: iterate over list or dict values
        if isinstance(data, list):
            results = []
            for item in data:
                results.extend(_traverse_json(item, remaining))
            return results
        elif isinstance(data, dict):
            results = []
            for val in data.values():
                results.extend(_traverse_json(val, remaining))
            return results
        return []

    if isinstance(data, dict):
        if current in data:
            return _traverse_json(data[current], remaining)
        return []

    if isinstance(data, list):
        try:
            idx = int(current)
            if 0 <= idx < len(data):
                return _traverse_json(data[idx], remaining)
        except ValueError:
            pass
        return []

    return []
