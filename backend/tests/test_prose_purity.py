"""正文净化:服务话术/诊断文本的剥离(2026-11-20)。

回归来源是**真实产物**:`docs/evaluation/style-ref-10/ch01.txt` 全文 1992 字,
前 1923 字是正常正文,末尾紧接
`I seem to be encountering an error. Can I try something else for you?`
—— 旧守卫只认"整章拒答 / 头部拒答",于是这种"正文 + 尾随话术"一路通过验收并落库。
"""

from __future__ import annotations

import re

import pytest

from app.services.prose_purity import (
    find_service_noise,
    looks_like_service_noise,
    strip_service_noise,
)

# 真实观测到的尾部话术(逐字)
OBSERVED_TAIL = "I seem to be encountering an error. Can I try something else for you?"

_BODY = "他把信封往桌角推了推,指尖在粗糙的陶釉上停了两秒才缩回袖口里。"


def _body(n: int = 40) -> str:
    return _BODY * n


def test_real_observed_tail_noise_is_stripped_and_body_kept():
    text = _body() + OBSERVED_TAIL
    result = strip_service_noise(text)
    assert result.removed, "真实观测到的尾部话术必须被识别"
    assert result.fatal is False, "正文充足时不应整章作废"
    assert OBSERVED_TAIL not in result.text
    assert result.text.rstrip().endswith("袖口里。")
    # 正文部分逐字节保留
    assert result.text.startswith(_BODY)


def test_head_glued_noise_keeps_the_prose_after_it():
    """头部粘连:"话术 + 正文"必须剥话术留正文,而不是整段丢掉。"""
    text = OBSERVED_TAIL + "夕阳将渡口边的江面染成一片浑浊的橘红。" + _body()
    result = strip_service_noise(text)
    assert result.removed
    assert result.fatal is False
    assert " encountering " not in result.text
    assert result.text.startswith("夕阳将渡口边的江面")


def test_mid_document_english_refusal_paragraph_is_removed():
    text = _body() + "\n\nI'm sorry, but I can't help with that request.\n\n" + _body()
    result = strip_service_noise(text)
    assert result.removed
    assert "sorry" not in result.text.lower()
    assert result.fatal is False


@pytest.mark.parametrize("text", [
    "upstream status 502 from provider",
    "no available channel for model gemini-3.5-flash under group default",
    "auth_unavailable: no auth available (providers=codex)",
    "Worker returned an empty response",
])
def test_relay_diagnostics_are_stripped(text):
    """中转把自己的错误当 HTTP-200 正文透传:这类串在小说里没有叙事用途。"""
    result = strip_service_noise(_body() + text)
    assert result.removed
    assert result.text.strip().endswith("袖口里。")


def test_dialogue_mentioning_refusal_is_not_noise():
    """人物在故事里说"我无法帮助你"是正常叙事 —— 引号内不算污染。"""
    text = "“我无法帮助你。”老周说。" + _body()
    assert looks_like_service_noise(text) is False
    assert strip_service_noise(text).removed == []


def test_english_word_inside_narration_is_not_noise():
    text = _body() + "屏幕上跳出一行 ERROR,她伸手把电源拔了。" + _body()
    assert looks_like_service_noise(text) is False


def test_clean_prose_is_untouched_byte_for_byte():
    text = _body(80)
    result = strip_service_noise(text)
    assert result.removed == []
    assert result.text == text


def test_whole_chapter_noise_is_fatal():
    """整章都是话术时不能当正文落库,必须判整章无效。"""
    result = strip_service_noise(OBSERVED_TAIL)
    assert result.fatal is True
    assert not re.sub(r"\s", "", result.text)


def test_residual_below_minimum_is_fatal():
    """剥完只剩几句残句 → 判整章无效,而不是把残句当章节。"""
    text = "他把门关上。" + OBSERVED_TAIL
    result = strip_service_noise(text, min_keep_chars=300)
    assert result.fatal is True


def test_find_service_noise_reports_located_spans():
    spans = find_service_noise(_body() + OBSERVED_TAIL)
    assert len(spans) == 1
    assert spans[0].offset >= len(_BODY) * 40 - 1
    assert "encountering" in spans[0].excerpt
