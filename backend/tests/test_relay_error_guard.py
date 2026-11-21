"""回归:网关/中转层把基础设施报错当正文返回时,不得被当成已完成稿件。

实测(2026-11-14,质量验收基线):对 `http://127.0.0.1:8317/v1/chat/completions`
发一次写作请求,HTTP 200、`finish_reason == "stop"`,但 `message.content` 是:

    ⚠️ Upstream Gemini returned an empty response. If this Worker runs on
    Cloudflare/serverless, Google may be blocking the egress IP (works locally
    but empty in production); also verify GEMINI_BL is current. Run
    `wrangler tail` to see the upstream status.

这段文字是运维诊断,不是小说正文。原稿能通过 ``looks_truncated`` 只是巧合:它最后一个
字符是 ASCII 句点,而中文终句符集合里没有 "."。同样的诊断只要以「。」或英文单词收尾,
现有三道门(空稿 / 截断 / 拒答话术)全部放行。``looks_like_relay_error`` 补这一类,
并接入 ``chapter_persist_status``(命中即 draft,不静默标 completed)。
"""
from __future__ import annotations

import pytest

from app.services.chapter_quality_gate import (
    chapter_persist_status,
    looks_like_refusal,
    looks_like_relay_error,
    looks_truncated,
)

OBSERVED_RELAY_ERROR = (
    "⚠️ Upstream Gemini returned an empty response. If this Worker runs on "
    "Cloudflare/serverless, Google may be blocking the egress IP (works locally "
    "but empty in production); also verify GEMINI_BL is current. Run "
    "`wrangler tail` to see the upstream status."
)
# 同一段诊断改成以中文终句符结尾 —— 这正是现有守卫会漏掉的情形。
RELAY_ERROR_CJK_TERMINATED = OBSERVED_RELAY_ERROR.rstrip(".") + "。"


def test_same_diagnostic_ending_on_cjk_slips_past_existing_guards():
    """固定缺陷:换成中文终句符后,截断门与拒答门都放行。"""
    assert looks_truncated(RELAY_ERROR_CJK_TERMINATED) is False
    assert looks_like_refusal(RELAY_ERROR_CJK_TERMINATED) is False


def test_relay_error_is_detected_and_never_marked_completed():
    assert looks_like_relay_error(OBSERVED_RELAY_ERROR) is True
    assert looks_like_relay_error(RELAY_ERROR_CJK_TERMINATED) is True
    assert chapter_persist_status(RELAY_ERROR_CJK_TERMINATED) == "draft"


@pytest.mark.parametrize("text", [
    "upstream status 502 from provider",
    "no available channel for model gemini-3.5-flash under group default",
    "auth_unavailable: no auth available (providers=codex, model=gpt-6-luna)",
    "model_not_found: no channel for this model",
    '{"error": "invalid api key"}',
    "Worker returned an empty response",
    "request id: 202611141452264296736678268d9d60LZ78ztG",
])
def test_other_relay_diagnostics_are_detected(text):
    assert looks_like_relay_error(text) is True


@pytest.mark.parametrize("text", [
    # 正常中文小说正文:不含运维词表。
    "他把信封压在碗下,天亮前渡船会来。江面上起了雾,灯影在水里晃。",
    # 人物在故事里谈论“上游/通道”等普通词。
    "“这条河的上游还有一道坝。”老周把烟头摁灭,指着水痕说。",
    # 对白里出现 error 一类英文词,仍属叙事。
    "屏幕上跳出一行 ERROR,她伸手把电源拔了。",
    # 单一弱标记不足以判定。
    "他翻出一张写着 GEMINI_BL 的旧标签,随手丢进火里。",
])
def test_ordinary_prose_is_not_flagged(text):
    assert looks_like_relay_error(text) is False


def test_empty_text_is_not_flagged():
    assert looks_like_relay_error("") is False
    assert looks_like_relay_error("   \n  ") is False


# 2026-11-16 实测(网关二进制 + 上游透传):中转/上游还有一批把错误当正文的形态。
# 单条 STRONG(网关错误码)或整段同现的 WEAK(上游对预算耗尽的自然语言说明)。
@pytest.mark.parametrize("text", [
    "terminal_empty_incomplete",
    "stream error: upstream terminated with incomplete empty response (0 tokens)",
    "model_not_supported: no channel for this model",
    "quota_exhausted: resets_at 2026-11-17T00:00:00Z",
    "upstream execution failed: provider=deepseek model=deepseek-chat",
    "no available clients for any model in handler type: openai",
])
def test_gateway_error_codes_are_detected(text):
    assert looks_like_relay_error(text) is True


def test_upstream_budget_diagnostic_is_detected_and_not_marked_completed():
    text = ("The model used its entire output budget on reasoning and returned "
            "no answer. Raise max_tokens or simplify the request.")
    assert looks_like_relay_error(text) is True
    assert chapter_persist_status(text) == "draft"


def test_single_generic_phrase_alone_is_not_a_relay_error():
    """宽泛词单独出现不足以定性:避免把正常英文文本误判成诊断。"""
    assert looks_like_relay_error("She asked him to simplify the request.") is False

def test_account_verification_diagnostic_is_detected():
    """2026-11-17 实测:中转把"账号未验证"当 HTTP-200 正文返回(中文前缀 + 英文错误码)。

    这是同一类缺陷的新变体;发生在章节生成上就会把诊断当正文落库。
    """
    text = ('[错误] 账号未验证,需要人工完成验证: 创建会话 HTTP 403: '
            '{"error":{"details":{"error_code":"account_needs_verification",'
            '"error_visibility":"user_facing"},"message":"account_needs_verification",'
            '"type":"permission_error"},"request_id":"req_x"}')
    assert looks_like_relay_error(text) is True
    assert chapter_persist_status(text) == "draft"
