"""Regression: an image-model refusal phrase must not be persisted as prose.

Live failure (2026-07-04 验收测试): relay occasionally misroutes a scene_writer
text call to an image model, which returns a refusal like

    您登录了吗？我可以搜索图片，但目前似乎无法为您创建任何图片。
    也有可能您所在的地区尚未开通图片创建功能。

The orchestrator appended this as a scene into the chapter body. Because it ends
on 「。」 it slipped past `looks_truncated` and the chapter was saved "completed"
carrying an assistant refusal instead of story prose — silent content corruption
(1 of 5 chapters in the acceptance run).

`looks_like_refusal` detects known assistant/service refusal boilerplate so the
caller can withhold "completed" (mark draft / trigger regeneration) the same way
it does for truncated chapters.
"""
from __future__ import annotations

import pytest

from app.services.chapter_quality_gate import looks_like_refusal


@pytest.mark.parametrize("text", [
    # The exact live failure phrase.
    "您登录了吗？我可以搜索图片，但目前似乎无法为您创建任何图片。也有可能您所在的地区尚未开通图片创建功能。",
    # Refusal buried at the tail of otherwise real prose (how it actually appears).
    "顾远走进值班室，四周一片死寂。\n\n我可以搜索图片，但目前似乎无法为您创建任何图片。",
    # Region-not-enabled variant on its own.
    "您所在的地区尚未开通图片创建功能。",
])
def test_flags_image_model_refusal(text):
    assert looks_like_refusal(text) is True


@pytest.mark.parametrize("text", [
    # Legitimate prose that merely mentions 图片 / photos — must NOT be flagged.
    "她翻看着手机里的旧图片，一张张都是父亲的笑脸。",
    # A character logging into a system in-story — not an assistant refusal.
    "系统弹出提示：您登录了吗？江临盯着屏幕，冷笑一声。",
    # Ordinary chapter ending.
    "他终于把门关上了。",
    # A scene describing image search as story action.
    "顾远在数据库里搜索图片，屏幕上跳出上百张监控截图。",
])
def test_accepts_legitimate_prose(text):
    assert looks_like_refusal(text) is False


def test_empty_or_blank_is_not_refusal():
    assert looks_like_refusal("") is False
    assert looks_like_refusal("   \n  ") is False


# ---------------------------------------------------------------------------
# PR-REFUSAL-BROADEN (2026-07-26 audit): generic assistant refusal openers,
# matched only in the head of the text (the function runs on the full chapter
# body at persist time, so a whole-output refusal sits at the opening) and only
# outside quoted dialogue.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    # Generic Chinese refusals (assistant-verb continuations of 我无法).
    "我无法帮助您完成这个请求。",
    "抱歉，我不能继续创作这个故事。",
    "抱歉，我无法提供这样的内容。",
    "作为AI，我不能生成包含此类情节的正文。",
    "作为人工智能助手，我无法满足这个请求。",
    # English refusal openers (any of these opening a Chinese chapter is a
    # misrouted chat reply, not prose).
    "I'm sorry, but I can't continue with this request.",
    "I cannot assist with that request.",
    "As an AI language model, I cannot write this chapter.",
    "I can’t help with writing that scene.",
])
def test_flags_generic_refusal_openers(text):
    assert looks_like_refusal(text) is True


_LONG_PROSE_HEAD = (
    "陈青沿着旧纸巷慢慢往前走，巷子两侧的门板都合着口，雨水顺着瓦沿往下滴。"
    "他把伞压得更低了一些，脚步没有停。铺子里的灯一盏一盏灭下去，只有拐角"
    "那家还亮着半扇窗。他在窗下站了一会儿，听见里面有人翻动纸页的声音，很轻，"
    "像是怕惊动了什么。雨下得更密了，他把领口拢紧，继续往巷子深处走。"
    "走到第三个门洞前，他停下来，抬手敲了三下。没有人应声，他又敲了三下，"
    "指节在潮湿的木门上敲出发闷的响。门后传来椅子腿蹭过地面的声音，"
    "接着是一阵拖沓的脚步，由远及近，在门内停住了。"
)
assert len(_LONG_PROSE_HEAD) > 220  # markers appended below sit past the head window


@pytest.mark.parametrize("text", [
    # Mid-chapter dialogue: a character saying 我无法… deep in the chapter
    # (beyond the head window) must NOT be flagged.
    _LONG_PROSE_HEAD + "\n\n她低声说：“我无法帮助你，这件事只能你自己去面对。”",
    # Opening dialogue: quoted speech at the very start is dialogue, not
    # assistant boilerplate.
    "“我无法帮助你。”她说完就把门关上了。",
    # A robot character speaking in-story, quoted.
    "机器人低声道：“作为AI，我也会做梦。”江临愣住了。",
    # First-person narration: 我无法 without an assistant-verb continuation.
    "我无法忘记那天的雨。它下了整整一夜，把巷子口的灯都浇灭了。",
    # Generic marker appearing only beyond the head window, unquoted narration.
    _LONG_PROSE_HEAD + "\n\n作为人工智能研究所的旧址，这栋楼早就没人进出了。",
])
def test_generic_refusal_guard_rejects_in_story_usage(text):
    assert looks_like_refusal(text) is False
