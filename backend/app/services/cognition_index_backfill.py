"""认知索引回填:换 embedding 模型后,把已写章节的认知条目重建到当前空间。

索引本来是**懒建**的 —— `rank_cognition` 在解析某一章上下文时,顺手把它需要的条目
upsert 进集合。但集合名里带 space 指纹(`cognition_collection(project, space_id, dim)`),
而 `space_identity` 把 provider / model / base_url / modality / encoding / truncate 一起做指纹。
于是**换一次 embedding 模型就等于换一个集合**:

- 新集合是空的;
- `cognition_index_maintenance` 还会把 `space_id != 当前` 的旧点删掉(它只做清理);
- 而对**已经写完的书**,没人会再去生成一次,于是召回长期为空 —— 且不报错(静默)。

本模块补上这个缺失的回填入口,与 `chapter_chunker.backfill_project_chunks` 对齐:
按写作时的同一路径逐章补齐,point id 确定性,重跑覆盖。

用法(orchestrator runbook)::

    await backfill_project_cognition(project_id)

单章失败只记日志并继续;返回值给出统计。
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def _project_chapters(sessions, project_id: str) -> list[Any]:
    """按全书顺序取章节(需要 outline/content 作为查询上下文)。"""
    from sqlalchemy import select

    from app.models.project import Chapter, Volume

    async with sessions() as db:
        rows = await db.execute(
            select(Chapter.id, Chapter.chapter_idx, Chapter.global_idx,
                   Chapter.outline_json, Chapter.content_text)
            .join(Volume, Chapter.volume_id == Volume.id)
            .where(Volume.project_id == project_id)
            .order_by(Chapter.global_idx.asc().nulls_last(), Chapter.chapter_idx.asc())
        )
        return list(rows.all())


async def backfill_project_cognition(project_id: Any, *, sessions=None, space_loader=None,
                                     client=None) -> dict[str, Any]:
    """把每一章"写作时会索引的认知条目"补写进当前 embedding 空间。

    做法与写作时完全一致:`load_candidate_context(before_chapter_id=本章)` 得到
    该章该看到的认知上下文,再调 `rank_cognition` —— 它内部会
    `ensure_collection` + `prepare_index`(只补缺失点)+ 查询。

    第一本书的第一章没有"之前的认知",所以从第 2 章开始。
    """
    from app.db.session import async_session_factory
    from app.services.character_cognition import load_prompt_context
    from app.services.cognition_retrieval import current_embedding_space, rank_cognition

    pid = str(project_id)
    sessions = sessions or async_session_factory
    space_loader = space_loader or current_embedding_space
    owns_client = client is None
    space = None
    result: dict[str, Any] = {"status": "ok", "project_id": pid,
                              "chapters_indexed": 0, "chapters_skipped": 0,
                              "chapters_failed": 0, "failures": []}
    try:
        if client is None:
            from app.config import settings
            from qdrant_client import AsyncQdrantClient

            client = AsyncQdrantClient(host=settings.QDRANT_HOST, port=settings.QDRANT_PORT,
                                       timeout=30, check_compatibility=False)
        space = await space_loader()
        chapters = await _project_chapters(sessions, pid)
        if not chapters:
            result["status"] = "empty"
            return result
        for row in chapters[1:]:
            try:
                # 与写作时同一路径:`load_prompt_context(..., defer_retrieval=True)`
                # 拿到 CognitionPrompt(它自带 before_chapter_id),再交给 rank_cognition
                # 去 ensure_collection + prepare_index + 查询。
                # 注意:不能在 DB 会话里做嵌入,所以 prompt 构造完就关会话。
                async with sessions() as db:
                    prompt = await load_prompt_context(
                        db, pid, before_chapter_id=row.id,
                        chapter_outline=row.outline_json or {},
                        chapter_text=row.content_text or "",
                        defer_retrieval=True,
                    )
                if getattr(prompt, "before_chapter_id", None) is None:
                    # 第一章之前没有认知材料,索引没有意义。
                    result["chapters_skipped"] += 1
                    continue
                await rank_cognition(prompt, client=client, space=space)
                result["chapters_indexed"] += 1
            except Exception as exc:  # noqa: BLE001
                result["chapters_failed"] += 1
                result["failures"].append({"chapter_idx": row.chapter_idx,
                                           "error": f"{type(exc).__name__}: {exc}"[:200]})
                logger.warning("cognition backfill chapter %s failed: %s",
                               row.chapter_idx, type(exc).__name__)
        result["space_id"] = space.identity
        if result["chapters_failed"]:
            result["status"] = "partial"
        return result
    finally:
        if owns_client and client is not None:
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                pass
        if space is not None and hasattr(space, "aclose"):
            try:
                await space.aclose()
            except Exception:  # noqa: BLE001
                pass