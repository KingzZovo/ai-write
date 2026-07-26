"""SSE scene-mode fallback must discard partial scene text (regression for duplicated chapter content).

Bug (B1): when SceneOrchestrator's stream fails mid-chapter, the SSE endpoint
falls back to the single-shot ChapterGenerator but did NOT clear the partial
scene chunks already appended to ``collected_text``. The saved
``full_text = "".join(collected_text)`` then contained
"half a scene + the full fallback chapter" — duplicated/corrupted content.
The Celery path (knowledge_tasks.py) clears its buffer; the API path must too.
"""

import json
import uuid

import pytest

PARTIAL_CHUNKS = ["【半截场景】夜色压住了城。", "他刚摸到墙头，鼓声忽然停了。"]
FALLBACK_TEXT = "【回退正文】这是单发生成器重写的完整章节，应当是唯一被保存的内容。"


def _parse_sse_events(body: str) -> list[dict]:
    """Parse `data: {...}` SSE lines into dicts (skipping the [DONE] marker)."""
    events = []
    for line in body.splitlines():
        if line.startswith("data: ") and line != "data: [DONE]":
            events.append(json.loads(line[len("data: "):]))
    return events


class _FakeReport:
    passed = True

    def to_safe_dict(self) -> dict:
        return {"passed": True}


class _FakeGateResult:
    """Minimal stand-in for ChapterQualityGateResult used by the endpoint."""

    def __init__(self, text: str):
        self.status = "passed"
        self.warning_reason = None
        self.rewrite_rounds = 0
        self.final_text = text
        self.final_report = _FakeReport()

    def to_safe_metadata(self) -> dict:
        return {}


@pytest.mark.asyncio
async def test_scene_fallback_discards_partial_scene_text(auth_client, monkeypatch):
    """Scene stream yields 2 chunks then dies -> fallback runs -> the full_text
    handed to the save/quality path must contain ONLY the fallback text, and the
    SSE stream must emit a `fallback_restart` event so the client can reset."""
    import app.api.generate as gen_mod
    from app.services.chapter_generator import ChapterGenerator
    from app.services.scene_orchestrator import SceneOrchestrator

    # --- real project row: the endpoint loads it from the DB up front ---
    resp = await auth_client.post(
        "/api/projects", json={"title": "SSE回退测试项目", "genre": "测试"}
    )
    assert resp.status_code == 201, resp.text
    project_id = resp.json()["id"]

    try:
        # Readiness gate: pretend the outline chain is complete.
        class _Ready:
            ready = True

        async def _fake_readiness(*args, **kwargs):
            return _Ready()

        monkeypatch.setattr(gen_mod, "build_outline_readiness_report", _fake_readiness)

        # Scene mode: stream two chunks, then explode mid-chapter.
        def _broken_scene_stream(self, **kwargs):
            async def _gen():
                for chunk in PARTIAL_CHUNKS:
                    yield chunk
                raise RuntimeError("scene_writer exploded mid-stream (test)")

            return _gen()

        monkeypatch.setattr(
            SceneOrchestrator, "orchestrate_chapter_stream", _broken_scene_stream
        )

        # Single-shot fallback: deterministic full-chapter text.
        async def _fake_single_shot(self, **kwargs):
            return FALLBACK_TEXT

        monkeypatch.setattr(ChapterGenerator, "generate", _fake_single_shot)

        # Quality gate: capture the exact full_text the endpoint is about to
        # persist (the save itself runs in a detached bg task, so this is the
        # cleanest observation point) and pass it through unchanged.
        captured: dict = {}

        monkeypatch.setattr(
            gen_mod, "analyze_chinese_prose_mechanics", lambda text: _FakeReport()
        )

        async def _fake_quality_gate(*, text, **kwargs):
            captured["full_text"] = text
            return _FakeGateResult(text)

        monkeypatch.setattr(gen_mod, "apply_chapter_quality_gate", _fake_quality_gate)
        # generate.py now routes polish through run_chapter_pipeline, which calls
        # apply_chapter_quality_gate from its OWN module namespace. Patch there too
        # so this test observes the text handed to the gate (the pipeline's logic
        # critic is skipped for sub-200-char fallback text, so it goes straight here).
        import app.services.chapter_pipeline as pipeline_mod

        monkeypatch.setattr(
            pipeline_mod, "apply_chapter_quality_gate", _fake_quality_gate
        )

        resp = await auth_client.post(
            "/api/generate/chapter",
            json={
                "project_id": project_id,
                "volume_id": str(uuid.uuid4()),
                "chapter_idx": 1,
                "use_scene_mode": True,
                "auto_revise": False,
                "skip_polish": False,
            },
        )
        assert resp.status_code == 200, resp.text
        events = _parse_sse_events(resp.text)

        # Partial chunks were streamed before the failure.
        text_events = [e["text"] for e in events if "text" in e]
        assert text_events[: len(PARTIAL_CHUNKS)] == PARTIAL_CHUNKS

        # Core regression assertion: the text handed to the quality gate /
        # save path is exactly the fallback text — no half-scene prefix.
        assert "full_text" in captured, f"quality gate never ran; events: {events}"
        assert captured["full_text"] == FALLBACK_TEXT, (
            "partial scene text leaked into the saved full_text: "
            f"{captured['full_text']!r}"
        )
        for chunk in PARTIAL_CHUNKS:
            assert chunk not in captured["full_text"]

        # The client gets a fallback_restart event so it can discard the
        # half-rendered scene text.
        assert any(
            e.get("event") == "fallback_restart" for e in events
        ), f"missing fallback_restart event in SSE stream: {events}"
    finally:
        await auth_client.delete(f"/api/projects/{project_id}")


