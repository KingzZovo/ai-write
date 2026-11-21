"""2026-11-21:设定提取 split 模式、图谱驱动懒初始化、设置键名兼容的回归。

用项目自己的流程(`POST /api/pipeline` → 逐章生成)跑真实项目时暴露三个稳定失败:

1. 三个提取 pass **并行**跑,relationships pass 看不到 characters 名单,却被
   `_validate_relationships` 要求"人名必须字面一致" → 实测 3/3 次 502
   「人物关系与角色列表不一致」,连带 world_rules 永远为 0,写作链卡在
   「当前世界规则暂时无法读取」。
2. celery prefork 子进程不跑 FastAPI lifespan,Neo4j 驱动从未初始化 →
   整批章节 failed,而错误消息被 `from None` 抹成一句模糊话。
3. 项目设置里每章字数 / 每卷章数有两个历史键名,只认其中一个会让用户设的值
   被静默忽略(实测把"100 万字、每章 2000 字"算成 2 卷 × 125 章)。
"""
from __future__ import annotations

import asyncio
import json

from app.services import settings_extractor as se
from app.services import world_rule_sources as wrs
from app.services.outline_generator import resolve_chapters_per_volume_pref


# --- 1. relationships pass 必须拿到 characters 名单 ---------------------------

def test_relationships_pass_receives_character_names(monkeypatch):
    seen: dict[str, str] = {}

    async def fake_extract_stream(task_type, system_prompt, outline_text, fields, project_id=None):
        seen[task_type] = system_prompt
        if task_type == "characters_extraction":
            return {"characters": [{"name": "林默"}, {"name": "老周"}]}
        if task_type == "world_rules_extraction":
            return {"world_rules": [{"category": "geography", "rule_text": "城南压着世界树的枯根"}]}
        return {"relationships": [{
            "source_name": "林默", "target_name": "老周", "rel_type": "mentor",
            "label": "师徒", "note": "", "sentiment": "neutral",
        }]}

    monkeypatch.setattr(se, "_extract_stream", fake_extract_stream)
    monkeypatch.setattr(se, "_settings_split_enabled", lambda: True)

    result = asyncio.run(se.extract_settings_from_outline("大纲原文", project_id="p"))

    assert result["relationships"][0]["source_name"] == "林默"
    assert result["world_rules"][0]["category"] == "geography"
    prompt = seen["relationships_extraction"]
    assert "林默" in prompt and "老周" in prompt, "relationships pass 必须收到可用角色名单"
    assert "逐字一致" in prompt


def test_characters_pass_runs_before_relationships(monkeypatch):
    order: list[str] = []

    async def fake_extract_stream(task_type, system_prompt, outline_text, fields, project_id=None):
        order.append(task_type)
        if task_type == "characters_extraction":
            return {"characters": [{"name": "甲"}]}
        if task_type == "world_rules_extraction":
            return {"world_rules": []}
        return {"relationships": []}

    monkeypatch.setattr(se, "_extract_stream", fake_extract_stream)
    monkeypatch.setattr(se, "_settings_split_enabled", lambda: True)

    asyncio.run(se.extract_settings_from_outline("大纲", project_id="p"))

    assert order[0] == "characters_extraction", "characters 必须先出,relationships 才能对齐人名"


def test_failed_characters_branch_is_not_silently_empty(monkeypatch):
    async def fake_extract_stream(task_type, system_prompt, outline_text, fields, project_id=None):
        raise se.SettingsExtractionError("boom")

    monkeypatch.setattr(se, "_extract_stream", fake_extract_stream)
    monkeypatch.setattr(se, "_settings_split_enabled", lambda: True)

    try:
        asyncio.run(se.extract_settings_from_outline("大纲", project_id="p"))
    except se.SettingsExtractionError:
        return
    raise AssertionError("characters 分支失败不得被静默当成空提取")


# --- 2. 图谱驱动懒初始化 ------------------------------------------------------

class _Driver:
    pass


def test_current_rule_driver_initializes_when_missing(monkeypatch):
    calls = {"init": 0}

    async def fake_get_neo4j():
        if calls["init"] == 0:
            raise RuntimeError("Neo4j driver has not been initialized")
        yield _Driver()

    async def fake_init():
        calls["init"] += 1

    import app.db.neo4j as neo4j_mod
    monkeypatch.setattr(neo4j_mod, "get_neo4j", fake_get_neo4j)
    monkeypatch.setattr(neo4j_mod, "init_neo4j", fake_init)

    driver = asyncio.run(wrs.current_rule_driver())

    assert isinstance(driver, _Driver)
    assert calls["init"] == 1


def test_current_rule_driver_does_not_reinit_when_ready(monkeypatch):
    calls = {"init": 0}

    async def fake_get_neo4j():
        yield _Driver()

    async def fake_init():
        calls["init"] += 1

    import app.db.neo4j as neo4j_mod
    monkeypatch.setattr(neo4j_mod, "get_neo4j", fake_get_neo4j)
    monkeypatch.setattr(neo4j_mod, "init_neo4j", fake_init)

    asyncio.run(wrs.current_rule_driver())

    assert calls["init"] == 0


# --- 3. 项目设置键名兼容 ------------------------------------------------------

def test_chapters_per_volume_alias_is_read():
    assert resolve_chapters_per_volume_pref({"chapters_per_volume": 30}) == (30, 30)


def test_explicit_keys_win_over_alias():
    pref = resolve_chapters_per_volume_pref(
        {"chapters_per_volume": 30, "chapters_per_volume_min": 10, "chapters_per_volume_max": 20}
    )
    assert pref == (10, 20)


def test_scale_alias_matches_project_settings_shape():
    """项目 settings_json 里真实的简写键必须能算出预期的规模。"""
    from app.services.outline_generator import compute_scale

    scale = compute_scale(1_000_000, chapter_words=2000,
                          chapters_per_volume_min=100, chapters_per_volume_max=100,
                          chapters_per_volume_target=100)
    assert scale is not None
    assert scale["n_volumes"] == 5
    assert scale["chapters_per_volume"] == 100
    assert json.dumps(scale)  # 可序列化,便于落库/日志
