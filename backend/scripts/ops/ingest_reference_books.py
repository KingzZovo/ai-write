#!/usr/bin/env python3
"""把本地文本导入为参考书,并跑完蒸馏流水线(离线,不依赖 FastAPI / Celery)。

为什么需要这个脚本:参考书导入在生产里是"前台上传 → Celery 处理",而这台机器上
8000 端口被别的项目占用、Celery worker 也没起。评估/蒸馏不该依赖那套进程模型,
所以这里直接调服务层:写 `reference_books` + `text_chunks`,再走
`reference_ingestor.reprocess_reference_book`(与线上完全同一条流水线),
最后自动生成并绑定 `style_profiles`。

用法::

    python -m scripts.ops.ingest_reference_books \\
      --database-url postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/aiwrite_eval \\
      --file /root/ai-write/data/reference/龙族.txt --title 龙族 --author 江南 \\
      --api-key "$KEY" [--embed-key "$EMBED"] [--max-chars 400000]

产物:标准输出一行 JSON,含 `book_id` / `status` / `slices` / `style_profile_id`;
后面跑长跑时把它传给 `continuity_probe --style-profile <id>` 即可让写作链用这套文笔。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path

_CHAPTER_RE = re.compile(
    r"^\s*(第[零一二三四五六七八九十百千0-9]{1,6}[章回节卷]|Chapter\s+\d+|CHAPTER\s+\d+)\s*(.*)$"
)


def split_chapters(text: str, *, max_chars: int) -> list[dict]:
    """按章标题切分;没有章标题就按 max_chars 硬切。

    返回 ``[{"title": str, "idx": int, "content": str}]``。参考书只需要"可分析的片段",
    不需要与线上 EPUB 解析完全一致 —— 但**必须保留章节边界**,否则蒸馏出的节奏/钩子卡
    会把不同章的事件混成一条。
    """
    lines = text.splitlines()
    chapters: list[dict] = []
    title: str | None = None
    buffer: list[str] = []

    def flush() -> None:
        nonlocal buffer, title
        body = "\n".join(buffer).strip()
        if body:
            chapters.append({"title": title or "", "idx": len(chapters) + 1, "content": body})
        buffer = []

    for line in lines:
        match = _CHAPTER_RE.match(line)
        if match:
            flush()
            title = (match.group(1) + (" " + match.group(2) if match.group(2) else "")).strip()
        else:
            buffer.append(line)
    flush()

    if not chapters:
        body = text.strip()
        chapters = [{"title": "", "idx": i + 1, "content": body[i:i + max_chars]}
                    for i in range(0, len(body), max_chars)] or []

    # 单章过长时切成 max_chars 的片段(保留原标题),避免一次送模超上下文。
    out: list[dict] = []
    for chapter in chapters:
        content = chapter["content"]
        if len(content) <= max_chars:
            out.append(chapter)
            continue
        for offset in range(0, len(content), max_chars):
            out.append({"title": chapter["title"], "idx": chapter["idx"],
                        "content": content[offset:offset + max_chars]})
    return out


async def _heal_endpoint_keys(db, *, flagship_key: str, embed_key: str | None) -> list[str]:
    """把端点密钥统一成"当前进程 SECRET_KEY 可解"的密文。

    为什么需要:`llm_endpoints.api_key` 是用 SECRET_KEY 派生的密钥加密的,而不同脚本
    可能用**不同 SECRET_KEY** 建行(探针用 `continuity-probe-secret`,临时脚本用默认值)。
    一旦解密失败,model_router 会跳过该端点 —— 表现是 `no_embedding_provider`
    或"模型配置整体加载失败",而库里看起来一切正常,极难定位。
    这里对**已知明文**的端点直接重写密文,让整条链自洽。
    """
    from sqlalchemy import select

    from app.models.project import LLMEndpoint
    from app.utils.crypto import decrypt_api_key, encrypt_api_key

    healed: list[str] = []
    for row in (await db.execute(select(LLMEndpoint))).scalars().all():
        plain = embed_key if row.tier == "embedding" else (flagship_key if row.tier == "flagship" else None)
        if not plain:
            continue
        try:
            decrypt_api_key(row.api_key or "")
            continue
        except Exception:  # noqa: BLE001 - 解不开就用已知明文重写
            row.api_key = encrypt_api_key(plain)
            healed.append(f"{row.name}({row.tier})")
    if healed:
        print(json.dumps({"healed_endpoints": healed}, ensure_ascii=False))
    return healed


async def _ensure_llm_endpoint(db, *, endpoint: str, model: str, api_key: str) -> str:
    """返回一个**可用**的 flagship 端点 id;没有就建一条。

    蒸馏流水线要调模型,而 `llm_endpoints` 里此前的 probe 端点可能都被禁用(enabled=0)。
    这里只"补一条启用端点",不改既有行 —— 改别人的配置会让别的跑批行为漂移。
    """
    from sqlalchemy import select

    from app.models.project import LLMEndpoint
    from app.utils.crypto import encrypt_api_key

    existing = (await db.execute(
        select(LLMEndpoint).where(LLMEndpoint.enabled == 1, LLMEndpoint.tier == "flagship")
    )).scalars().first()
    if existing is not None:
        return str(existing.id)
    row = LLMEndpoint(name="reference-ingest", base_url=endpoint,
                      api_key=encrypt_api_key(api_key), default_model=model,
                      tier="flagship", enabled=1)
    db.add(row)
    await db.flush()
    return str(row.id)


async def ingest(*, db_url: str, path: Path, title: str, author: str,
                 api_key: str, model: str, endpoint: str,
                 embed_key: str | None, max_chars: int) -> dict:
    # 必须复用探针那套环境准备:它把 Qdrant/Redis/Neo4j 指到本机隔离栈
    # (6333/6379/7687 默认值在这台机器上是别的项目)。少了这一步,蒸馏会在
    # 写向量时抛 "All connection attempts failed"。
    from scripts.quality_eval.continuity_probe import _prepare_env
    _prepare_env(db_url)
    os.environ["DATABASE_URL"] = db_url
    from sqlalchemy import select

    from app.db.session import Base, async_session_factory, engine
    from app.models.project import ReferenceBook, StyleProfile, TextChunk
    from app.services.reference_ingestor import reprocess_reference_book

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    text = path.read_text(encoding="utf-8", errors="replace")
    chapters = split_chapters(text, max_chars=max_chars)
    if not chapters:
        raise SystemExit(f"文件里没有可导入的正文:{path}")

    async with async_session_factory() as db:
        endpoint_id = await _ensure_llm_endpoint(db, endpoint=endpoint, model=model, api_key=api_key)
        await _heal_endpoint_keys(db, flagship_key=api_key, embed_key=embed_key)
        # 蒸馏链（与写作链一样）通过 `prompt_assets` 的 task_type 路由反查端点：
        # 不种路由就会报 no_embedding_provider / 找不到模型配置。这里复用探针那套
        # 幂等种法（有则更新、无则插入），避免又攒出一堆重复行。
        from scripts.quality_eval.continuity_probe import _seed_prompts
        await _seed_prompts(db, endpoint_id=endpoint_id, model=model,
                            max_tokens=4096, embed_key=embed_key,
                            extra_tasks=("style_abstraction", "beat_extraction", "redaction"))
        book = ReferenceBook(title=title, author=author, source="upload_txt",
                             source_detail=str(path), status="pending")
        db.add(book)
        await db.flush()
        for order, chapter in enumerate(chapters, start=1):
            # text_chunks 的 block_idx / char_count 都是 NOT NULL:
            # block_idx 是"同一章内的片段序号"(这里一段一章,故为 0),
            # char_count 用于覆盖率统计与重切判断。
            db.add(TextChunk(book_id=book.id, sequence_id=order, chapter_idx=chapter["idx"],
                             block_idx=0, chapter_title=chapter["title"],
                             content=chapter["content"], char_count=len(chapter["content"])))
        await db.commit()
        book_id = str(book.id)
        print(json.dumps({"stage": "loaded", "book_id": book_id, "chunks": len(chapters),
                          "chars": sum(len(c["content"]) for c in chapters)}, ensure_ascii=False))

        result = await reprocess_reference_book(book_id=book_id, db=db)
        print(json.dumps({"stage": "ingested", "book_id": book_id,
                          "result": {k: v for k, v in (result or {}).items()
                                     if k in ("status", "total", "style_done", "beat_done",
                                              "missing_style", "missing_beat")}},
                         ensure_ascii=False))

        profile = (await db.execute(
            select(StyleProfile).where(StyleProfile.source_book_id == book.id)
        )).scalars().first()
        summary = {"book_id": book_id, "title": title, "author": author,
                   "status": result.get("status") if isinstance(result, dict) else None,
                   "slices": result.get("total") if isinstance(result, dict) else None,
                   "style_profile_id": str(profile.id) if profile else None,
                   "style_profile_name": profile.name if profile else None,
                   "rules": len(profile.rules_json or []) if profile else 0,
                   "samples": len(profile.sample_passages or []) if profile else 0}
        print(json.dumps(summary, ensure_ascii=False))
        return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--file", required=True, help="本地文本(txt/md,UTF-8)")
    parser.add_argument("--title", default=None)
    parser.add_argument("--author", default="")
    parser.add_argument("--model", default="deepseek-v4-flash")
    parser.add_argument("--endpoint", default="http://127.0.0.1:8317/v1")
    parser.add_argument("--api-key", default=None,
                        help="模型网关 key;缺省读 QUALITY_EVAL_API_KEY")
    parser.add_argument("--embed-key", default=None,
                        help="embedding key;缺省读 QUALITY_EVAL_EMBED_KEY 或本机容器配置")
    parser.add_argument("--max-chars", type=int, default=8000,
                        help="单片段上限(字符)。参考书只需可分析片段,默认 8000")
    args = parser.parse_args(argv)

    path = Path(args.file)
    if not path.is_file():
        print(f"文件不存在:{path}", file=sys.stderr)
        return 2
    api_key = args.api_key or os.environ.get("QUALITY_EVAL_API_KEY", "")
    if not api_key:
        print("缺少 --api-key(或设置 QUALITY_EVAL_API_KEY)", file=sys.stderr)
        return 2
    title = args.title or path.stem

    from scripts.quality_eval.continuity_probe import _detect_embedding_key
    embed_key = args.embed_key or _detect_embedding_key()
    if embed_key:
        os.environ.setdefault("QUALITY_EVAL_EMBED_KEY", embed_key)

    summary = asyncio.run(ingest(db_url=args.database_url, path=path, title=title,
                                 author=args.author, api_key=api_key, model=args.model,
                                 endpoint=args.endpoint, embed_key=embed_key,
                                 max_chars=args.max_chars))
    return 0 if summary.get("style_profile_id") else 1


if __name__ == "__main__":
    raise SystemExit(main())