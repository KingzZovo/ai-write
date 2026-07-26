"""SSE chapter-stream hardening: heartbeat pings, scene events, [DONE] on error.

Covers three defects in ``POST /api/generate/chapter``:

1. Heartbeat: long non-yielding phases (logic critic / drafter / evaluator)
   could go >600s with no bytes on the wire, tripping nginx
   ``proxy_read_timeout`` while the shielded save survived. The stream now
   emits SSE comment frames (``: ping``) via ``_yield_with_heartbeat``.
2. Scene events: ``_on_scene_start`` was a no-op, so clients could not tell
   "scene 3 in progress" from "hung". It now emits
   ``{"event": "scene", "scene_idx": N, "total": null, "title": "..."}``.
3. [DONE] sentinel: the top-level exception path emitted ``{"error": ...}``
   without the ``data: [DONE]`` terminator every other exit path sends,
   leaving clients hanging until their own timeout.
"""

import asyncio
import json
import uuid

import pytest

from app.api.generate import _yield_with_heartbeat


def _parse_sse_events(body: str) -> list[dict]:
    """Parse `data: {...}` SSE lines into dicts (skipping the [DONE] marker)."""
    events = []
    for line in body.splitlines():
        if line.startswith("data: ") and line != "data: [DONE]":
            events.append(json.loads(line[len("data: "):]))
    return events


# ---------------------------------------------------------------------------
# 1) Heartbeat helper (unit)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_heartbeat_pings_while_slow_coroutine_runs():
    async def slow():
        await asyncio.sleep(0.05)
        return "the-result"

    pings: list[str] = []
    result = None
    async for done, val in _yield_with_heartbeat(slow(), interval=0.01):
        if done:
            result = val
        else:
            pings.append(val)

    assert result == "the-result"
    assert len(pings) >= 1, "slow coroutine must produce at least one ping"
    # SSE comment frames only: never a data: line the frontend would parse.
    assert all(p == ": ping\n\n" for p in pings)


@pytest.mark.asyncio
async def test_heartbeat_fast_coroutine_yields_result_without_pings():
    async def fast():
        return 42

    frames = [frame async for frame in _yield_with_heartbeat(fast(), interval=5.0)]
    assert frames == [(True, 42)]


@pytest.mark.asyncio
async def test_heartbeat_reraises_coroutine_exception():
    async def boom():
        await asyncio.sleep(0.02)
        raise ValueError("llm exploded (test)")

    pings = 0
    with pytest.raises(ValueError, match="llm exploded"):
        async for done, _val in _yield_with_heartbeat(boom(), interval=0.005):
            if not done:
                pings += 1
    assert pings >= 1


@pytest.mark.asyncio
async def test_heartbeat_cancels_task_when_consumer_closes():
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def hang():
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    gen = _yield_with_heartbeat(hang(), interval=0.01)
    frame = await gen.__anext__()  # first ping proves the task is running
    assert frame == (False, ": ping\n\n")
    assert started.is_set()
    await gen.aclose()  # client disconnect -> generator closed
    await asyncio.wait_for(cancelled.wait(), timeout=1)


# ---------------------------------------------------------------------------
# 2) Scene events + 3) [DONE] on error (endpoint-level)
# ---------------------------------------------------------------------------

class _FakeScene:
    def __init__(self, idx: int, title: str):
        self.idx = idx
        self.title = title


@pytest.mark.asyncio
async def test_scene_events_emitted_per_scene(auth_client, monkeypatch):
    """on_scene_start frames reach the client, each before its scene's text."""
    import app.api.generate as gen_mod
    from app.services.scene_orchestrator import SceneOrchestrator

    resp = await auth_client.post(
        "/api/projects", json={"title": "SSE场景事件测试项目", "genre": "测试"}
    )
    assert resp.status_code == 201, resp.text
    project_id = resp.json()["id"]

    try:
        class _Ready:
            ready = True

        async def _fake_readiness(*args, **kwargs):
            return _Ready()

        monkeypatch.setattr(gen_mod, "build_outline_readiness_report", _fake_readiness)

        def _fake_stream(self, **kwargs):
            cb = kwargs["on_scene_start"]

            async def _gen():
                await cb(_FakeScene(1, "开端"))
                yield "第一场文本。"
                await cb(_FakeScene(2, "转折"))
                yield "第二场文本。"

            return _gen()

        monkeypatch.setattr(
            SceneOrchestrator, "orchestrate_chapter_stream", _fake_stream
        )

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

        scene_events = [e for e in events if e.get("event") == "scene"]
        assert scene_events == [
            {"event": "scene", "scene_idx": 1, "total": None, "title": "开端"},
            {"event": "scene", "scene_idx": 2, "total": None, "title": "转折"},
        ]

        # Ordering: each scene frame precedes its own scene's text chunk.
        kinds = [
            ("scene", e["scene_idx"]) if e.get("event") == "scene"
            else ("text", e["text"])
            for e in events
            if e.get("event") == "scene" or "text" in e
        ]
        assert kinds.index(("scene", 1)) < kinds.index(("text", "第一场文本。"))
        assert kinds.index(("text", "第一场文本。")) < kinds.index(("scene", 2))
        assert kinds.index(("scene", 2)) < kinds.index(("text", "第二场文本。"))

        assert "data: [DONE]" in resp.text
    finally:
        await auth_client.delete(f"/api/projects/{project_id}")


@pytest.mark.asyncio
async def test_top_level_error_path_emits_done_sentinel(auth_client, monkeypatch):
    """An exception inside event_stream must still terminate with [DONE]."""
    import app.api.generate as gen_mod

    resp = await auth_client.post(
        "/api/projects", json={"title": "SSE错误终止测试项目", "genre": "测试"}
    )
    assert resp.status_code == 201, resp.text
    project_id = resp.json()["id"]

    try:
        async def _boom(*args, **kwargs):
            raise RuntimeError("readiness exploded (test)")

        monkeypatch.setattr(gen_mod, "build_outline_readiness_report", _boom)

        resp = await auth_client.post(
            "/api/generate/chapter",
            json={
                "project_id": project_id,
                "volume_id": str(uuid.uuid4()),
                "chapter_idx": 1,
            },
        )
        assert resp.status_code == 200, resp.text
        events = _parse_sse_events(resp.text)
        assert any("error" in e for e in events), events
        # The terminator must be the final frame on the wire.
        assert resp.text.rstrip().endswith("data: [DONE]"), resp.text[-200:]
    finally:
        await auth_client.delete(f"/api/projects/{project_id}")
