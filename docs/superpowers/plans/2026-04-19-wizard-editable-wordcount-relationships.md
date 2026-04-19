# 向导可编辑 + 字数目标 + 单卷重生 + 关系表 + 7 bug 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让向导每一步可跳可改；字数目标全书 + 单章化；侧栏支持单卷重生；角色关系表成为 first-class 数据；顺手修掉设定集崩溃、偏好刷新丢失、空壳卷、卷数识别漏前传、加载慢等 7 个 bug。

**Architecture:** 后端先加 `relationships` 表 + `chapters.target_words` 列，然后堆 API；前端先修崩溃类 bug 再做向导/字数/重生等 UI 改造。分 15 个独立 commit。

**Tech Stack:** FastAPI + SQLAlchemy + Alembic（后端）；Next.js 16 + React 19 + zustand + localStorage（前端）；pytest（后端）。

---

## Task 1: Alembic 迁移

**Files:**
- Create: `backend/alembic/versions/2026_04_19_relationships_and_chapter_target_words.py`

- [ ] **Step 1: 生成骨架**

Run:
```bash
docker compose -f /root/ai-write/docker-compose.yml exec -T backend alembic revision -m "add relationships table and chapters.target_words"
```
输出形如 `Generating .../<rev>_add_relationships_table_and_chapters_target_words.py`

- [ ] **Step 2: 填写迁移**

替换生成文件内的 `upgrade()` / `downgrade()`:

```python
def upgrade() -> None:
    op.create_table(
        "relationships",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("characters.id", ondelete="CASCADE"), nullable=False),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("characters.id", ondelete="CASCADE"), nullable=False),
        sa.Column("rel_type", sa.String(50), nullable=False),
        sa.Column("label", sa.String(200), nullable=False, server_default=""),
        sa.Column("note", sa.Text, nullable=False, server_default=""),
        sa.Column("sentiment", sa.String(20), nullable=False, server_default="neutral"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_relationships_project_id", "relationships", ["project_id"])
    op.create_index("ix_relationships_source_id", "relationships", ["source_id"])
    op.create_index("ix_relationships_target_id", "relationships", ["target_id"])

    op.add_column("chapters", sa.Column("target_words", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("chapters", "target_words")
    op.drop_index("ix_relationships_target_id", table_name="relationships")
    op.drop_index("ix_relationships_source_id", table_name="relationships")
    op.drop_index("ix_relationships_project_id", table_name="relationships")
    op.drop_table("relationships")
```

确保文件顶部已有 `from sqlalchemy.dialects import postgresql`；若无则补。

- [ ] **Step 3: 应用迁移**

Run:
```bash
docker compose -f /root/ai-write/docker-compose.yml exec -T backend alembic upgrade head
```
Expected: `Running upgrade <prev> -> <new>, add relationships table and chapters.target_words`

- [ ] **Step 4: 验证**

Run:
```bash
docker exec ai-write-postgres-1 psql -U postgres -d aiwrite -c "\d relationships" | head -20
docker exec ai-write-postgres-1 psql -U postgres -d aiwrite -c "\d chapters" | grep target_words
```
Expected: 表结构齐全、`target_words | integer`

- [ ] **Step 5: commit**

```bash
git -C /root/ai-write add backend/alembic/versions/
git -C /root/ai-write commit -m "feat(db): add relationships table and chapters.target_words"
```

---

## Task 2: Relationship 模型 + schema

**Files:**
- Modify: `backend/app/models/project.py`
- Modify: `backend/app/schemas/project.py`

- [ ] **Step 1: 加 Relationship 模型**

在 `backend/app/models/project.py` 最后（`FilterWord` 之前或之后均可）加：

```python
class Relationship(Base):
    __tablename__ = "relationships"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id = Column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_id = Column(
        UUID(as_uuid=True),
        ForeignKey("characters.id", ondelete="CASCADE"),
        nullable=False,
    )
    target_id = Column(
        UUID(as_uuid=True),
        ForeignKey("characters.id", ondelete="CASCADE"),
        nullable=False,
    )
    rel_type = Column(String(50), nullable=False)
    label = Column(String(200), default="")
    note = Column(Text, default="")
    sentiment = Column(String(20), default="neutral")
    created_at = Column(DateTime(timezone=True), default=_utcnow)
```

- [ ] **Step 2: 加 target_words 到 Chapter 模型**

`Chapter` 类（约 line 78）`updated_at` 下面加：
```python
    target_words = Column(Integer, nullable=True)
```

- [ ] **Step 3: 加 Pydantic schemas**

`backend/app/schemas/project.py` 末尾（或紧跟 `ForeshadowResponse` 附近）追加：

```python
class RelationshipCreate(BaseModel):
    source_id: UUID
    target_id: UUID
    rel_type: str = Field(..., max_length=50)
    label: str = Field(default="", max_length=200)
    note: str = ""
    sentiment: str = Field(default="neutral", max_length=20)


class RelationshipUpdate(BaseModel):
    rel_type: str | None = Field(None, max_length=50)
    label: str | None = Field(None, max_length=200)
    note: str | None = None
    sentiment: str | None = Field(None, max_length=20)


class RelationshipResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    source_id: UUID
    target_id: UUID
    rel_type: str
    label: str
    note: str
    sentiment: str
    created_at: datetime


class RelationshipListResponse(BaseModel):
    relationships: list[RelationshipResponse]
    total: int


class RelationshipBulkRequest(BaseModel):
    items: list[RelationshipCreate]
```

注意 `datetime` 和 `UUID` 在文件顶部应已 imported；若无则补。

- [ ] **Step 4: 同时更新 ChapterResponse 暴露 target_words**

`ChapterResponse`（约 line 97）加字段：
```python
    target_words: int | None = None
```

- [ ] **Step 5: commit**

```bash
git -C /root/ai-write add backend/app/models/project.py backend/app/schemas/project.py
git -C /root/ai-write commit -m "feat(models): Relationship model + chapters.target_words schema"
```

---

## Task 3: Relationships CRUD 端点

**Files:**
- Modify: `backend/app/api/settings.py`
- Test: `backend/tests/test_api_core.py`

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_api_core.py` 末尾追加：

```python
@pytest.mark.asyncio
async def test_relationships_crud(auth_client):
    # Create project
    resp = await auth_client.post("/api/projects", json={"title": "关系测试", "genre": "测试"})
    pid = resp.json()["id"]
    # Create two characters
    c1 = (await auth_client.post(f"/api/projects/{pid}/characters", json={"name": "甲"})).json()
    c2 = (await auth_client.post(f"/api/projects/{pid}/characters", json={"name": "乙"})).json()

    # Create relationship
    resp = await auth_client.post(
        f"/api/projects/{pid}/relationships",
        json={
            "source_id": c1["id"],
            "target_id": c2["id"],
            "rel_type": "rival",
            "label": "宿敌",
            "sentiment": "negative",
        },
    )
    assert resp.status_code == 201
    rid = resp.json()["id"]

    # List
    resp = await auth_client.get(f"/api/projects/{pid}/relationships")
    assert resp.status_code == 200
    assert resp.json()["total"] == 1
    assert resp.json()["relationships"][0]["label"] == "宿敌"

    # Update
    resp = await auth_client.put(
        f"/api/projects/{pid}/relationships/{rid}",
        json={"label": "死敌"},
    )
    assert resp.status_code == 200
    assert resp.json()["label"] == "死敌"

    # Bulk
    resp = await auth_client.post(
        f"/api/projects/{pid}/relationships/bulk",
        json={"items": [{
            "source_id": c2["id"],
            "target_id": c1["id"],
            "rel_type": "rival",
            "label": "反向",
            "sentiment": "negative",
        }]},
    )
    assert resp.status_code == 201
    assert resp.json()["created"] == 1

    # Delete
    resp = await auth_client.delete(f"/api/projects/{pid}/relationships/{rid}")
    assert resp.status_code == 204
    resp = await auth_client.get(f"/api/projects/{pid}/relationships")
    assert resp.json()["total"] == 1

    # Cleanup project
    await auth_client.delete(f"/api/projects/{pid}?purge=true")
