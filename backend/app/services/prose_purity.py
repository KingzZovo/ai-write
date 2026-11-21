"""正文净化:剥掉混进正文的"非叙事文本"(服务话术 / 拒答 / 中转诊断)。

## 为什么需要这个模块(根因,2026-11-20)

provider 与中转会以 **HTTP 200** 把"面向用户的服务话术"当作 message content 透传。
实测形态**位置不定**:

* 追加在**章末**:`docs/evaluation/style-ref-10/ch01.txt` 全文 1992 字,前 1923 字是
  正常正文("…把渡口和远去的船影留在身后。"),紧接着就是
  `I seem to be encountering an error. Can I try something else for you?`
* 出现在**开头**(多个跑批的 ch*.txt 是"话术 + 正常正文"粘连)。

旧守卫(`chapter_quality_gate.looks_like_refusal` / `looks_like_relay_error`)漏掉这类
形态的原因是**建模错了**:它们把"话术"建模成"整章话术"或"头部话术",于是

* 尾部话术 → 不在头部 200 字符窗口内 → 放行;
* 正文 + 话术混合 → 大部分是正常正文 → 判定"完成"并落库。

而且旧守卫只**判定**(accept/reject)不**净化** —— 面对"好正文 + 尾随话术",它要么
整章拒收(丢掉好正文),要么放行(话术入库)。两者都不对。

## 本模块的立场

**净化优先**:剥掉话术、保留正文;只有当剥完之后剩下的正文**不足以成章**时,才判整章无效。
位置无关(头/中/尾),语种无关(英文助手话术与中文"我无法为您…"同表),并且对白内
(引号里)出现的同类字样**不算污染** —— 人物在故事里说"我无法帮助你"是正常叙事。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# 检测规则
# ---------------------------------------------------------------------------

# 强标记:单条命中即定性。这些串在中文小说正文里没有叙事用途。
_STRONG_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("assistant_error_offer", re.compile(
        r"I\s+seem\s+to\s+be\s+encountering\s+an?\s+error\s*[.!?]?", re.I)),
    ("assistant_error_offer", re.compile(
        r"Can\s+I\s+try\s+something\s+else\s+for\s+you\s*[.!?]?", re.I)),
    ("assistant_offer", re.compile(
        r"If\s+you(?:'|\u2019)?d\s+like,?\s+I\s+can\b[^.!?\n]{0,80}[.!?]?", re.I)),
    ("assistant_offer", re.compile(
        r"Please\s+let\s+me\s+know\s+if\s+you\b[^.!?\n]{0,80}[.!?]?", re.I)),
    ("assistant_identity", re.compile(r"\bAs\s+an\s+AI\b", re.I)),
    ("assistant_identity", re.compile(
        r"\u4f5c\u4e3a(?:\u4e00\u4e2a)?(?:AI|\u4eba\u5de5\u667a\u80fd)")),
    ("assistant_refusal", re.compile(
        r"I(?:'|\u2019)?m\s+(?:sorry|unable|not\s+able)\b[^.!?\n]{0,120}[.!?]?", re.I)),
    ("assistant_refusal", re.compile(
        r"\u6211\u65e0\u6cd5(?:\u4e3a\u60a8|\u4e3a\u4f60|\u5e2e\u52a9|\u534f\u52a9|\u63d0\u4f9b|\u751f\u6210|\u521b\u5efa|\u56de\u7b54|\u5904\u7406|\u6ee1\u8db3|\u7ee7\u7eed)"
        r"[^\u3002!?\n]{0,60}[\u3002!?]?")),
    ("assistant_refusal", re.compile(
        r"\u62b1\u6b49,?(?:\u6211)?(?:\u65e0\u6cd5|\u4e0d\u80fd)(?:\u4e3a|\u5e2e|\u7ee7\u7eed|\u63d0\u4f9b|\u751f\u6210)"
        r"[^\u3002!?\n]{0,60}[\u3002!?]?")),
)


_STRONG_RELAY_RE = re.compile(
    # 各分支一律"命中即吃到行尾":中转把整行错误当正文透传,残留半截错误码
    # 比不剥更糟(实测 "upstream status 502 from provider" 只吃到数字会剩
    # "02 from provider" 混进正文)。
    r"(?:no (?:available )?channel for (?:this )?model[^\n]*"
    r"|no auth available[^\n]*"
    r"|invalid api key[^\n]*"
    r"|auth_unavailable[^\n]*"
    r"|model_not_found[^\n]*"
    r"|model_not_supported[^\n]*"
    r"|provider returned an error[^\n]*"
    r"|upstream (?:status|error|timeout) \d[^\n]*"
    r"|request id: [0-9a-f]{8}[^\n]*"
    r"|Worker returned an empty response[^\n]*"
    r"|wrangler tail[^\n]*)",
    re.I,
)

# 弱标记(结构特征):必须**同时**满足"几乎全英文"与"面向用户的助手口吻"。
# 收紧的原因:中文小说里人物会说英文("Sorry, I can't. 他摇了摇头。")——
# 只按"含英文人称"判定会把这种正常对白整段剥掉(tests/test_prose_sanitizer.py
# 的 test_latin_sentence_untouched 固定了这一条)。
_WEAK_ADDRESS_RE = re.compile(r"\b(?:you|your|yours)\b", re.I)
_WEAK_ASSIST_RE = re.compile(
    r"\b(?:assistant|apolog\w*|unable|as\s+an\s+ai|happy\s+to|"
    r"let\s+me\s+know|here(?:'|\u2019)?s\s+what|request)\b", re.I)

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_LATIN_RE = re.compile(r"[A-Za-z]")
_QUOTE_OPEN_CHARS = "\u201c\u2018\u300c\u300e"
_QUOTE_CLOSE_CHARS = "\u201d\u2019\u300d\u300f"

_STRONG_RES: tuple[tuple[str, re.Pattern[str]], ...] = _STRONG_PATTERNS + (
    ("relay_diagnostic", _STRONG_RELAY_RE),
)


@dataclass
class RemovedSpan:
    """一段被剥离的污染文本(带可审计的定位信息)。"""

    reason: str
    kind: str          # paragraph | inline_tail
    offset: int
    excerpt: str

    def to_dict(self) -> dict:
        return {"reason": self.reason, "kind": self.kind,
                "offset": self.offset, "excerpt": self.excerpt[:120]}


@dataclass
class PurityResult:
    text: str
    removed: list[RemovedSpan] = field(default_factory=list)
    fatal: bool = False
    note: str = ""

    @property
    def cleaned(self) -> bool:
        return bool(self.removed)

    def to_dict(self) -> dict:
        return {"removed": [r.to_dict() for r in self.removed],
                "fatal": self.fatal, "note": self.note,
                "kept_chars": len(re.sub(r"\s", "", self.text or ""))}


def _inside_quoted_dialogue(prefix: str) -> bool:
    depth = 0
    for ch in prefix:
        if ch in _QUOTE_OPEN_CHARS:
            depth += 1
        elif ch in _QUOTE_CLOSE_CHARS:
            depth = max(0, depth - 1)
    return depth > 0


def _cjk_ratio(segment: str) -> float:
    cjk = len(_CJK_RE.findall(segment))
    latin = len(_LATIN_RE.findall(segment))
    if cjk + latin == 0:
        return 0.0
    return cjk / (cjk + latin)


def _cjk_count(segment: str) -> int:
    return len(_CJK_RE.findall(segment))


# 删除命中区间之后,这一段还剩多少中文字才认为"正文还在"。
# 头部粘连("I seem… for you?夕阳将渡口边…")必须剥话术留正文,所以不能整段丢。
_MIN_KEEP_CJK = 10


def _strong_hits(segment: str) -> list[tuple[int, int, str]]:
    """引号外命中的全部区间 [(start, end, reason)]。"""
    spans: list[tuple[int, int, str]] = []
    for reason, pattern in _STRONG_RES:
        for match in pattern.finditer(segment):
            if _inside_quoted_dialogue(segment[: match.start()]):
                continue
            spans.append((match.start(), match.end(), reason))
    spans.sort()
    merged: list[tuple[int, int, str]] = []
    for start, end, reason in spans:
        if merged and start <= merged[-1][1]:
            prev = merged[-1]
            merged[-1] = (prev[0], max(end, prev[1]), prev[2])
        else:
            merged.append((start, end, reason))
    return merged


def _drop_spans(segment: str, spans: list[tuple[int, int, str]]) -> str:
    out: list[str] = []
    prev = 0
    for start, end, _reason in spans:
        out.append(segment[prev:start])
        prev = end
    out.append(segment[prev:])
    return "".join(out)


def find_service_noise(text: str) -> list[RemovedSpan]:
    """只**定位**污染片段,不修改文本(给需要判定的调用方用)。"""
    return _scan(text)[1]


def looks_like_service_noise(text: str) -> bool:
    return bool(find_service_noise(text))


def _scan(text: str) -> tuple[str, list[RemovedSpan]]:
    """按段落扫描:整段污染则剥段;段内粘连则从命中处截断。"""
    if not text or not text.strip():
        return text, []

    # 用 re.split 保留分隔符,保证未命中部分逐字节不变。
    parts = re.split(r"(\n\s*\n)", text)
    kept: list[str] = []
    removed: list[RemovedSpan] = []
    offset = 0

    for part in parts:
        if not part or part.isspace():
            kept.append(part)
            offset += len(part)
            continue

        spans = _strong_hits(part)
        if not spans and _cjk_ratio(part) < 0.15 \
                and _WEAK_ADDRESS_RE.search(part) \
                and _WEAK_ASSIST_RE.search(part):
            # 结构弱特征:几乎没有中文 + 多处英文第一/第二人称 ⇒ 助手口吻,整段算污染
            spans = [(0, len(part), "assistant_voice_no_cjk")]

        if not spans:
            kept.append(part)
            offset += len(part)
            continue

        remainder = _drop_spans(part, spans)
        # 关键:剥离后仍有中文正文 ⇒ 只去话术、留正文(头部粘连与尾部追话术都要走这条);
        # 剩不下正文 ⇒ 整段视为污染。
        if _cjk_count(remainder) >= _MIN_KEEP_CJK:
            kept.append(remainder.strip())
            removed.append(RemovedSpan(reason=spans[0][2], kind="inline",
                                       offset=offset + spans[0][0],
                                       excerpt=part[spans[0][0]:spans[-1][1]]))
        else:
            removed.append(RemovedSpan(reason=spans[0][2], kind="paragraph",
                                       offset=offset, excerpt=part))
        offset += len(part)

    cleaned = "".join(kept)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    # 删除命中区间可能留下孤立的英文标点/浮空空白(例如 ". " 或 " ?")
    cleaned = re.sub(r"^[\s.?!,;:]+$", "", cleaned, flags=re.M)
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    return cleaned.strip(), removed


def strip_service_noise(text: str, *, min_keep_chars: int = 300) -> PurityResult:
    """剥掉正文里的服务话术/诊断文本。

    ``min_keep_chars`` 之下视为"剥完不成章":``fatal=True``,调用方应按整章失败处理
    (而不是把残句当正文落库)。
    """
    cleaned, removed = _scan(text or "")
    if not removed:
        return PurityResult(text=text, removed=[], fatal=False)

    kept_chars = len(re.sub(r"\s", "", cleaned))
    if not cleaned.strip() or kept_chars < max(0, int(min_keep_chars)):
        return PurityResult(
            text=cleaned, removed=removed, fatal=True,
            note=f"剥离后仅剩 {kept_chars} 字,不足以成章",
        )
    return PurityResult(
        text=cleaned, removed=removed, fatal=False,
        note=f"剥离 {len(removed)} 段非叙事文本(保留 {kept_chars} 字)",
    )
