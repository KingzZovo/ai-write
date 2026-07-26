"""Style/structure distillation pipeline — signal-flow regression suite.

Covers the 2026-07-26 audit fixes:
  1. Style threading: the [风格要求] block reaches BOTH ChapterGenerator's
     and SceneOrchestrator's writer prompts (fake router capture).
  2. Binding chain: settings' style_profile_id loads the profile directly by
     id (regardless of bind_level) and derives reference_book_id from a
     book-bound profile.
  3. Filtered recall: _v2_three_way_recall passes the resolved book_id to
     Qdrant searches; recall lines land in pack.style_recall (own L3 render
     slot), never in rag_snippets where [:5] truncated them away.
  4. Compiler: keeps chapter-hook/structure rules (blacklist removed), caps
     raised to rules[:16] / 160 chars, generalized proper-noun scrub,
     include_samples=False strips raw few-shot.
  5. flag_modified: structure re-extraction persists metadata_json.
  6. Beat sketch: stratified evenly-spaced sampling capped at 400 lines.
  7. Structure proper-noun scrub from reference book metadata.
  8. Dossier contract: dossier['style_block'] preferred over compiled style.
  9. test-write endpoint runs the production-equivalent prompt.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.context_pack import ContextPack, ContextPackBuilder
from app.services.style_compiler import compile_style
from app.services.style_runtime import (
    STYLE_INJECTION_MAX_CHARS,
    build_style_injection_block,
    collect_reference_proper_nouns,
    derive_reference_book_id,
    get_dossier_block,
    production_style_text_for_profile,
    resolve_active_profile,
    resolve_reference_book_id,
    scrub_reference_proper_nouns,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeDB:
    """Minimal AsyncSession stand-in: get() by (model-name, pk), scripted execute()."""

    def __init__(self, objects: dict | None = None, execute_results: list | None = None):
        self._objects = objects or {}
        self._execute_results = list(execute_results or [])
        self.flush = AsyncMock()

    async def get(self, model, pk):
        return self._objects.get((model.__name__, str(pk)))

    async def execute(self, stmt):
        if self._execute_results:
            return self._execute_results.pop(0)
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=None)
        return result


def _scalar_result(value):
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=value)
    return result


def _make_profile(**overrides):
    base = dict(
        name="测试风格",
        source_book="参考书甲",
        source_book_id=None,
        rules_json=[{"rule": "短句为主，多留白", "weight": 0.9, "category": "rhythm"}],
        anti_ai_rules=[],
        tone_keywords=[],
        sample_passages=[],
        config_json={},
        bind_level="global",
        bind_target_id=None,
        is_active=1,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class _FakePack:
    """Minimal ContextPack stand-in for generator prompt-capture tests."""

    def __init__(self) -> None:
        self.character_cards: list = []
        self.rag_snippets: list = []
        self.dialogue_samples: dict = {}
        self.style_samples: list = []
        self.style_recall: list = []

    def to_system_prompt(self) -> str:
        return "<背景>"

    def to_messages(self, user_instruction: str = "") -> list[dict]:
        msgs = [{"role": "system", "content": self.to_system_prompt()}]
        if user_instruction:
            msgs.append({"role": "user", "content": user_instruction})
        return msgs


# ---------------------------------------------------------------------------
# 1. Style threading into both writer prompts
# ---------------------------------------------------------------------------


def test_build_style_injection_block_format_and_cap():
    assert build_style_injection_block("") == ""
    assert build_style_injection_block("  ") == ""
    block = build_style_injection_block("短句为主")
    assert block == "[风格要求] 短句为主"
    long_text = "风" * 5000
    capped = build_style_injection_block(long_text)
    assert capped.startswith("[风格要求] ")
    assert len(capped) == len("[风格要求] ") + STYLE_INJECTION_MAX_CHARS


@pytest.mark.asyncio
async def test_style_block_reaches_chapter_generator_prompt():
    from app.services.chapter_generator import ChapterGenerator

    captured: dict = {}

    async def fake_run_text_prompt(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(text="")

    fake_builder = MagicMock()
    fake_builder.build = AsyncMock(return_value=_FakePack())

    instr = "写第1章。" + "\n\n" + build_style_injection_block("短句为主，多留白")
    with patch(
        "app.services.chapter_generator.ContextPackBuilder",
        MagicMock(return_value=fake_builder),
    ), patch(
        "app.services.chapter_generator.run_text_prompt", fake_run_text_prompt
    ), patch.object(
        ChapterGenerator, "_assert_outline_chain_ready", AsyncMock()
    ):
        text = await ChapterGenerator().generate(
            project_id="p1",
            volume_id="v1",
            chapter_idx=1,
            db=MagicMock(),
            user_instruction=instr,
        )

    assert text == ""
    user_msgs = [m for m in captured["messages"] if m["role"] == "user"]
    assert any("[风格要求] 短句为主，多留白" in m["content"] for m in user_msgs)


@pytest.mark.asyncio
async def test_style_block_reaches_scene_writer_prompt():
    from app.services.scene_orchestrator import SceneBrief, SceneOrchestrator

    captured: dict = {}

    async def fake_stream_text_prompt(**kwargs):
        captured.update(kwargs)
        yield "文"

    instr = "写作。" + "\n\n" + build_style_injection_block("风格X：冷峻克制")
    with patch(
        "app.services.scene_orchestrator.stream_text_prompt", fake_stream_text_prompt
    ):
        orch = SceneOrchestrator()
        scene = SceneBrief(idx=1, title="开场", brief="推进")
        chunks = [
            c
            async for c in orch.write_scene_stream(
                scene=scene,
                pack=_FakePack(),
                prior_scenes_summary="",
                db=MagicMock(),
                project_id="p1",
                chapter_id=None,
                user_instruction=instr,
            )
        ]
    assert chunks == ["文"]
    assert "[风格要求] 风格X：冷峻克制" in captured["user_content"]


# ---------------------------------------------------------------------------
# 2. Binding chain resolution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_settings_profile_id_resolves_book_bound_profile_directly():
    """Live profiles bind to reference-book ids; the settings-declared id must
    load the profile directly, regardless of bind_level/bind_target_id."""
    book_id = str(uuid.uuid4())
    profile = _make_profile(bind_level="book", bind_target_id=book_id)
    project = SimpleNamespace(settings_json={"style_profile_id": "prof-1"})
    db = _FakeDB(objects={
        ("Project", "proj-1"): project,
        ("StyleProfile", "prof-1"): profile,
    })
    resolved = await resolve_active_profile(db, "proj-1")
    assert resolved is profile


@pytest.mark.asyncio
async def test_bind_level_fallback_still_works_without_settings_id():
    """Without a settings id, the chain falls through to global binding."""
    global_profile = _make_profile(bind_level="global")
    project = SimpleNamespace(settings_json={})
    db = _FakeDB(
        objects={("Project", "proj-1"): project},
        execute_results=[
            _scalar_result(None),           # project-level bind miss
            _scalar_result(global_profile), # global hit
        ],
    )
    resolved = await resolve_active_profile(db, "proj-1")
    assert resolved is global_profile


@pytest.mark.asyncio
async def test_derive_reference_book_id_from_book_bound_profile():
    book_id = str(uuid.uuid4())
    profile = _make_profile(bind_level="book", bind_target_id=book_id)
    book = SimpleNamespace(id=book_id, metadata_json={})
    db = _FakeDB(objects={("ReferenceBook", book_id): book})
    assert await derive_reference_book_id(db, profile) == book_id


@pytest.mark.asyncio
async def test_derive_reference_book_id_rejects_non_book_target():
    """Legacy project-bound profiles reuse bind_level='book' with a project
    id; that id must not be treated as a reference book."""
    profile = _make_profile(bind_level="book", bind_target_id=str(uuid.uuid4()))
    db = _FakeDB()  # no ReferenceBook rows
    assert await derive_reference_book_id(db, profile) is None


@pytest.mark.asyncio
async def test_resolve_reference_book_id_settings_key_wins():
    project = SimpleNamespace(
        settings_json={"style_reference": {"reference_book_id": "rb-7"}}
    )
    db = _FakeDB(objects={("Project", "proj-1"): project})
    assert await resolve_reference_book_id(db, "proj-1") == "rb-7"


@pytest.mark.asyncio
async def test_resolve_reference_book_id_derives_from_profile_binding():
    book_id = str(uuid.uuid4())
    profile = _make_profile(bind_level="book", bind_target_id=book_id)
    project = SimpleNamespace(settings_json={"style_profile_id": "prof-1"})
    book = SimpleNamespace(id=book_id, metadata_json={})
    db = _FakeDB(objects={
        ("Project", "proj-1"): project,
        ("StyleProfile", "prof-1"): profile,
        ("ReferenceBook", book_id): book,
    })
    assert await resolve_reference_book_id(db, "proj-1") == book_id


# ---------------------------------------------------------------------------
# 3. Filtered recall + L3 render slot
# ---------------------------------------------------------------------------


def _make_fake_store_cls(calls: list):
    class _FakeStore:
        def __init__(self, client):
            pass

        async def search_style_profiles(self, embedding, book_id=None, top_k=3):
            calls.append(("style_profiles", book_id))
            return [{"payload": {"profile": {"pov": "第三人称", "sentence_rhythm": "短促",
                                             "emotional_register": "克制", "vocab_tone": ["冷"]}}}]

        async def search_beat_sheets(self, embedding, book_id=None, top_k=2):
            calls.append(("beat_sheets", book_id))
            return [{"payload": {"beat": {"scene_type": "冲突", "reusable_pattern": "压迫",
                                          "outcome": "反击"}}}]

    return _FakeStore


@pytest.mark.asyncio
async def test_three_way_recall_passes_resolved_book_id_and_fills_style_recall():
    calls: list = []
    builder = ContextPackBuilder.__new__(ContextPackBuilder)
    book = SimpleNamespace(id="rb-1", metadata_json={})  # no dossier
    builder._db = _FakeDB(objects={("ReferenceBook", "rb-1"): book})
    builder._owns_db = False
    pack = ContextPack()

    with patch(
        "app.services.style_runtime.resolve_reference_book_id",
        AsyncMock(return_value="rb-1"),
    ), patch(
        "app.services.qdrant_store.QdrantStore", _make_fake_store_cls(calls)
    ):
        await builder._v2_three_way_recall(pack, [0.1], MagicMock(), "proj-1")

    assert ("style_profiles", "rb-1") in calls
    assert ("beat_sheets", "rb-1") in calls
    assert any(line.startswith("[风格]") for line in pack.style_recall)
    assert any(line.startswith("[骨架]") for line in pack.style_recall)
    # Own render slot: recall lines never compete in rag_snippets[:5]
    assert pack.rag_snippets == []


@pytest.mark.asyncio
async def test_three_way_recall_skips_when_no_book_resolved():
    calls: list = []
    builder = ContextPackBuilder.__new__(ContextPackBuilder)
    builder._db = _FakeDB()
    builder._owns_db = False
    pack = ContextPack()

    with patch(
        "app.services.style_runtime.resolve_reference_book_id",
        AsyncMock(return_value=None),
    ), patch(
        "app.services.qdrant_store.QdrantStore", _make_fake_store_cls(calls)
    ):
        await builder._v2_three_way_recall(pack, [0.1], MagicMock(), "proj-1")

    assert calls == []  # never searched unfiltered across all books
    assert pack.style_recall == []


@pytest.mark.asyncio
async def test_three_way_recall_prefers_dossier_style_block():
    calls: list = []
    builder = ContextPackBuilder.__new__(ContextPackBuilder)
    book = SimpleNamespace(
        id="rb-1",
        metadata_json={"dossier": {"style_block": "冷峻短句，白描为主。"}},
    )
    builder._db = _FakeDB(objects={("ReferenceBook", "rb-1"): book})
    builder._owns_db = False
    pack = ContextPack()

    with patch(
        "app.services.style_runtime.resolve_reference_book_id",
        AsyncMock(return_value="rb-1"),
    ), patch(
        "app.services.qdrant_store.QdrantStore", _make_fake_store_cls(calls)
    ):
        await builder._v2_three_way_recall(pack, [0.1], MagicMock(), "proj-1")

    assert "冷峻短句，白描为主。" in pack.style_recall
    assert ("style_profiles", "rb-1") not in calls  # dossier replaces vector style recall
    assert ("beat_sheets", "rb-1") in calls


def test_style_recall_renders_in_l3_style_block_despite_full_rag_snippets():
    """Pre-fix: recall lines were appended to rag_snippets AFTER 5 chapter
    summaries while rendering took rag_snippets[:5] — always truncated away."""
    pack = ContextPack()
    pack.rag_snippets = [f"第{i}章摘要" for i in range(1, 6)]  # fills all 5 slots
    pack.style_recall = ["[风格] pov=第三人称 节奏=短促", "[骨架] 冲突: 压迫 → 反击"]
    prompt = pack.to_system_prompt()
    assert "【风格参考】" in prompt
    assert "[风格] pov=第三人称 节奏=短促" in prompt
    assert "[骨架] 冲突: 压迫 → 反击" in prompt
    # chapter summaries still render in their own slot
    assert "第5章摘要" in prompt


# ---------------------------------------------------------------------------
# 4. Compiler: hook rules kept, caps expanded, scrub generalized
# ---------------------------------------------------------------------------


def test_compile_style_keeps_chapter_hook_and_structure_rules():
    profile = _make_profile(rules_json=[
        {"rule": "每卷以一个大事件收束，卷末留全局钩子", "category": "structure"},
        {"rule": "章节开场方式：以动作或对话切入，禁止环境铺陈开头", "category": "structure"},
        {"rule": "结构上每三章安排一次小高潮", "category": "structure"},
    ])
    compiled = compile_style(profile)
    assert "卷末留全局钩子" in compiled
    assert "开场方式" in compiled
    assert "每三章安排一次小高潮" in compiled


def test_compile_style_expanded_caps_16_rules_160_chars():
    long_rule = "长" * 200
    rules = [{"rule": f"规则{i}：节奏交替务必自然贯穿全章始终", "category": "rhythm"} for i in range(15)]
    rules.append({"rule": long_rule, "category": "rhythm"})
    rules.append({"rule": "规则十七：这条必须被截断掉", "category": "rhythm"})
    profile = _make_profile(rules_json=rules)
    compiled = compile_style(profile)
    assert "规则14" in compiled          # rule 15 of 16 survives (old cap was 8)
    assert "长" * 160 in compiled        # clipped at 160, not 80
    assert "长" * 161 not in compiled
    assert "规则十七" not in compiled    # 17th dropped by the [:16] cap


def test_compile_style_scrubs_profile_source_proper_nouns():
    profile = _make_profile(
        source_book="龙族前传",
        config_json={"source_character_names": ["西泽尔", "密涅瓦"]},
        rules_json=[
            {"rule": "像西泽尔那样以冷峻视角推进，密涅瓦式的独白收尾", "category": "style"},
            {"rule": "路明非式自嘲缓和紧张节奏，保持市井口吻", "category": "style"},  # legacy list
        ],
    )
    compiled = compile_style(profile)
    assert "西泽尔" not in compiled
    assert "密涅瓦" not in compiled
    assert "路明非" not in compiled  # hardcoded legacy entries kept
    assert "冷峻视角推进" in compiled


def test_compile_style_include_samples_false_strips_few_shot():
    profile = _make_profile(sample_passages=["这是原文片段，绝不能进入生产提示词。"])
    with_samples = compile_style(profile)
    without = compile_style(profile, include_samples=False)
    assert "风格参考样本" in with_samples
    assert "风格参考样本" not in without
    assert "这是原文片段" not in without


# ---------------------------------------------------------------------------
# 5. flag_modified persists structure re-extraction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extract_book_structure_flags_metadata_modified():
    from sqlalchemy.orm import attributes as sa_attributes
    from app.api.styles import extract_book_structure
    from app.models.project import ReferenceBook

    book = ReferenceBook(title="参考书", metadata_json={"plot_structure": {"old": True}})
    original_meta = book.metadata_json
    book_id = uuid.uuid4()

    chunks = [SimpleNamespace(content=f"章节内容{i}") for i in range(6)]
    scalars = MagicMock()
    scalars.all = MagicMock(return_value=chunks)
    chunk_result = MagicMock()
    chunk_result.scalars = MagicMock(return_value=scalars)

    db = _FakeDB(
        objects={("ReferenceBook", str(book_id)): book},
        execute_results=[chunk_result],
    )

    flag_calls: list = []
    real_flag_modified = sa_attributes.flag_modified

    def spy_flag_modified(instance, key):
        flag_calls.append((instance, key))
        return real_flag_modified(instance, key)

    with patch(
        "app.services.plot_structure.extract_plot_structure",
        AsyncMock(return_value={"arc_pattern": "三幕式"}),
    ), patch.object(sa_attributes, "flag_modified", spy_flag_modified):
        result = await extract_book_structure(book_id, db)

    assert result["structure"] == {"arc_pattern": "三幕式"}
    assert (book, "metadata_json") in flag_calls
    # dict must be copied, not mutated in place (JSON column no-op trap)
    assert book.metadata_json is not original_meta
    assert book.metadata_json["plot_structure"] == {"arc_pattern": "三幕式"}


@pytest.mark.asyncio
async def test_extract_structure_by_author_flags_metadata_modified():
    from sqlalchemy.orm import attributes as sa_attributes
    from app.api.styles import extract_structure_by_author
    from app.models.project import ReferenceBook

    book = ReferenceBook(title="书一", author="作者甲", metadata_json={})
    book.id = uuid.uuid4()

    books_scalars = MagicMock()
    books_scalars.all = MagicMock(return_value=[book])
    books_result = MagicMock()
    books_result.scalars = MagicMock(return_value=books_scalars)

    chunks = [SimpleNamespace(content=f"内容{i}") for i in range(4)]
    chunk_scalars = MagicMock()
    chunk_scalars.all = MagicMock(return_value=chunks)
    chunk_result = MagicMock()
    chunk_result.scalars = MagicMock(return_value=chunk_scalars)

    db = _FakeDB(execute_results=[books_result, chunk_result])

    flag_calls: list = []
    real_flag_modified = sa_attributes.flag_modified

    def spy_flag_modified(instance, key):
        flag_calls.append((instance, key))
        return real_flag_modified(instance, key)

    with patch(
        "app.services.plot_structure.extract_plot_structure",
        AsyncMock(return_value={"arc_pattern": "双线"}),
    ), patch.object(sa_attributes, "flag_modified", spy_flag_modified):
        result = await extract_structure_by_author("作者甲", db)

    assert result["structure"] == {"arc_pattern": "双线"}
    assert (book, "metadata_json") in flag_calls
    assert book.metadata_json["author_structure"] == {"arc_pattern": "双线"}


# ---------------------------------------------------------------------------
# 6. Beat sketch stratified sampling
# ---------------------------------------------------------------------------


def test_stratified_sample_caps_and_preserves_order():
    from app.services.outline_from_reference import MAX_BEAT_SKETCH_LINES, _stratified_sample

    lines = [f"line-{i}" for i in range(27000)]
    sampled = _stratified_sample(lines)
    assert len(sampled) <= MAX_BEAT_SKETCH_LINES
    assert sampled[0] == "line-0"
    assert sampled[-1] == "line-26999"
    idxs = [int(s.split("-")[1]) for s in sampled]
    assert idxs == sorted(idxs)  # chapter order preserved
    # evenly spaced: gaps within 1 of each other
    gaps = [b - a for a, b in zip(idxs, idxs[1:])]
    assert max(gaps) - min(gaps) <= 1


def test_stratified_sample_passthrough_when_small():
    from app.services.outline_from_reference import _stratified_sample

    lines = [f"l{i}" for i in range(50)]
    assert _stratified_sample(lines) == lines


@pytest.mark.asyncio
async def test_load_beat_sketch_bounded():
    from app.services.outline_from_reference import MAX_BEAT_SKETCH_LINES, _load_beat_sketch

    rows = []
    for i in range(1000):
        card = SimpleNamespace(beat_json={"scene_type": "冲突", "outcome": "推进",
                                          "reusable_pattern": f"模式{i}"})
        slc = SimpleNamespace(chapter_idx=i // 10, sequence_id=i)
        rows.append((card, slc))
    rows_result = MagicMock()
    rows_result.all = MagicMock(return_value=rows)
    db = _FakeDB(execute_results=[rows_result])

    sketch = await _load_beat_sketch("book-1", db)
    line_count = sketch.count("\n") + 1
    assert line_count <= MAX_BEAT_SKETCH_LINES
    assert "[ch0/0]" in sketch          # start of range kept
    assert "模式999" in sketch          # end of range kept


# ---------------------------------------------------------------------------
# 7. Structure proper-noun scrub
# ---------------------------------------------------------------------------


def test_scrub_reference_proper_nouns_uses_metadata_names():
    book = SimpleNamespace(
        id="rb-1",
        title="龙族",
        metadata_json={
            "characters": [{"name": "西泽尔"}, "密涅瓦"],
            "dossier": {"world_data": {"character_names": ["昂热"]}},
        },
    )
    text = "开局方式：西泽尔式冷开场；密涅瓦担任引导者；昂热坐镇；参考《龙族》节奏。"
    scrubbed = scrub_reference_proper_nouns(text, book)
    assert "西泽尔" not in scrubbed
    assert "密涅瓦" not in scrubbed
    assert "昂热" not in scrubbed
    assert "龙族" not in scrubbed
    assert "冷开场" in scrubbed


def test_scrub_reference_proper_nouns_no_names_leaves_text(caplog):
    book = SimpleNamespace(id="rb-2", title="", metadata_json={})
    text = "开局方式：主角冷开场。"
    with caplog.at_level("WARNING"):
        assert scrub_reference_proper_nouns(text, book) == text
    assert any("unscrubbed" in r.message for r in caplog.records)


def test_collect_reference_proper_nouns_shapes():
    book = SimpleNamespace(
        title="《书名》",
        metadata_json={"characters": ["甲乙", {"name": "丙丁"}, "x"]},  # "x" too short
    )
    nouns = collect_reference_proper_nouns(book)
    assert "书名" in nouns
    assert "甲乙" in nouns and "丙丁" in nouns
    assert "x" not in nouns


# ---------------------------------------------------------------------------
# 8. Dossier preference over compiled fallback
# ---------------------------------------------------------------------------


def test_get_dossier_block_contract():
    book = SimpleNamespace(metadata_json={"dossier": {
        "style_block": "风格摘要", "structure_block": "结构摘要"}})
    assert get_dossier_block(book, "style_block") == "风格摘要"
    assert get_dossier_block(book, "structure_block") == "结构摘要"
    assert get_dossier_block(book, "world_block") == ""
    assert get_dossier_block(SimpleNamespace(metadata_json={}), "style_block") == ""
    assert get_dossier_block(SimpleNamespace(metadata_json=None), "style_block") == ""


@pytest.mark.asyncio
async def test_production_style_text_prefers_dossier():
    book_id = str(uuid.uuid4())
    profile = _make_profile(bind_level="book", bind_target_id=book_id)
    book = SimpleNamespace(
        id=book_id,
        metadata_json={"dossier": {"style_block": "冷峻白描，短句推进。"}},
    )
    db = _FakeDB(objects={("ReferenceBook", book_id): book})
    text, source, ref_id = await production_style_text_for_profile(db, profile)
    assert source == "dossier"
    assert text == "冷峻白描，短句推进。"
    assert ref_id == book_id


@pytest.mark.asyncio
async def test_production_style_text_falls_back_to_compiled_without_dossier():
    book_id = str(uuid.uuid4())
    profile = _make_profile(
        bind_level="book",
        bind_target_id=book_id,
        sample_passages=["原文片段不得进入生产提示词"],
    )
    book = SimpleNamespace(id=book_id, metadata_json={})
    db = _FakeDB(objects={("ReferenceBook", book_id): book})
    text, source, ref_id = await production_style_text_for_profile(db, profile)
    assert source == "compiled"
    assert "写作风格参考：测试风格" in text
    assert "原文片段不得进入生产提示词" not in text  # include_samples=False
    assert ref_id == book_id


# ---------------------------------------------------------------------------
# 9. test-write endpoint runs the production-equivalent prompt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_test_write_uses_production_equivalent_block():
    from app.api.styles import TestWriteRequest, test_write
    from app.models.project import StyleProfile

    profile = StyleProfile(
        name="测试风格",
        rules_json=[{"rule": "短句为主，多留白收束", "category": "rhythm"}],
        sample_passages=["原文片段，不得进入生产提示词。"],
    )
    profile.bind_level = "global"
    style_id = uuid.uuid4()
    db = _FakeDB(objects={("StyleProfile", str(style_id)): profile})

    captured: dict = {}

    class _FakeRouter:
        async def generate(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(text="雨巷。")

    with patch(
        "app.services.model_router.get_model_router",
        MagicMock(return_value=_FakeRouter()),
    ):
        resp = await test_write(style_id, TestWriteRequest(prompt="写雨巷"), db)

    assert resp.mode == "production_equivalent"
    assert resp.style_source == "compiled"
    assert resp.style_block_chars > 0
    user_content = [m for m in captured["messages"] if m["role"] == "user"][0]["content"]
    assert "[风格要求] " in user_content
    assert "短句为主，多留白收束" in user_content
    assert "原文片段" not in user_content  # production strips sample few-shot
