"""API tests for POST /api/styles/rebuild-from-dossier (档案 → 画像).

Covers: book-dossier rebuild (rule/anti-AI/keyword derivation shape + book
binding), author-dossier rebuild (global, merged vocab keywords), idempotent
overwrite of the same-named profile, 404 when no dossier exists, and 400
when not exactly one of book_id/author is given.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete, select, text

from app.db.session import async_session_factory
from app.models.author_dossier import AuthorDossier
from app.models.project import ReferenceBook, StyleProfile

# DDL mirrors alembic a1001920 exactly (idempotent).
_AUTHOR_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS author_dossiers (
  id uuid PRIMARY KEY,
  author varchar(200) NOT NULL UNIQUE,
  status_json jsonb,
  dossier_json jsonb,
  source_book_ids_json jsonb,
  created_at timestamptz,
  updated_at timestamptz
);
"""

_STYLE_DATA = {
    "profile": {
        "narrative_pov_rules": "第三人称有限视角，场景切换时换段",
        "syntax_rhythm": "短句为主，高潮段落连续短句",
        "dialogue_style": "口语化，对话密度高",
        "sensory_rhetoric": "偏听觉与触觉",
        "emotional_curve": "主导模态冷峻克制",
        "low_freq_burst": "每卷一次抒情高峰，篇幅约千字",
        "opening_patterns": "以动作切入",
        "hook_patterns": "章末留悬念",
        "signature_moves": ["以天气写心", "冷幽默反差", "第三条应被丢弃"],
        "forbidden": ["避免大段总结性抒情", "璀璨"],
        "evidence_quotes": ["刀光一闪"],
    },
    "stats": {
        "vocab_tone": [{"value": "硬核", "count": 9}, {"value": "冷峻", "count": 5}],
    },
}


async def _create_book(title: str, *, with_dossier: bool) -> str:
    meta = {"dossier": {"style_data": _STYLE_DATA}} if with_dossier else {}
    async with async_session_factory() as db:
        book = ReferenceBook(
            title=title, source="upload_txt", status="ready", metadata_json=meta,
        )
        db.add(book)
        await db.commit()
        return str(book.id)


async def _cleanup(book_id: str | None = None, author: str | None = None,
                   profile_name: str | None = None) -> None:
    async with async_session_factory() as db:
        if profile_name:
            await db.execute(
                delete(StyleProfile).where(StyleProfile.name == profile_name)
            )
        if book_id:
            await db.execute(
                delete(ReferenceBook).where(ReferenceBook.id == book_id)
            )
        if author:
            await db.execute(
                delete(AuthorDossier).where(AuthorDossier.author == author)
            )
        await db.commit()


async def _load_profile(profile_id: str) -> StyleProfile | None:
    async with async_session_factory() as db:
        return await db.get(StyleProfile, profile_id)


