"""大纲里的金额要摆到写手面前,而且不能把后文剧情提前说出去。


实测(2026-11-14 连续 24 章):第 7 章让角色"算出卖这批活能挣多少",模型自己写了
880;第 8 章大纲钉的是 820。读者一眼就看出来,而当时没有任何机制会拦。
"""
import pytest

from app.services.context_pack import ContextPack
from app.services.numeric_pins import (
    amount_spans,
    chapter_label,
    pin_block,
    pin_fragments,
)


def _texts(matches):
    return [m.group(0) for m in matches]


def test_label_indices_are_not_money():
    assert amount_spans("[VOL-1/CH-8; id=x] 的正文") == []


def test_colloquial_amounts_without_unit_are_recognized():
    assert pin_fragments("校服交了,周芸收下八百二。") == ["校服交了,周芸收下八百二"]


def test_amounts_with_space_and_unit_are_recognized():
    assert pin_fragments("他把抽屉里的 1200 元分成三份。") == ["他把抽屉里的 1200 元分成三份"]


def test_time_words_are_not_money():
    assert pin_fragments("十年后他回到渡口。铺租还是八百。") == ["铺租还是八百"]


def test_fragment_does_not_cross_sentence():
    fragment = pin_fragments("上一句很长很长很长很长很长很长很长很长。复查费一千二。")[0]
    assert fragment.startswith("复查费") and "上一句" not in fragment


def test_pin_block_labels_and_caps_lines():
    blocks = [("CH-7", "工钱八百八十块。"), ("CH-8", "工钱八百二十块。")]
    block = pin_block(blocks)
    assert block.splitlines() == ["[CH-7] 工钱八百八十块", "[CH-8] 工钱八百二十块"]


def test_pin_block_drops_duplicate_lines():
    block = pin_block([("CH-7", "工钱八百。"), ("CH-8", "工钱八百。")])
    assert len(block.splitlines()) == 1


def test_chapter_label_extracts_number():
    assert chapter_label("[VOL-1/CH-12; id=abc]《第十二章》") == "CH-12"
    assert chapter_label("没有编号") == ""


def test_context_pack_injects_amounts_from_future_outlines():
    pack = ContextPack(future_outlines=[
        "[VOL-1/CH-8; id=x]《第八章 校服的钱》: 校服交了,周芸收下八百二。"])
    prompt = pack.to_system_prompt(token_budget=16000)
    assert "大纲已定的金额" in prompt and "八百二" in prompt


def test_context_pack_injects_amounts_from_volume_summaries():
    pack = ContextPack(volume_outline={"chapter_summaries": [
        {"chapter_idx": 16, "title": "九百块的铺子", "summary": "老刘说下半年铺租涨到九百。",
         "key_events": ["老刘说清九百的期限"]}]})
    prompt = pack.to_system_prompt(token_budget=16000)
    assert "[CH-16]" in prompt and "九百" in prompt


def test_context_pack_omits_block_when_no_amounts_are_planned():
    pack = ContextPack(future_outlines=["[VOL-1/CH-9; id=x]《第九章》: 两人谈妥,各回各家。"])
    assert "大纲已定的金额" not in pack.to_system_prompt(token_budget=16000)


@pytest.mark.asyncio
async def test_writer_user_content_carries_the_outline_amounts(monkeypatch):
    """大纲已定的金额必须贴在生成旁边(user content),不能只埋在系统提示里。

    实测:同一个数字放进约 1 万字的系统提示时模型会无视(第 7 章自造 840、
    第 8 章却按大纲收 820);而拆段/续写之所以有效,就是把约束重新送到了眼前。
    """
    from types import SimpleNamespace
    from app.services import scene_orchestrator as so

    captured = {}

    async def fake_stream(*, task_type, user_content, **kw):
        captured["user"] = user_content
        captured["task"] = task_type
        yield "正文"

    monkeypatch.setattr(so, "stream_text_prompt", fake_stream)
    pack = SimpleNamespace(to_system_prompt=lambda: "sys", character_cards=[],
                           numeric_pin_lines=lambda: "[CH-8] 校服交了,周芸收下八百二")
    scene = SimpleNamespace(idx=1, target_words=600,
                            to_writer_user_content=lambda: "【场景号】第 1 场")
    chunks = []
    async for chunk in so.SceneOrchestrator().write_scene_stream(
            scene=scene, pack=pack, prior_scenes_summary="", db=None,
            project_id="p", chapter_id="c"):
        chunks.append(chunk)
    assert captured["task"] == "scene_writer"
    assert "[CH-8] 校服交了,周芸收下八百二" in captured["user"]
    assert "必须与大纲一致" in captured["user"]


@pytest.mark.asyncio
async def test_writer_without_pins_is_unchanged(monkeypatch):
    """没有可注入的金额时,user content 不许多出这一块。"""
    from types import SimpleNamespace
    from app.services import scene_orchestrator as so

    captured = {}

    async def fake_stream(*, task_type, user_content, **kw):
        captured["user"] = user_content
        yield "正文"

    monkeypatch.setattr(so, "stream_text_prompt", fake_stream)
    pack = SimpleNamespace(to_system_prompt=lambda: "sys", character_cards=[])
    scene = SimpleNamespace(idx=1, target_words=600,
                            to_writer_user_content=lambda: "【场景号】第 1 场")
    async for _ in so.SceneOrchestrator().write_scene_stream(
            scene=scene, pack=pack, prior_scenes_summary="", db=None,
            project_id="p", chapter_id="c"):
        pass
    assert "必须与大纲一致" not in captured["user"]
