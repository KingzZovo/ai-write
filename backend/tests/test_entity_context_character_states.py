"""回归:有角色的项目必须能读到人物状态,不能因为一个未导入的名字整章无法生成。

实测(2026-11-14,连续章探针):HEAD 提交 854f9b9「character location tracking +
entity timeline context」在 ``entity_context.verified_character_states`` 里使用了
``UUID(...)``,但模块没有导入 ``UUID``。异常被 ``ContextPackBuilder`` 归类成
``character_states_context_unavailable``(必要上下文缺失),于是**只要项目里有角色卡,
章节生成就会被阻断**——不是降级,是整章写不出来。

真实复现:隔离 PG 上建项目/卷/十章大纲并塞入三张角色卡,调
``SceneOrchestrator.orchestrate_chapter_stream`` 直接得到
``RequiredContextUnavailable: character_states_context_unavailable``,
底层 traceback 为 ``NameError: name 'UUID' is not defined``。

这里用一个只记录查询的假会话固定住"构造查询阶段不再抛 NameError"这一条。
"""
from __future__ import annotations

import pytest

from app.services.entity_context import verified_character_states


class _FakeResult:
    def all(self):
        return []


class _FakeDb:
    """只够让查询构造与执行走到 ``.all()``;不连任何数据库。"""

    def __init__(self):
        self.queries: list[object] = []

    async def execute(self, query):
        self.queries.append(query)
        return _FakeResult()


@pytest.mark.asyncio
async def test_verified_character_states_builds_query_without_name_error():
    db = _FakeDb()
    states = await verified_character_states(
        db, "00000000-0000-0000-0000-000000000001", 3, ["周芸"]
    )
    assert states == {}
    assert db.queries, "应当至少执行一次查询"


@pytest.mark.asyncio
async def test_non_positive_chapter_index_short_circuits_without_query():
    db = _FakeDb()
    assert await verified_character_states(db, "00000000-0000-0000-0000-000000000001", 0) == {}
    assert db.queries == []


def test_module_imports_uuid():
    """直接固定缺失的导入,避免同类回归再次滑过测试。"""
    import app.services.entity_context as module

    assert hasattr(module, "UUID")