@pytest.mark.asyncio
async def test_rebuild_from_book_dossier_rule_derivation(auth_client) -> None:
    book_id = await _create_book("重建测试书", with_dossier=True)
    name = "重建测试书·档案重建"
    try:
        resp = await auth_client.post(
            "/api/styles/rebuild-from-dossier", json={"book_id": book_id}
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["name"] == name
        assert body["created"] is True
        assert body["bind_level"] == "book"
        # 8 facets + 2 signature moves (third dropped)
        assert body["rule_count"] == 10
        assert body["anti_ai_count"] == 2

        profile = await _load_profile(body["profile_id"])
        assert profile is not None
        assert profile.bind_level == "book"
        assert str(profile.bind_target_id) == book_id
        assert str(profile.source_book_id) == book_id
        assert profile.source_book == "重建测试书"
        assert (profile.config_json or {}).get("rebuilt_from_dossier") == {
            "book_id": book_id
        }

        # Rule shape: every entry {rule, weight, category} with sane weights
        rules = profile.rules_json
        assert len(rules) == 10
        for r in rules:
            assert set(r) == {"rule", "weight", "category"}
            assert 0.0 < r["weight"] <= 1.0
        rule_texts = [r["rule"] for r in rules]
        assert "视角：第三人称有限视角，场景切换时换段" in rule_texts
        assert "低频爆发段：每卷一次抒情高峰，篇幅约千字" in rule_texts
        assert "标志性手法：以天气写心" in rule_texts
        assert "标志性手法：冷幽默反差" in rule_texts
        assert not any("第三条应被丢弃" in t for t in rule_texts)

        # anti_ai from 禁忌 only; imperative prefix stripped (no 避免-避免)
        patterns = [a["pattern"] for a in profile.anti_ai_rules]
        assert patterns == ["大段总结性抒情", "璀璨"]
        assert all(a["autoRewrite"] is False for a in profile.anti_ai_rules)

        # tone keywords from vocab preferences
        assert profile.tone_keywords == ["硬核", "冷峻"]
    finally:
        await _cleanup(book_id=book_id, profile_name=name)


@pytest.mark.asyncio
async def test_rebuild_is_idempotent_overwrite(auth_client) -> None:
    book_id = await _create_book("重建幂等书", with_dossier=True)
    name = "重建幂等书·档案重建"
    try:
        first = await auth_client.post(
            "/api/styles/rebuild-from-dossier", json={"book_id": book_id}
        )
        assert first.status_code == 200, first.text
        second = await auth_client.post(
            "/api/styles/rebuild-from-dossier", json={"book_id": book_id}
        )
        assert second.status_code == 200, second.text
        assert second.json()["profile_id"] == first.json()["profile_id"]
        assert second.json()["created"] is False

        async with async_session_factory() as db:
            rows = (
                await db.execute(
                    select(StyleProfile).where(StyleProfile.name == name)
                )
            ).scalars().all()
            assert len(rows) == 1
    finally:
        await _cleanup(book_id=book_id, profile_name=name)


@pytest.mark.asyncio
async def test_rebuild_from_author_dossier(auth_client) -> None:
    author = "重建测试作者"
    name = f"{author}·档案重建"
    author_style = {
        "profile": dict(_STYLE_DATA["profile"]),
        "cross_book": {
            "merged_top": {"vocab_tone": [{"value": "古风", "count": 7}]},
        },
    }
    async with async_session_factory() as db:
        await db.execute(text(_AUTHOR_TABLE_DDL))
        db.add(AuthorDossier(
            author=author,
            status_json={"state": "done"},
            dossier_json={"style_data": author_style},
            source_book_ids_json=[],
        ))
        await db.commit()
    try:
        resp = await auth_client.post(
            "/api/styles/rebuild-from-dossier", json={"author": author}
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["name"] == name
        # No author bind level exists in the runtime chain -> unbound/global
        assert body["bind_level"] == "global"
        assert body["rule_count"] == 10

        profile = await _load_profile(body["profile_id"])
        assert profile.bind_level == "global"
        assert profile.bind_target_id is None
        assert profile.source_book_id is None
        assert profile.source_book == f"author:{author}"
        # author keywords come from the merged cross-book vocab table
        assert profile.tone_keywords == ["古风"]
    finally:
        await _cleanup(author=author, profile_name=name)


@pytest.mark.asyncio
async def test_rebuild_404_when_no_dossier(auth_client) -> None:
    # book exists but has no dossier
    book_id = await _create_book("无档案书", with_dossier=False)
    try:
        resp = await auth_client.post(
            "/api/styles/rebuild-from-dossier", json={"book_id": book_id}
        )
        assert resp.status_code == 404
    finally:
        await _cleanup(book_id=book_id)

    # missing book / unknown author
    resp = await auth_client.post(
        "/api/styles/rebuild-from-dossier", json={"book_id": str(uuid.uuid4())}
    )
    assert resp.status_code == 404
    resp = await auth_client.post(
        "/api/styles/rebuild-from-dossier", json={"author": "不存在的作者"}
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_rebuild_400_requires_exactly_one_target(auth_client) -> None:
    resp = await auth_client.post("/api/styles/rebuild-from-dossier", json={})
    assert resp.status_code == 400
    resp = await auth_client.post(
        "/api/styles/rebuild-from-dossier",
        json={"book_id": str(uuid.uuid4()), "author": "某作者"},
    )
    assert resp.status_code == 400