# ---------------------------------------------------------------------------
# PR-SCENE-PARTIAL: both generation paths fail -> partial scenes saved as draft
# ---------------------------------------------------------------------------

# One full scene (>=200 chars, well-terminated sentences) + a fragment of the
# second scene that died mid-stream. The whole accumulated text is preserved.
SCENE1_TEXT = "夜色像浸了墨的布，一层层压住整座城，巡夜的梆子声隔着雨幕闷闷传来。" * 10
SCENE2_FRAGMENT = "他刚摸到墙头，鼓声忽然停了"


@pytest.mark.asyncio
async def test_partial_scene_text_preserved_as_draft_when_both_paths_fail(
    auth_client, monkeypatch
):
    """Scene stream dies mid-scene-2 AND the single-shot fallback dies too:
    the accumulated scene text must be persisted as a draft (not discarded),
    resent after fallback_restart, and announced via a partial_saved event."""
    import app.api.generate as gen_mod
    import app.services.chapter_summarizer as summarizer_mod
    import app.services.entity_dispatch as entity_dispatch_mod
    import app.services.memory as memory_mod
    from app.services.chapter_generator import ChapterGenerator
    from app.services.scene_orchestrator import SceneOrchestrator

    resp = await auth_client.post(
        "/api/projects", json={"title": "部分场景保存测试项目", "genre": "测试"}
    )
    assert resp.status_code == 201, resp.text
    project_id = resp.json()["id"]

    try:
        v = await auth_client.post(
            f"/api/projects/{project_id}/volumes",
            json={"title": "第一卷", "volume_idx": 1},
        )
        assert v.status_code == 201, v.text
        volume_id = v.json()["id"]
        c = await auth_client.post(
            f"/api/projects/{project_id}/chapters",
            json={"volume_id": volume_id, "title": "第一章", "chapter_idx": 1},
        )
        assert c.status_code == 201, c.text
        chapter_id = c.json()["id"]

        class _Ready:
            ready = True

        async def _fake_readiness(*args, **kwargs):
            return _Ready()

        monkeypatch.setattr(gen_mod, "build_outline_readiness_report", _fake_readiness)

        # Scene mode: scene 1 completes, scene 2 dies mid-stream.
        def _broken_scene_stream(self, **kwargs):
            async def _gen():
                yield SCENE1_TEXT
                yield "\n\n"
                yield SCENE2_FRAGMENT
                raise RuntimeError("scene_writer exploded mid-scene-2 (test)")

            return _gen()

        monkeypatch.setattr(
            SceneOrchestrator, "orchestrate_chapter_stream", _broken_scene_stream
        )

        # Single-shot fallback ALSO fails.
        async def _broken_single_shot(self, **kwargs):
            raise RuntimeError("single-shot generator exploded too (test)")

        monkeypatch.setattr(ChapterGenerator, "generate", _broken_single_shot)

        # Best-effort post-save hooks would attempt real LLM calls; no-op them.
        async def _noop_dispatch(*args, **kwargs):
            return None

        async def _noop_summarize(*args, **kwargs):
            return False, ""

        async def _noop_backfill(*args, **kwargs):
            return None

        monkeypatch.setattr(entity_dispatch_mod, "dispatch_for_chapter", _noop_dispatch)
        monkeypatch.setattr(
            summarizer_mod, "summarize_and_save_chapter", _noop_summarize
        )
        monkeypatch.setattr(
            memory_mod, "backfill_prev_volume_summary", _noop_backfill
        )

        resp = await auth_client.post(
            "/api/generate/chapter",
            json={
                "project_id": project_id,
                "chapter_id": chapter_id,
                "volume_id": volume_id,
                "chapter_idx": 1,
                "use_scene_mode": True,
                "auto_revise": True,  # must be skipped for a partial draft
                "skip_polish": False,
            },
        )
        assert resp.status_code == 200, resp.text
        events = _parse_sse_events(resp.text)
        expected_partial = SCENE1_TEXT + "\n\n" + SCENE2_FRAGMENT

        # fallback_restart still fires (client buffer-reset contract), and the
        # preserved partial is resent as a text frame AFTER it.
        kinds = [
            ("restart",) if e.get("event") == "fallback_restart" else ("text", e["text"])
            for e in events
            if e.get("event") == "fallback_restart" or "text" in e
        ]
        assert ("restart",) in kinds, events
        assert kinds.index(("text", expected_partial)) > kinds.index(("restart",))

        # The save is announced both as a normal saved frame and a dedicated
        # partial_saved event carrying draft status.
        saved = [e for e in events if e.get("status") == "saved"]
        assert saved and saved[0]["chapter_id"] == chapter_id, events
        partial_events = [e for e in events if e.get("event") == "partial_saved"]
        assert partial_events, f"missing partial_saved event: {events}"
        assert partial_events[0]["status"] == "draft"
        assert partial_events[0]["chapter_id"] == chapter_id

        # No auto-revise ran on the partial, and the stream terminated.
        assert not any(e.get("event") == "evaluating" for e in events), events
        assert resp.text.rstrip().endswith("data: [DONE]"), resp.text[-200:]

        # DB: the partial is the chapter draft.
        r = await auth_client.get(
            f"/api/projects/{project_id}/chapters/{chapter_id}"
        )
        assert r.status_code == 200, r.text
        row = r.json()
        assert row["status"] == "draft"
        assert row["content_text"] == expected_partial
    finally:
        await auth_client.delete(f"/api/projects/{project_id}?purge=true")


