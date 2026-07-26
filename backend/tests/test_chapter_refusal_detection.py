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
