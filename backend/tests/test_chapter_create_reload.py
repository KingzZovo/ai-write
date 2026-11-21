"""建章接口的加载契约(2026-11-21)。

实测缺陷:走正常用户流程(建项目 → 卷纲 → 建章节清单)时,`POST
/api/projects/{pid}/chapters` 稳定返回 **503 chapter_order_unconfirmed**,
日志只有一句 `Chapter structure save not confirmed (MissingGreenlet)`。

根因:`_create_chapter` 在 `reindex_chapter_structure` 之后只做了一次
`await db.refresh(chapter)`,而实测它只回填了 5 个键
(chapter_idx/id/outline_json/updated_at/volume_id);随后构造响应 payload 时访问
`chapter.title` 触发**同步 IO** → SQLAlchemy `MissingGreenlet` → `structure_write`
兜底成 503。没有章节清单,后面"写正文"整条路都走不通。

这组用例盯住"构造响应前必须把整行加载齐"。
"""

from __future__ import annotations

import inspect

from app.api import chapters


def test_create_chapter_reloads_row_before_building_payload():
    src = inspect.getsource(chapters._create_chapter)
    assert "populate_existing" in src or "attribute_names" in src, (
        "建章后必须显式重读整行(populate_existing / attribute_names):"
        "只在 reindex 后 refresh 会留下未加载列,构造响应时触发 MissingGreenlet → 503"
    )


def test_create_chapter_payload_stays_after_reload():
    """顺序也不能变:重读必须在构造 payload 之前。"""
    src = inspect.getsource(chapters._create_chapter)
    reload_at = src.find("populate_existing")
    if reload_at == -1:
        reload_at = src.find("attribute_names")
    payload_at = src.find("_chapter_response_payload")
    assert reload_at != -1 and payload_at != -1
    assert reload_at < payload_at, "重读整行必须发生在构造响应之前"


def test_structure_write_still_maps_unexpected_errors_to_503():
    """兜底语义保留:非冲突类异常仍然是 503(只是不该由 Loading 类错误触发)。"""
    from app.api import chapter_order

    src = inspect.getsource(chapter_order.structure_write)
    assert "chapter_order_unconfirmed" in src