@pytest.mark.asyncio
async def test_trivial_partial_still_surfaces_error_when_both_paths_fail(
    auth_client, monkeypatch
):
    """Below the preservation threshold the old contract holds: error + [DONE],
    nothing persisted."""
    import app.api.generate as gen_mod
    from app.services.chapter_generator import ChapterGenerator
    from app.services.scene_orchestrator import SceneOrchestrator

    resp = await auth_client.post(
        "/api/projects", json={"title": "琐碎partial不保存测试", "genre": "测试"}
    )
    assert resp.status_code == 201, resp.text
    project_id = resp.json()["id"]

    try:
        class _Ready:
            ready = True

        async def _fake_readiness(*args, **kwargs):
            return _Ready()

        monkeypatch.setattr(gen_mod, "build_outline_readiness_report", _fake_readiness)

        def _broken_scene_stream(self, **kwargs):
            async def _gen():
                yield "只有几个字。"  # < _PARTIAL_SCENE_MIN_CHARS
                raise RuntimeError("scene_writer exploded (test)")

            return _gen()

        monkeypatch.setattr(
            SceneOrchestrator, "orchestrate_chapter_stream", _broken_scene_stream
        )

        async def _broken_single_shot(self, **kwargs):
            raise RuntimeError("single-shot exploded too (test)")

        monkeypatch.setattr(ChapterGenerator, "generate", _broken_single_shot)

        resp = await auth_client.post(
            "/api/generate/chapter",
            json={
                "project_id": project_id,
                "volume_id": str(uuid.uuid4()),
                "chapter_idx": 1,
                "use_scene_mode": True,
                "auto_revise": False,
                "skip_polish": True,
            },
        )
        assert resp.status_code == 200, resp.text
        events = _parse_sse_events(resp.text)
        assert any("error" in e for e in events), events
        assert not any(e.get("event") == "partial_saved" for e in events), events
        assert resp.text.rstrip().endswith("data: [DONE]"), resp.text[-200:]
    finally:
        await auth_client.delete(f"/api/projects/{project_id}")


# ---------------------------------------------------------------------------
# Pre-stream "Project not found" response must carry the [DONE] sentinel
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_project_not_found_emits_done_sentinel(auth_client):
    resp = await auth_client.post(
        "/api/generate/chapter",
        json={
            "project_id": str(uuid.uuid4()),
            "volume_id": str(uuid.uuid4()),
            "chapter_idx": 1,
        },
    )
    assert resp.status_code == 200, resp.text
    events = _parse_sse_events(resp.text)
    assert any(e.get("error") == "Project not found" for e in events), events
    assert resp.text.rstrip().endswith("data: [DONE]"), resp.text