```

- [ ] **Step 2: 运行确认失败**

Run:
```bash
docker compose -f /root/ai-write/docker-compose.yml exec -T backend python -m pytest tests/test_api_core.py::test_relationships_crud -v
```
Expected: FAIL (404 on POST relationships)

- [ ] **Step 3: 加端点实现**

在 `backend/app/api/settings.py` 末尾追加：

```python
# =========================================================================
# Relationship schemas (imported from schemas/project.py)
# =========================================================================

from app.schemas.project import (
    RelationshipCreate,
    RelationshipUpdate,
    RelationshipResponse,
    RelationshipListResponse,
    RelationshipBulkRequest,
)
from app.models.project import Relationship


@router.get("/relationships", response_model=RelationshipListResponse)
async def list_relationships(
    project_id: str,
    db: AsyncSession = Depends(get_db),
) -> RelationshipListResponse:
    result = await db.execute(
        select(Relationship).where(Relationship.project_id == project_id)
    )
    items = list(result.scalars().all())
    return RelationshipListResponse(
        relationships=[RelationshipResponse.model_validate(r) for r in items],
        total=len(items),
    )


@router.post("/relationships", response_model=RelationshipResponse, status_code=201)
async def create_relationship(
    project_id: str,
    body: RelationshipCreate,
    db: AsyncSession = Depends(get_db),
) -> RelationshipResponse:
    rel = Relationship(
        project_id=project_id,
        source_id=body.source_id,
        target_id=body.target_id,
        rel_type=body.rel_type,
        label=body.label,
        note=body.note,
        sentiment=body.sentiment,
    )
    db.add(rel)
    await db.flush()
    await db.refresh(rel)
    return RelationshipResponse.model_validate(rel)


class RelationshipBulkResponse(BaseModel):
    created: int


@router.post("/relationships/bulk", response_model=RelationshipBulkResponse, status_code=201)
async def bulk_create_relationships(
    project_id: str,
    body: RelationshipBulkRequest,
    db: AsyncSession = Depends(get_db),
) -> RelationshipBulkResponse:
    created = 0
    for item in body.items:
        rel = Relationship(
            project_id=project_id,
            source_id=item.source_id,
            target_id=item.target_id,
            rel_type=item.rel_type,
            label=item.label,
            note=item.note,
            sentiment=item.sentiment,
        )
        db.add(rel)
        created += 1
    await db.flush()
    return RelationshipBulkResponse(created=created)


@router.put("/relationships/{relationship_id}", response_model=RelationshipResponse)
async def update_relationship(
    project_id: str,
    relationship_id: str,
    body: RelationshipUpdate,
    db: AsyncSession = Depends(get_db),
) -> RelationshipResponse:
    rel = await db.get(Relationship, relationship_id)
    if rel is None or str(rel.project_id) != project_id:
        raise HTTPException(status_code=404, detail="Relationship not found")
    data = body.model_dump(exclude_unset=True)
    for k, v in data.items():
        setattr(rel, k, v)
    await db.flush()
    await db.refresh(rel)
    return RelationshipResponse.model_validate(rel)


@router.delete("/relationships/{relationship_id}", status_code=204)
async def delete_relationship(
    project_id: str,
    relationship_id: str,
    db: AsyncSession = Depends(get_db),
) -> None:
    rel = await db.get(Relationship, relationship_id)
    if rel is None or str(rel.project_id) != project_id:
        raise HTTPException(status_code=404, detail="Relationship not found")
    await db.delete(rel)
    await db.flush()
```

- [ ] **Step 4: 重启 backend + 运行测试**

Run:
```bash
docker compose -f /root/ai-write/docker-compose.yml restart backend
sleep 3
docker compose -f /root/ai-write/docker-compose.yml exec -T backend python -m pytest tests/test_api_core.py::test_relationships_crud -v
```
Expected: PASS

- [ ] **Step 5: commit**

```bash
git -C /root/ai-write add backend/app/api/settings.py backend/tests/test_api_core.py
git -C /root/ai-write commit -m "feat(api): relationships CRUD + bulk + tests"
```

---

## Task 4: Extractor v2（relationships 提取）

**Files:**
- Modify: `backend/app/services/settings_extractor.py`
- Modify: `backend/app/api/outlines.py`

- [ ] **Step 1: 扩展 extractor system prompt**

修改 `backend/app/services/settings_extractor.py` 的 `SETTINGS_EXTRACTOR_SYSTEM` — 把 top-level schema 里增加 `relationships`:

```
JSON 结构：
{
  "characters": [...已有定义...],
  "world_rules": [...已有定义...],
  "relationships": [
    {
      "source_name": "源角色名（必须出现在 characters 中）",
      "target_name": "目标角色名",
      "rel_type": "ally|enemy|mentor|lover|rival|family|other",
      "label": "关系描述短语，如 宿敌 / 生死之交",
      "note": "可选长描述（1-2 句背景、演变、代价）",
      "sentiment": "positive|negative|neutral"
    }
  ]
}
```

在规则部分追加：
```
- relationships 必须覆盖大纲里所有明显的人物关系；单条关系只填一次，不要 A→B 和 B→A 重复同内容（除非关系是单向的，比如单恋）
- relationships 的 source_name/target_name 必须和 characters[].name 字面一致
```

- [ ] **Step 2: 返回值也带 relationships**

修改 `extract_settings_from_outline` 的末尾：

```python
    chars = data.get("characters") if isinstance(data, dict) else None
    rules = data.get("world_rules") if isinstance(data, dict) else None
    rels = data.get("relationships") if isinstance(data, dict) else None
    return {
        "characters": chars if isinstance(chars, list) else [],
        "world_rules": rules if isinstance(rules, list) else [],
        "relationships": rels if isinstance(rels, list) else [],
    }
```

- [ ] **Step 3: 在 extract-settings 端点里消费 relationships**

修改 `backend/app/api/outlines.py` 的 `extract_settings` 端点：在创建完 characters 和 world_rules 之后追加一段（`await db.flush()` 之前），把 relationships 写入：

```python
    # Map name -> character_id (includes both existing and just-created)
    await db.flush()
    name_to_id_result = await db.execute(
        select(Character.id, Character.name).where(Character.project_id == project_id)
    )
    name_to_id = {name: cid for cid, name in name_to_id_result.all()}

    rels_created = 0
    for r in extracted.get("relationships", []):
        if not isinstance(r, dict):
            continue
        src = name_to_id.get((r.get("source_name") or "").strip())
        tgt = name_to_id.get((r.get("target_name") or "").strip())
        if not src or not tgt or src == tgt:
            continue
        rel_type = (r.get("rel_type") or "other").strip()
        label = (r.get("label") or "").strip()
        note = (r.get("note") or "").strip()
        sentiment = (r.get("sentiment") or "neutral").strip()
        # Dedup: skip if a relationship with same (source, target, rel_type, label) already exists
        dup = await db.execute(
            select(Relationship.id).where(
                Relationship.project_id == project_id,
                Relationship.source_id == src,
                Relationship.target_id == tgt,
                Relationship.rel_type == rel_type,
                Relationship.label == label,
            )
        )
        if dup.scalar_one_or_none():
            continue
        db.add(Relationship(
            project_id=project_id,
            source_id=src,
            target_id=tgt,
            rel_type=rel_type,
            label=label,
            note=note,
            sentiment=sentiment,
        ))
        rels_created += 1
```

同时修改 `ExtractResponse`:

```python
class ExtractResponse(BaseModel):
    characters_created: int
    world_rules_created: int
    relationships_created: int
```

函数返回语句：
```python
    return ExtractResponse(
        characters_created=chars_created,
        world_rules_created=rules_created,
        relationships_created=rels_created,
    )
```

顶部 import 补：
```python
from app.models.project import Character, Outline, WorldRule, Relationship
```

- [ ] **Step 4: 冒烟**

Run:
```bash
docker compose -f /root/ai-write/docker-compose.yml restart backend
sleep 3
T=$(curl -s -X POST http://127.0.0.1:8000/api/auth/login -H 'Content-Type: application/json' -d '{"username":"king","password":"Wt991125"}' | python3 -c "import sys,json;print(json.load(sys.stdin)['token'])")
PID=6e331209-056b-4b2b-9798-ac246ee8dd48
OID=6753e9d0-a4bf-4aa2-8d36-eaea5520e55d
# 清掉 测试 项目已有的 relationships 让重新提取
docker exec ai-write-postgres-1 psql -U postgres -d aiwrite -c "DELETE FROM relationships WHERE project_id='$PID';"
curl -s -X POST -H "Authorization: Bearer $T" "http://127.0.0.1:8000/api/projects/$PID/outlines/$OID/extract-settings" | python3 -m json.tool
```
Expected: `{"characters_created": 0, "world_rules_created": 0, "relationships_created": N}`（N>=3；characters 和 rules 此前已提取过，去重后为 0）

- [ ] **Step 5: commit**

```bash
git -C /root/ai-write add backend/app/services/settings_extractor.py backend/app/api/outlines.py
git -C /root/ai-write commit -m "feat(extractor): extract character relationships to new table"
```

---

## Task 5: 轻量 chapters 列表

**Files:**
- Modify: `backend/app/api/chapters.py`

- [ ] **Step 1: 加 lightweight 参数**

修改 `list_chapters` 端点（约 line 49）:

```python
@router.get("")
async def list_chapters(
    project_id: str,
    volume_id: str | None = None,
    lightweight: bool = False,
    db: AsyncSession = Depends(get_db),
) -> list[dict] | list[ChapterResponse]:
    """List chapters. lightweight=true omits content_text for fast loading."""
    if volume_id:
        query = select(Chapter).where(Chapter.volume_id == volume_id).order_by(Chapter.chapter_idx)
    else:
        vol_query = select(Volume.id).where(Volume.project_id == project_id)
        vol_result = await db.execute(vol_query)
        volume_ids = [str(v) for v in vol_result.scalars().all()]
        if not volume_ids:
            return []
        query = select(Chapter).where(Chapter.volume_id.in_(volume_ids)).order_by(Chapter.chapter_idx)

    result = await db.execute(query)
    chapters = result.scalars().all()
    if lightweight:
        return [
            {
                "id": str(c.id),
                "volume_id": str(c.volume_id),
                "title": c.title,
                "chapter_idx": c.chapter_idx,
                "word_count": c.word_count,
                "status": c.status,
                "target_words": c.target_words,
            }
            for c in chapters
        ]
    return [ChapterResponse.model_validate(c) for c in chapters]
```

注：返回类型提升为 `list[dict] | list[ChapterResponse]`；FastAPI 会当 dict 序列直接返回。前端 normalize 继续工作（无 content_text 视为空字符串即可）。

- [ ] **Step 2: 冒烟**

Run:
```bash
docker compose -f /root/ai-write/docker-compose.yml restart backend; sleep 3
T=$(curl -s -X POST http://127.0.0.1:8000/api/auth/login -H 'Content-Type: application/json' -d '{"username":"king","password":"Wt991125"}' | python3 -c "import sys,json;print(json.load(sys.stdin)['token'])")
PID=6e331209-056b-4b2b-9798-ac246ee8dd48
echo "--- lightweight ---"
time curl -s -H "Authorization: Bearer $T" "http://127.0.0.1:8000/api/projects/$PID/chapters?lightweight=true" | wc -c
echo "--- full ---"
time curl -s -H "Authorization: Bearer $T" "http://127.0.0.1:8000/api/projects/$PID/chapters" | wc -c
```
Expected: lightweight 返回字节数明显少于 full；两者都 200。

- [ ] **Step 3: commit**

```bash
git -C /root/ai-write add backend/app/api/chapters.py
git -C /root/ai-write commit -m "perf(api): add lightweight chapters list (no content_text)"
```

---

## Task 6: 单卷重生成端点（SSE）

**Files:**
- Modify: `backend/app/api/volumes.py`

- [ ] **Step 1: 加 regenerate 端点**

在 `backend/app/api/volumes.py` 末尾追加：

```python
import json
from collections.abc import AsyncGenerator
from fastapi.responses import StreamingResponse
from app.db.session import async_session_factory
from app.models.project import Chapter, Outline
from app.services.outline_generator import OutlineGenerator


SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


@router.post("/{volume_id}/regenerate")
async def regenerate_volume(
    project_id: str,
    volume_id: str,
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """Delete existing chapters + volume outline and regenerate via SSE."""
    volume = await db.get(Volume, volume_id)
    if not volume or str(volume.project_id) != project_id:
        raise HTTPException(status_code=404, detail="Volume not found")

    # Find book outline
    book_result = await db.execute(
        select(Outline).where(
            Outline.project_id == project_id,
            Outline.level == "book",
            Outline.is_confirmed == 1,
        ).order_by(Outline.created_at.asc())
    )
    book_outline = book_result.scalar_one_or_none()
    if not book_outline:
        raise HTTPException(status_code=400, detail="No confirmed book outline found")
    book_outline_data = book_outline.content_json or {}

    # Delete existing chapters under this volume
    ch_result = await db.execute(select(Chapter).where(Chapter.volume_id == volume_id))
    for ch in ch_result.scalars().all():
        await db.delete(ch)

    # Delete existing volume outlines for this volume_idx
    ol_result = await db.execute(
        select(Outline).where(
            Outline.project_id == project_id,
            Outline.level == "volume",
            Outline.parent_id == book_outline.id,
        )
    )
    for ol in ol_result.scalars().all():
        cj = ol.content_json or {}
        if isinstance(cj, dict) and cj.get("volume_idx") == volume.volume_idx:
            await db.delete(ol)

    await db.flush()

    volume_idx = volume.volume_idx

    async def event_stream() -> AsyncGenerator[str, None]:
        try:
            yield f"data: {json.dumps({'status': 'generating', 'volume_idx': volume_idx})}\n\n"
            collected: list[str] = []
            generator = OutlineGenerator()
            async for chunk in await generator.generate_volume_outline(
                book_outline=book_outline_data,
                volume_idx=volume_idx,
                user_notes="",
                stream=True,
            ):
                collected.append(chunk)
                yield f"data: {json.dumps({'text': chunk})}\n\n"

            full = "".join(collected).strip()
            if not full:
                yield f"data: {json.dumps({'error': 'LLM returned empty'})}\n\n"
                return

            # Parse JSON
            cleaned = full
            if cleaned.startswith("```"):
                lines = cleaned.split("\n")
                lines = lines[1:]
                if lines and lines[-1].strip() == "```":
                    lines = lines[:-1]
                cleaned = "\n".join(lines).strip()
            try:
                parsed = json.loads(cleaned)
            except json.JSONDecodeError:
                parsed = {"raw_text": full}
            if not isinstance(parsed, dict):
                parsed = {"raw_text": full}

            # Persist
            async with async_session_factory() as save_db:
                # Save outline
                new_ol = Outline(
                    project_id=project_id,
                    level="volume",
                    parent_id=book_outline.id,
                    content_json=parsed,
                )
                save_db.add(new_ol)

                # Update volume title/summary
                vol = await save_db.get(Volume, volume_id)
                if isinstance(parsed.get("title"), str) and parsed["title"].strip():
                    vol.title = parsed["title"].strip()
                summary = parsed.get("core_conflict") or parsed.get("emotional_arc")
                if isinstance(summary, str):
                    vol.summary = summary

                # Create chapters from chapter_summaries
                chs = parsed.get("chapter_summaries") if isinstance(parsed, dict) else None
                chapters_created = 0
                if isinstance(chs, list):
                    for i, cs in enumerate(chs):
                        if not isinstance(cs, dict):
                            continue
                        chapter_idx = cs.get("chapter_idx") if isinstance(cs.get("chapter_idx"), int) else i + 1
                        title = cs.get("title") if isinstance(cs.get("title"), str) and cs["title"].strip() else f"第{chapter_idx}章"
                        save_db.add(Chapter(
                            volume_id=volume_id,
                            title=title,
                            chapter_idx=chapter_idx,
                            outline_json=cs,
                        ))
                        chapters_created += 1
                await save_db.commit()

            yield f"data: {json.dumps({'status': 'done', 'chapters_created': chapters_created})}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream", headers=SSE_HEADERS)
```

- [ ] **Step 2: 冒烟**

暂时先不做功能测试（要 LLM 调用），只确认路由注册：

Run:
```bash
docker compose -f /root/ai-write/docker-compose.yml restart backend; sleep 3
T=$(curl -s -X POST http://127.0.0.1:8000/api/auth/login -H 'Content-Type: application/json' -d '{"username":"king","password":"Wt991125"}' | python3 -c "import sys,json;print(json.load(sys.stdin)['token'])")
curl -s -o /dev/null -w "%{http_code}\n" -X POST -H "Authorization: Bearer $T" "http://127.0.0.1:8000/api/projects/00000000-0000-0000-0000-000000000000/volumes/00000000-0000-0000-0000-000000000000/regenerate"
```
Expected: 404 (Volume not found) — 证明路由注册成功

- [ ] **Step 3: commit**

```bash
git -C /root/ai-write add backend/app/api/volumes.py
git -C /root/ai-write commit -m "feat(api): POST /volumes/{id}/regenerate — SSE single-volume outline regen"
```

---

## Task 7: 前端 bug E1 — SettingsPanel 拆包

**Files:**
- Modify: `frontend/src/components/panels/SettingsPanel.tsx`

- [ ] **Step 1: 修复两处拆包**

把两处：
```typescript
const data = await apiFetch<Character[]>(`/api/projects/${projectId}/characters`)
setCharacters(data)
```
```typescript
const data = await apiFetch<WorldRule[]>(`/api/projects/${projectId}/world-rules`)
setWorldRules(data)
```

改成：
```typescript
const data = await apiFetch<{ characters: Character[]; total: number }>(`/api/projects/${projectId}/characters`)
setCharacters(data.characters)
```
```typescript
const data = await apiFetch<{ world_rules: WorldRule[]; total: number }>(`/api/projects/${projectId}/world-rules`)
setWorldRules(data.world_rules)
```

- [ ] **Step 2: typecheck**

Run:
```bash
cd /root/ai-write/frontend && npx tsc --noEmit 2>&1 | head
```
Expected: 无错

- [ ] **Step 3: commit**

```bash
git -C /root/ai-write add frontend/src/components/panels/SettingsPanel.tsx
git -C /root/ai-write commit -m "fix(settings-panel): unwrap {characters,total} / {world_rules,total} envelopes"
```

---

## Task 8: 前端 bug E2 — RelationshipGraph 使用新 API

**Files:**
- Modify: `frontend/src/components/panels/RelationshipGraph.tsx`

- [ ] **Step 1: 改为新 API 结构**

替换整个 `fetchData` 函数和相关 state：

```typescript
interface Character {
  id: string
  name: string
  profile_json?: Record<string, unknown>
}

interface Relationship {
  id: string
  source_id: string
  target_id: string
  rel_type: string
  label: string
  note: string
  sentiment: string
}

// ... 删除原 CharacterData interface

// 在 component:
useEffect(() => {
  if (!projectId) return
  setLoading(true)
  ;(async () => {
    try {
      const charsRes = await apiFetch<{ characters: Character[]; total: number }>(
        `/api/projects/${projectId}/characters`
      )
      setCharacters(charsRes.characters)
      const relsRes = await apiFetch<{ relationships: Relationship[]; total: number }>(
        `/api/projects/${projectId}/relationships`
      )
      setRelationships(relsRes.relationships)
    } catch {
      setCharacters([])
      setRelationships([])
    } finally {
      setLoading(false)
    }
  })()
}, [projectId])
```

然后把渲染区的 `rel.sourceId` / `rel.targetId` 改为 `rel.source_id` / `rel.target_id`（两处）。

加 sentiment 颜色：在渲染 `<line>` 的 stroke 值前：
```typescript
const sentimentColor = rel.sentiment === 'positive' ? '#10B981' : rel.sentiment === 'negative' ? '#EF4444' : '#9CA3AF'
```
并将原 `stroke={isHighlighted ? '#3B82F6' : '#D1D5DB'}` 改为 `stroke={isHighlighted ? '#3B82F6' : sentimentColor}`

- [ ] **Step 2: typecheck**

Run:
```bash
cd /root/ai-write/frontend && npx tsc --noEmit 2>&1 | head
```
Expected: 无错

- [ ] **Step 3: commit**

```bash
git -C /root/ai-write add frontend/src/components/panels/RelationshipGraph.tsx
git -C /root/ai-write commit -m "fix(relationship-graph): use new /relationships endpoint + sentiment colors"
```

---

## Task 9: 前端 bug E3 — WritingGuidePanel 持久化

**Files:**
- Modify: `frontend/src/components/panels/WritingGuidePanel.tsx`

- [ ] **Step 1: 加 localStorage 读写**

替换组件开头 state 初始化：

```typescript
const LS_KEY = 'writing-guide-prefs:v1'

interface WGPrefs {
  activeModules: string[]
  genre: string
}

function loadPrefs(): WGPrefs {
  if (typeof window === 'undefined') return { activeModules: ['show_not_tell', 'micro_tension', 'info_weaving'], genre: '' }
  try {
    const raw = window.localStorage.getItem(LS_KEY)
    if (raw) {
      const p = JSON.parse(raw)
      if (p && Array.isArray(p.activeModules)) return p
    }
  } catch {}
  return { activeModules: ['show_not_tell', 'micro_tension', 'info_weaving'], genre: '' }
}

export function WritingGuidePanel() {
  const initial = loadPrefs()
  const [activeModules, setActiveModules] = useState<Set<string>>(new Set(initial.activeModules))
  const [showProhibitions, setShowProhibitions] = useState(false)
  const [selectedGenre, setSelectedGenre] = useState(initial.genre)

  // Persist on change
  useEffect(() => {
    if (typeof window === 'undefined') return
    const payload: WGPrefs = {
      activeModules: Array.from(activeModules),
      genre: selectedGenre,
    }
    try { window.localStorage.setItem(LS_KEY, JSON.stringify(payload)) } catch {}
  }, [activeModules, selectedGenre])
```

记得顶部 import `useEffect`。

- [ ] **Step 2: typecheck**

Run:
```bash
cd /root/ai-write/frontend && npx tsc --noEmit 2>&1 | head
```
Expected: 无错

- [ ] **Step 3: commit**

```bash
git -C /root/ai-write add frontend/src/components/panels/WritingGuidePanel.tsx
git -C /root/ai-write commit -m "fix(writing-guide-panel): persist module toggles and genre to localStorage"
```

---

## Task 10: 前端 bug E4 — GeneratePanel 选择持久化

**Files:**
- Modify: `frontend/src/components/panels/GeneratePanel.tsx`

- [ ] **Step 1: 改为按 project 持久化**

替换文件顶部模块级变量：

```typescript
// Exported helpers. projectId is optional for backward compatibility with legacy
// call sites that did not have a current project context.
export function getSelectedStyleId(projectId?: string): string | null {
  if (typeof window === 'undefined') return null
  const key = projectId ? `gp:style:${projectId}` : 'gp:style:global'
  return window.localStorage.getItem(key) || null
}
export function getSelectedStructureBookId(projectId?: string): string | null {
  if (typeof window === 'undefined') return null
  const key = projectId ? `gp:structure:${projectId}` : 'gp:structure:global'
  return window.localStorage.getItem(key) || null
}

function setSelectedStyleId(projectId: string | undefined, id: string | null) {
  if (typeof window === 'undefined') return
  const key = projectId ? `gp:style:${projectId}` : 'gp:style:global'
  if (id) window.localStorage.setItem(key, id)
  else window.localStorage.removeItem(key)
}
function setSelectedStructureBookId(projectId: string | undefined, id: string | null) {
  if (typeof window === 'undefined') return
  const key = projectId ? `gp:structure:${projectId}` : 'gp:structure:global'
  if (id) window.localStorage.setItem(key, id)
  else window.localStorage.removeItem(key)
}
```

- [ ] **Step 2: props 加 projectId**

```typescript
interface GeneratePanelProps {
  onGenerate?: () => void
  onGenerateOutline?: (level: string) => void
  projectId?: string
}

export function GeneratePanel({ onGenerate, onGenerateOutline, projectId }: GeneratePanelProps) {
```

`StyleSelector` 和 `StructureSelector` 也加 `projectId?: string` prop。GeneratePanel 内调用 `<StyleSelector projectId={projectId} />` 和 `<StructureSelector projectId={projectId} />`。

- [ ] **Step 3: Selector 初值从 localStorage 读**

在 `StyleSelector`：
```typescript
function StyleSelector({ projectId }: { projectId?: string }) {
  const [styles, setStyles] = useState<StyleInfo[]>([])
  const [loading, setLoading] = useState(true)
  const [selectedId, setSelectedId] = useState<string>(
    () => (typeof window !== 'undefined' ? getSelectedStyleId(projectId) || '' : '')
  )

  useEffect(() => {
    apiFetch<StyleInfo[]>('/api/styles')
      .then(data => {
        setStyles(data)
        if (!selectedId) {
          const active = data.find(s => s.is_active)
          if (active) {
            setSelectedId(active.id)
            setSelectedStyleId(projectId, active.id)
          }
        }
      })
      .catch(() => {})
      .finally(() => setLoading(false))
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  const handleChange = (id: string) => {
    setSelectedId(id)
    setSelectedStyleId(projectId, id || null)
  }
  // ... rest unchanged
}
```

同样改 `StructureSelector`。

- [ ] **Step 4: DesktopWorkspace 调用处传 projectId**

修改 `frontend/src/components/workspace/DesktopWorkspace.tsx` 里 `<GeneratePanel>` 调用：
```tsx
<GeneratePanel
  projectId={currentProject?.id}
  onGenerate={handleGenerateChapter}
  onGenerateOutline={handleGenerateOutline}
/>
```

以及 `getSelectedStyleId()` 调用（搜一下），改为 `getSelectedStyleId(currentProject?.id)`。同理 `getSelectedStructureBookId(currentProject?.id)`（如有使用）。

MobileWorkspace 同理（搜索 `getSelectedStyleId`、`getSelectedStructureBookId`）。

- [ ] **Step 5: typecheck**

Run:
```bash
cd /root/ai-write/frontend && npx tsc --noEmit 2>&1 | head
```
Expected: 无错

- [ ] **Step 6: commit**

```bash
git -C /root/ai-write add frontend/src/components/panels/GeneratePanel.tsx frontend/src/components/workspace/
git -C /root/ai-write commit -m "fix(generate-panel): persist style and structure selection per-project"
```

---

## Task 11: 前端 E5 — 空壳卷检测 + 重试

**Files:**
- Modify: `frontend/src/components/workspace/DesktopWorkspace.tsx`

- [ ] **Step 1: 在 handleGenerateVolumeOutlines 内加失败判定函数**

找到 `handleGenerateVolumeOutlines` 里的 `for (let i = 1; i <= count; i++)` 循环。

在循环体开头（`setWizardProgress(...)` 前）加：

```typescript
    const isEmptyOrInvalid = (p: Record<string, unknown>) => {
      const hasStructure = typeof p.title === 'string' || Array.isArray(p.chapter_summaries) || typeof p.core_conflict === 'string'
      return !hasStructure
    }
```

把原来的"一次 apiSSE + parse + create volume + create chapters"逻辑改成一次 helper `runOnce()`，然后判定 parsed 是否有效。失败则重试一次。重试仍失败则跳过此卷并记进度：

```typescript
    // 完整改写循环体内部，从 setWizardProgress(`正在生成第 ${i}/${count} 卷...`) 开始：
    setWizardProgress(`正在生成第 ${i}/${count} 卷大纲...`)

    const runOnce = async (): Promise<{ text: string; outlineId: string | null; parsed: Record<string, unknown> }> => {
      let text = ''
      let outlineId: string | null = null
      await new Promise<void>((resolve) => {
        apiSSE(
          '/api/generate/outline',
          {
            project_id: currentProject.id,
            level: 'volume',
            volume_idx: i,
            parent_outline_id: confirmedOutlineId,
            user_input: creativeInput,
          },
          (t) => {
            text += t
            setWizardProgress(`正在生成第 ${i}/${count} 卷大纲...\n${text.slice(-200)}`)
          },
          () => resolve(),
          (evt) => {
            if (evt.status === 'saved' && typeof evt.outline_id === 'string') {
              outlineId = evt.outline_id
            }
          },
        )
      })
      return { text, outlineId, parsed: parseVolumeOutline(text) }
    }

    let { text: volumeOutlineText, outlineId: volumeOutlineId, parsed } = await runOnce()
    if (isEmptyOrInvalid(parsed)) {
      setWizardProgress(`第 ${i} 卷首次生成无效，重试中...`)
      const retry = await runOnce()
      volumeOutlineText = retry.text
      volumeOutlineId = retry.outlineId
      parsed = retry.parsed
    }
    if (isEmptyOrInvalid(parsed)) {
      setWizardProgress((prev) => prev + `\n⚠ 第 ${i} 卷生成失败，已跳过`)
      continue
    }

    outlinesByIdx[i] = parsed
    setVolumeOutlines((prev) => ({ ...prev, [i]: parsed }))
```

下面的"Persist parsed"、"create Volume"、"create Chapters"逻辑保持原样。注意变量名 `volumeOutlineText` / `volumeOutlineId` 在下方代码仍然引用。

- [ ] **Step 2: typecheck**

Run:
```bash
cd /root/ai-write/frontend && npx tsc --noEmit 2>&1 | head
```
Expected: 无错

- [ ] **Step 3: commit**

```bash
git -C /root/ai-write add frontend/src/components/workspace/DesktopWorkspace.tsx
git -C /root/ai-write commit -m "fix(volume-gen): retry on empty/invalid LLM output, skip shell creation"
```

---

## Task 12: 前端 E6 — detectVolumeCount 识别前传

**Files:**
- Modify: `frontend/src/components/workspace/DesktopWorkspace.tsx`

- [ ] **Step 1: 扩展函数**

找到 `function detectVolumeCount(text: string): number {`。在返回 `indices.size` 前加关键词扫描：

```typescript
  // 非数字卷：前传/外传/番外/序卷/终章 各至多算一次
  const keywords = ['前传', '外传', '番外', '序卷', '终章', '终卷']
  let extras = 0
  for (const kw of keywords) {
    if (text.includes(kw)) extras += 1
  }

  if (indices.size === 0 && extras === 0) return 0
  return indices.size + extras
```

- [ ] **Step 2: commit**

```bash
git -C /root/ai-write add frontend/src/components/workspace/DesktopWorkspace.tsx
git -C /root/ai-write commit -m "fix(volume-detect): also count 前传/外传/番外/终章 as separate volumes"
```

---

## Task 13: 前端 E7 — 轻量拉取 + 混乱按钮

**Files:**
- Modify: `frontend/src/components/workspace/DesktopWorkspace.tsx`

- [ ] **Step 1: loadProjectData 改用 lightweight**

搜 `` `/api/projects/${projectId}/chapters` ``，改为 `` `/api/projects/${projectId}/chapters?lightweight=true` ``。注意只改 `loadProjectData` 内部那行，不动 chapter 编辑器里的单章 fetch。

- [ ] **Step 2: 移除混乱按钮**

搜 "继续生成分卷"（应该在 editor 视图 `!currentChapter && outlinePreview` 那段）。

把原按钮：
```tsx
<button onClick={() => { setActiveView('wizard'); setWizardStep(2) }}
  className="px-3 py-1.5 text-xs bg-indigo-600 text-white rounded-lg">
  继续生成分卷
</button>
```

改为两个按钮并排：
```tsx
<div className="flex items-center gap-2">
  <button onClick={() => { setActiveView('wizard'); setWizardStep(1) }}
    className="px-3 py-1.5 text-xs border border-gray-300 text-gray-700 rounded-lg hover:bg-gray-50">
    编辑大纲
  </button>
  <button onClick={() => { setActiveView('wizard'); setWizardStep(2) }}
    className="px-3 py-1.5 text-xs border border-gray-300 text-gray-700 rounded-lg hover:bg-gray-50">
    查看分卷
  </button>
</div>
```

- [ ] **Step 3: typecheck**

Run:
```bash
cd /root/ai-write/frontend && npx tsc --noEmit 2>&1 | head
```
Expected: 无错

- [ ] **Step 4: commit**

```bash
git -C /root/ai-write add frontend/src/components/workspace/DesktopWorkspace.tsx
git -C /root/ai-write commit -m "perf(workspace): use lightweight chapters list; replace confusing regenerate button"
```

---

## Task 14: 向导步骤可跳 + Step 1/2 编辑

**Files:**
- Modify: `frontend/src/components/workspace/DesktopWorkspace.tsx`

- [ ] **Step 1: 步骤指示器改成 button**

搜 `wizard steps indicator`（约 line 880-ish）。替换现有 div：

```tsx
{/* Wizard steps indicator */}
<div className="flex items-center gap-2 mb-6">
  {[1, 2, 3].map((step) => (
    <div key={step} className="flex items-center gap-2">
      <button
        onClick={() => setWizardStep(step)}
        className={`w-8 h-8 rounded-full flex items-center justify-center text-sm font-medium transition-colors ${
          wizardStep === step
            ? 'bg-blue-600 text-white'
            : wizardStep > step
              ? 'bg-green-500 text-white hover:bg-green-600'
              : 'bg-gray-200 text-gray-500 hover:bg-gray-300'
        }`}
      >
        {wizardStep > step ? '✓' : step}
      </button>
      {step < 3 && (
        <div className={`w-12 h-0.5 ${wizardStep > step ? 'bg-green-500' : 'bg-gray-200'}`} />
      )}
    </div>
  ))}
</div>
```

- [ ] **Step 2: Step 1 加编辑模式**

Step 1 的"大纲预览"区里，加一个 editing state 和按钮。在 DesktopWorkspace 组件顶部 state 区加：

```typescript
const [outlineEditing, setOutlineEditing] = useState(false)
```

Step 1 outlinePreview 显示块改为：

```tsx
{outlinePreview && (
  <div className="mt-6">
    <div className="flex items-center justify-between mb-2">
      <h3 className="text-sm font-semibold text-gray-700">大纲预览</h3>
      {!isGenerating && confirmedOutlineId && (
        <button
          onClick={() => setOutlineEditing((v) => !v)}
          className="text-xs text-blue-600 hover:underline"
        >
          {outlineEditing ? '取消编辑' : '编辑'}
        </button>
      )}
    </div>
    {outlineEditing ? (
      <div>
        <textarea
          value={outlinePreview}
          onChange={(e) => setOutlinePreview(e.target.value)}
          className="w-full h-96 px-4 py-3 text-sm border border-gray-300 rounded-xl resize-none font-mono"
        />
        <div className="mt-2 flex gap-2">
          <button
            onClick={async () => {
              if (!currentProject || !confirmedOutlineId) return
              await apiFetch(`/api/projects/${currentProject.id}/outlines/${confirmedOutlineId}`, {
                method: 'PUT',
                body: JSON.stringify({ content_json: { raw_text: outlinePreview } }),
              })
              setOutlineEditing(false)
            }}
            className="px-4 py-2 text-sm bg-green-600 text-white rounded-lg"
          >
            保存
          </button>
        </div>
      </div>
    ) : (
      <pre className="whitespace-pre-wrap text-sm text-gray-800 bg-gray-50 p-4 rounded-xl border max-h-96 overflow-y-auto">
        {outlinePreview}
      </pre>
    )}

    {!isGenerating && !confirmedOutlineId && (
      <div className="mt-4 flex gap-3">
        <button onClick={handleConfirmOutline}
          className="px-4 py-2 bg-green-600 text-white rounded-lg text-sm font-medium">
          确认大纲
        </button>
        <button onClick={() => { setOutlinePreview(''); handleGenerateOutline('book') }}
          className="px-4 py-2 bg-gray-200 text-gray-700 rounded-lg text-sm">
          重新生成
        </button>
      </div>
    )}
  </div>
)}
```

注意原"确认大纲 + 重新生成"按钮组留在未确认时显示；编辑按钮仅在已确认时出现。

- [ ] **Step 3: Step 2 每卷折叠加编辑**

Step 2 的"已生成分卷"显示不存在？当前实现 Step 2 只显示输入 + 按钮 + 进度。需要加一段"已生成分卷"列表。

在 Step 2 的 `<button onClick={handleGenerateVolumeOutlines}>` 下方追加：

```tsx
{Object.keys(volumeOutlines).length > 0 && (
  <div className="mt-6 space-y-2">
    <h3 className="text-sm font-semibold text-gray-700">已生成分卷</h3>
    {volumes
      .slice()
      .sort((a, b) => (a.volume_idx ?? a.volumeIdx) - (b.volume_idx ?? b.volumeIdx))
      .map((v) => {
        const vi = v.volume_idx ?? v.volumeIdx
        const vo = volumeOutlines[vi]
        if (!vo) return null
        return (
          <VolumeOutlineEditor
            key={v.id}
            volume={v}
            data={vo}
            projectId={currentProject!.id}
            onSaved={(updated) => setVolumeOutlines((prev) => ({ ...prev, [vi]: updated }))}
          />
        )
      })}
  </div>
)}
```

新建组件 `VolumeOutlineEditor` 放在文件末尾（helpers 附近）：

```tsx
function VolumeOutlineEditor({
  volume, data, projectId, onSaved,
}: {
  volume: Volume
  data: Record<string, unknown>
  projectId: string
  onSaved: (data: Record<string, unknown>) => void
}) {
  const [editing, setEditing] = React.useState(false)
  const [text, setText] = React.useState(() => {
    if (typeof data.raw_text === 'string') return data.raw_text
    return JSON.stringify(data, null, 2)
  })
  const [busy, setBusy] = React.useState(false)

  const save = async () => {
    if (busy) return
    setBusy(true)
    try {
      // Find the outline id that belongs to this volume
      const outlines = await apiFetch<OutlineRes[]>(`/api/projects/${projectId}/outlines?level=volume`)
      const target = outlines.find((o) => {
        const cj = (o.content_json as Record<string, unknown>) || {}
        return cj.volume_idx === (volume.volume_idx ?? volume.volumeIdx)
      })
      if (!target) return
      let contentJson: Record<string, unknown>
      try {
        contentJson = JSON.parse(text)
      } catch {
        contentJson = { ...data, raw_text: text }
      }
      await apiFetch(`/api/projects/${projectId}/outlines/${target.id}`, {
        method: 'PUT',
        body: JSON.stringify({ content_json: contentJson }),
      })
      onSaved(contentJson)
      setEditing(false)
    } finally {
      setBusy(false)
    }
  }

  return (
    <details className="border rounded-xl overflow-hidden">
      <summary className="cursor-pointer px-4 py-2 bg-gray-50 text-sm font-medium text-gray-700 hover:bg-gray-100 flex items-center justify-between">
        <span>{volume.title}</span>
        <button
          onClick={(e) => { e.preventDefault(); setEditing((v) => !v) }}
          className="text-xs text-blue-600 hover:underline"
        >
          {editing ? '取消' : '编辑'}
        </button>
      </summary>
      <div className="px-4 py-3 bg-white border-t text-sm">
        {editing ? (
          <div>
            <textarea
              value={text}
              onChange={(e) => setText(e.target.value)}
              className="w-full h-64 px-3 py-2 text-xs border border-gray-300 rounded-lg font-mono resize-none"
            />
            <div className="mt-2">
              <button onClick={save} disabled={busy}
                className="px-4 py-1.5 text-sm bg-green-600 text-white rounded-lg disabled:opacity-50">
                {busy ? '保存中...' : '保存'}
              </button>
            </div>
          </div>
        ) : (
          <VolumeOutlineBlock data={data} />
        )}
      </div>
    </details>
  )
}
```

顶部 import React 已有。`Volume`/`OutlineRes` 类型已有。

- [ ] **Step 4: 按钮文案随状态变化**

Step 2 中 `handleGenerateVolumeOutlines` 按钮改为：

```tsx
<button
  onClick={handleGenerateVolumeOutlines}
  disabled={isGenerating || !confirmedOutlineId}
  className="px-6 py-2.5 bg-indigo-600 text-white rounded-xl hover:bg-indigo-700 disabled:opacity-50 disabled:cursor-not-allowed text-sm font-medium"
>
  {isGenerating ? '正在生成...' : (volumes.length > 0 ? '补齐缺失卷' : '生成分卷大纲')}
</button>
```

同时修改 `handleGenerateVolumeOutlines` 循环：如果 volume_idx i 已经存在 volume（`volumes.find(v => (v.volume_idx ?? v.volumeIdx) === i)`），跳过；否则生成。循环开头加：

```typescript
      const existing = volumes.find((v) => (v.volume_idx ?? v.volumeIdx) === i)
      if (existing) {
        setWizardProgress(`第 ${i} 卷已存在，跳过`)
        continue
      }
```

- [ ] **Step 5: typecheck**

Run:
```bash
cd /root/ai-write/frontend && npx tsc --noEmit 2>&1 | head -10
```
Expected: 无错

- [ ] **Step 6: commit**

```bash
git -C /root/ai-write add frontend/src/components/workspace/DesktopWorkspace.tsx
git -C /root/ai-write commit -m "feat(wizard): jumpable steps + inline edit book outline + editable per-volume outline + gap-fill generation"
```

---

## Task 15: 项目设置 modal（字数目标）

**Files:**
- Create: `frontend/src/components/project/ProjectSettingsModal.tsx`
- Modify: `frontend/src/components/project/ProjectCard.tsx` (menu)
- Modify: `frontend/src/components/project/ProjectListPage.tsx` (wire modal)

- [ ] **Step 1: 新建 ProjectSettingsModal**

```typescript
'use client'

import { useState } from 'react'
import { apiFetch } from '@/lib/api'
import type { Project } from '@/stores/projectStore'

interface Settings {
  target_total_words?: number | null
  target_chapter_words?: number | null
}

export function ProjectSettingsModal({
  project,
  onClose,
  onDone,
}: {
  project: Project
  onClose: () => void
  onDone: () => void
}) {
  // settings_json lives inside Project; fall back to empty
  const initial = (project as unknown as { settings_json?: Settings }).settings_json || {}
  const [totalStr, setTotalStr] = useState(initial.target_total_words ? String(initial.target_total_words) : '')
  const [chapterStr, setChapterStr] = useState(initial.target_chapter_words ? String(initial.target_chapter_words) : '')
  const [busy, setBusy] = useState(false)

  const parseNum = (s: string): number | null => {
    const trimmed = s.trim()
    if (!trimmed) return null
    const n = parseInt(trimmed, 10)
    if (Number.isNaN(n) || n <= 0) return null
    return n
  }

  const save = async () => {
    if (busy) return
    setBusy(true)
    try {
      const next: Settings = {
        target_total_words: parseNum(totalStr),
        target_chapter_words: parseNum(chapterStr),
      }
      await apiFetch(`/api/projects/${project.id}`, {
        method: 'PUT',
        body: JSON.stringify({ settings_json: { ...initial, ...next } }),
      })
      onDone()
    } finally { setBusy(false) }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div className="bg-white rounded-2xl shadow-xl w-full max-w-md mx-4 p-6">
        <h3 className="text-lg font-bold text-gray-900 mb-4">项目设置 — {project.title}</h3>
        <div className="space-y-4">
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">全书目标字数</label>
            <input
              type="number" min={1}
              value={totalStr}
              onChange={(e) => setTotalStr(e.target.value)}
              placeholder="留空为不限"
              className="w-full px-3 py-2 text-sm border border-gray-300 rounded-lg"
            />
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">单章默认字数</label>
            <input
              type="number" min={1}
              value={chapterStr}
              onChange={(e) => setChapterStr(e.target.value)}
              placeholder="留空为不限（如 3000）"
              className="w-full px-3 py-2 text-sm border border-gray-300 rounded-lg"
            />
          </div>
        </div>
        <div className="flex gap-3 mt-6">
          <button onClick={onClose} className="flex-1 px-4 py-2 text-sm border border-gray-300 rounded-lg">取消</button>
          <button onClick={save} disabled={busy}
            className="flex-1 px-4 py-2 text-sm bg-blue-600 text-white rounded-lg disabled:opacity-50">
            {busy ? '保存中...' : '保存'}
          </button>
        </div>
      </div>
    </div>
  )
}
```

- [ ] **Step 2: ProjectCardMenu 加"设置"项**

编辑 `frontend/src/components/project/ProjectCard.tsx`：

- `ProjectCardMenu` 的 props 加 `onSettings: () => void`
- 菜单中在"重命名"和"删除"之间加一个按钮：

```tsx
<button
  onClick={() => { setOpen(false); onSettings() }}
  className="block w-full text-left px-3 py-2 text-sm hover:bg-gray-50"
>
  项目设置
</button>
```

- `ProjectCard` 的 Props 加 `onSettings: (p: Project) => void`，在 `<ProjectCardMenu>` 里传 `onSettings={() => onSettings(project)}`

- [ ] **Step 3: ProjectListPage 挂 modal**

编辑 `frontend/src/components/project/ProjectListPage.tsx`：

- 顶部 import 加：
```typescript
import { ProjectSettingsModal } from './ProjectSettingsModal'
```
- state 加：
```typescript
const [settingsTarget, setSettingsTarget] = useState<Project | null>(null)
```
- `<ProjectCard ... onSettings={setSettingsTarget} />`（新增 prop）
- 底部 modals 区域追加：
```tsx
{settingsTarget && (
  <ProjectSettingsModal
    project={settingsTarget}
    onClose={() => setSettingsTarget(null)}
    onDone={async () => { setSettingsTarget(null); await load() }}
  />
)}
```

- [ ] **Step 4: 前端 Project 接口加 settings_json**

编辑 `frontend/src/stores/projectStore.ts` 的 `Project` 接口加字段：
```typescript
settings_json?: {
  target_total_words?: number | null
  target_chapter_words?: number | null
  [key: string]: unknown
} | null
```

- [ ] **Step 5: typecheck**

Run:
```bash
cd /root/ai-write/frontend && npx tsc --noEmit 2>&1 | head -10
```
Expected: 无错

- [ ] **Step 6: commit**

```bash
git -C /root/ai-write add frontend/src/components/project/ frontend/src/stores/projectStore.ts
git -C /root/ai-write commit -m "feat(project): settings modal for total/chapter word count targets"
```

---

## Task 16: 章节 target_words 行内编辑

**Files:**
- Modify: `frontend/src/components/workspace/DesktopWorkspace.tsx`

- [ ] **Step 1: 在章节编辑器头部加输入**

搜 `<h3 className="text-lg font-semibold text-gray-800">` 里 `currentChapter.title`。在该行所在 `<div className="flex items-center gap-2">`（显示字数和状态的那个）里前插一个 target_words 编辑器：

```tsx
<ChapterTargetWordsEditor
  projectId={currentProject!.id}
  chapter={currentChapter}
  projectDefault={
    (currentProject as unknown as { settings_json?: { target_chapter_words?: number } }).settings_json?.target_chapter_words ?? null
  }
  onSaved={() => loadProjectData(currentProject!.id)}
/>
```

- [ ] **Step 2: 新 helper 组件（放在文件末尾）**

```tsx
function ChapterTargetWordsEditor({
  projectId, chapter, projectDefault, onSaved,
}: {
  projectId: string
  chapter: Chapter
  projectDefault: number | null
  onSaved: () => void
}) {
  const initial = (chapter as unknown as { target_words?: number | null }).target_words ?? null
  const [text, setText] = React.useState(initial != null ? String(initial) : '')
  const [editing, setEditing] = React.useState(false)
  const effective = initial != null ? initial : projectDefault
  const save = async () => {
    const n = text.trim() ? parseInt(text.trim(), 10) : null
    if (text.trim() && (Number.isNaN(n!) || n! <= 0)) return
    await apiFetch(`/api/projects/${projectId}/chapters/${chapter.id}`, {
      method: 'PUT',
      body: JSON.stringify({ target_words: n }),
    })
    setEditing(false)
    onSaved()
  }
  return (
    <span className="text-xs text-gray-500">
      {editing ? (
        <>
          目标：
          <input
            type="number" min={0}
            value={text}
            onChange={(e) => setText(e.target.value)}
            onBlur={save}
            onKeyDown={(e) => { if (e.key === 'Enter') save() }}
            autoFocus
            className="w-20 px-1 py-0.5 text-xs border border-blue-300 rounded ml-1"
            placeholder={projectDefault ? String(projectDefault) : '默认'}
          />
        </>
      ) : (
        <button onClick={() => setEditing(true)} className="hover:text-gray-800">
          目标 {effective ? `${effective.toLocaleString()} 字` : '未设'}{initial == null && projectDefault ? '（默认）' : ''}
        </button>
      )}
    </span>
  )
}
```

- [ ] **Step 3: ChapterUpdate schema 已支持 target_words？**

查看 `backend/app/api/chapters.py` 中 `ChapterUpdate`。若没有 `target_words` 字段则加：

```python
class ChapterUpdate(BaseModel):
    title: str | None = None
    content_text: str | None = None
    outline_json: dict | None = None
    status: str | None = None
    target_words: int | None = None
```

并在 `update_chapter` 里把 `body.target_words` 赋值 `chapter.target_words`：

```python
    if body.target_words is not None or "target_words" in body.model_dump(exclude_unset=True):
        chapter.target_words = body.target_words
```

等价简写：用 `exclude_unset=True` 直接应用。

- [ ] **Step 4: typecheck + restart backend**

```bash
docker compose -f /root/ai-write/docker-compose.yml restart backend; sleep 3
cd /root/ai-write/frontend && npx tsc --noEmit 2>&1 | head
```
Expected: 无错

- [ ] **Step 5: commit**

```bash
git -C /root/ai-write add frontend/src/components/workspace/DesktopWorkspace.tsx backend/app/api/chapters.py
git -C /root/ai-write commit -m "feat(chapter): per-chapter target_words editor with project default fallback"
```

---

## Task 17: 单卷重生 UI + 侧栏菜单

**Files:**
- Create: `frontend/src/components/outline/RegenerateVolumeModal.tsx`
- Modify: `frontend/src/components/outline/OutlineTree.tsx`

- [ ] **Step 1: RegenerateVolumeModal**

```typescript
'use client'

import { useState } from 'react'
import { apiSSE } from '@/lib/api'

export function RegenerateVolumeModal({
  projectId, volumeId, volumeTitle, chapterCount, onClose, onDone,
}: {
  projectId: string
  volumeId: string
  volumeTitle: string
  chapterCount: number
  onClose: () => void
  onDone: () => void
}) {
  const [busy, setBusy] = useState(false)
  const [progress, setProgress] = useState('')

  const go = () => {
    if (busy) return
    setBusy(true)
    setProgress('准备中...')
    apiSSE(
      `/api/projects/${projectId}/volumes/${volumeId}/regenerate`,
      {},
      (text) => setProgress((p) => (p + text).slice(-600)),
      () => { setBusy(false); onDone() },
      (evt) => {
        if (evt.status === 'done') setProgress(`已生成 ${evt.chapters_created} 章`)
        if (evt.error) setProgress(`错误：${evt.error}`)
      },
    )
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div className="bg-white rounded-2xl shadow-xl w-full max-w-md mx-4 p-6">
        <h3 className="text-lg font-bold text-red-600 mb-2">⚠ 重新生成卷大纲</h3>
        <p className="text-sm text-gray-700">
          卷「{volumeTitle}」下 {chapterCount} 章内容和本卷大纲将被删除，然后 AI 将根据全书大纲重新生成。此操作不可撤销。
        </p>
        {progress && (
          <pre className="mt-3 text-xs text-gray-700 bg-gray-50 p-3 rounded border max-h-48 overflow-y-auto whitespace-pre-wrap">
            {progress}
          </pre>
        )}
        <div className="flex gap-3 mt-6">
          <button onClick={onClose} disabled={busy}
            className="flex-1 px-4 py-2 text-sm border border-gray-300 rounded-lg hover:bg-gray-50 disabled:opacity-50">
            {busy ? '运行中...' : '取消'}
          </button>
          {!busy && (
            <button onClick={go}
              className="flex-1 px-4 py-2 text-sm bg-red-600 text-white rounded-lg">
              确认重生
            </button>
          )}
        </div>
      </div>
    </div>
  )
}
```

- [ ] **Step 2: OutlineTree 菜单加项**

编辑 `frontend/src/components/outline/OutlineTree.tsx`：

1. 顶部 import：
```typescript
import { RegenerateVolumeModal } from './RegenerateVolumeModal'
```

2. state 加：
```typescript
const [regenerateVolume, setRegenerateVolume] = useState<{ id: string; title: string; chapterCount: number } | null>(null)
```

3. 在 volume 行的 RowMenu items 里加一项（在"重命名"和"删除"之间）：
```typescript
{
  label: '重新生成',
  onClick: () => setRegenerateVolume({ id: volume.id, title: volume.title, chapterCount: volChapters.length }),
},
```

4. 组件末尾（在 `deleteChapter` modal 后面）追加 modal：
```tsx
{regenerateVolume && (
  <RegenerateVolumeModal
    projectId={projectId}
    volumeId={regenerateVolume.id}
    volumeTitle={regenerateVolume.title}
    chapterCount={regenerateVolume.chapterCount}
    onClose={() => setRegenerateVolume(null)}
    onDone={() => { setRegenerateVolume(null); onChanged?.() }}
  />
)}
```

- [ ] **Step 3: typecheck**

```bash
cd /root/ai-write/frontend && npx tsc --noEmit 2>&1 | head -10
```
Expected: 无错

- [ ] **Step 4: commit**

```bash
git -C /root/ai-write add frontend/src/components/outline/
git -C /root/ai-write commit -m "feat(outline): single-volume regenerate via sidebar three-dot menu (SSE)"
```

---

## Task 18: 生成章节正文时注入 target_words

**Files:**
- Modify: `backend/app/api/generate.py`
- Modify: `backend/app/services/chapter_generator.py`

- [ ] **Step 1: generate_chapter 端点读取 target**

编辑 `backend/app/api/generate.py` 的 `generate_chapter`：在查完 `chapter` 之后加读取 target_words 和 project 默认：

```python
    target_words = None
    if chapter and chapter.target_words:
        target_words = chapter.target_words
    elif project_settings and isinstance(project_settings.get("target_chapter_words"), int):
        target_words = project_settings["target_chapter_words"]
```

- [ ] **Step 2: chapter_generator 使用 target_words**

`chapter_generator.generate_stream` 签名加 `target_words: int | None = None`。在 system/user 消息里追加约束：

```python
    if target_words:
        user_instruction = (user_instruction or "") + f"\n\n【本章目标字数】约 {target_words} 字（允许 ±15% 浮动）。"
```

把这段加在 `user_instruction` 被使用前（看 generator 现有代码，找到组装 messages 的地方）。

- [ ] **Step 3: generate_chapter 传递 target_words**

`generator.generate_stream(..., target_words=target_words)`.

- [ ] **Step 4: 冒烟（只确认不崩）**

```bash
docker compose -f /root/ai-write/docker-compose.yml restart backend; sleep 3
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/api/projects | head
```
Expected: 401（未鉴权），证明 backend 仍运行

- [ ] **Step 5: commit**

```bash
git -C /root/ai-write add backend/app/api/generate.py backend/app/services/chapter_generator.py
git -C /root/ai-write commit -m "feat(generate): inject target_words into chapter generation prompt"
```

---

## Task 19: 部署 + 冒烟

- [ ] **Step 1: 重建前端**

```bash
cd /root/ai-write && docker compose up -d --build frontend 2>&1 | tail -5
```

- [ ] **Step 2: 路由冒烟**

```bash
for path in / /workspace /trash; do
  code=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:8080${path}")
  echo "$code  $path"
done
```
Expected: 3 个都 200

- [ ] **Step 3: API 冒烟（relationships 端到端）**

```bash
T=$(curl -s -X POST http://127.0.0.1:8000/api/auth/login -H 'Content-Type: application/json' -d '{"username":"king","password":"Wt991125"}' | python3 -c "import sys,json;print(json.load(sys.stdin)['token'])")
PID=6e331209-056b-4b2b-9798-ac246ee8dd48
curl -s -H "Authorization: Bearer $T" "http://127.0.0.1:8000/api/projects/$PID/relationships" | python3 -c "import sys,json;d=json.load(sys.stdin);print('rels total:', d['total'])"
```

Expected: 新 API 正常返 JSON

- [ ] **Step 4: 浏览器验证清单**

（人工）
1. 打开一个项目 → 设定集面板不崩，显示角色和规则
2. 协作指南切几个开关 → 刷新仍保留
3. GeneratePanel 切换写作风格 → 刷新仍保留
4. 角色关系图显示线 + label + sentiment 色
5. 向导 Step 1 / 2 可跳转、可编辑
6. 项目卡片"项目设置"弹字数目标
7. 章节标题旁"目标字数"可编辑
8. 侧栏卷三点菜单"重新生成" → 模态运行 SSE → 完成

---

## 非目标

- 不做 undo/redo / 编辑历史
- 不做字数强制上限
- 不做关系图拖拽编辑（仅数据可视化 + API）
- 不做整本"一键重生"
- 不拆 DesktopWorkspace.tsx（现已过 1500 行，但重构不属本次范围）
