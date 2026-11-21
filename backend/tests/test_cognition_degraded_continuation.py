"""回归:认知提取是**软**失败时,不许把整本书停住。

实测(2026-11-14 / 11-16,三次):某一章认知提取连续 3 次判"依据无法在正文逐字定位"
→ 该章 cognition 落成 `skipped` → `cognition_reader` 判定上一章未就绪
→ **之后每一章都被 `character_cognition_not_ready` 拦住**,重试也没用。

默认行为不变(未就绪即阻断)。打开 `ALLOW_COGNITION_DEGRADED_CONTINUATION` 后:
那一章按"无认知变化"计入,整本书可以继续;凭据不符、作者决定被改这类**真实损坏**
仍然照旧抛错,不放进降级通道。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config import settings
from app.services.cognition_reader import effective_change_payload
from app.services.context_errors import RequiredContextUnavailable


def _source():
    return SimpleNamespace(chapter_id="c1", source_hash="h1", receipt={"title": "第一章"})


def _record(*, status, mode="auto", changes=()):
    from app.services.entity_sources import digest

    return SimpleNamespace(
        id="r1", project_id="p1", chapter_id="c1", status=status, source_mode=mode,
        source_hash="h1", source_json={"title": "第一章"}, changes_json=list(changes),
        payload_hash=digest(["h1", list(changes)]), author_revision_id=None,
    )


def test_missing_record_still_blocks_by_default(monkeypatch):
    monkeypatch.setattr(settings, "ALLOW_COGNITION_DEGRADED_CONTINUATION", False, raising=False)
    with pytest.raises(RequiredContextUnavailable, match="character_cognition_not_ready"):
        effective_change_payload(None, _source())


def test_not_completed_record_still_blocks_by_default(monkeypatch):
    monkeypatch.setattr(settings, "ALLOW_COGNITION_DEGRADED_CONTINUATION", False, raising=False)
    with pytest.raises(RequiredContextUnavailable, match="character_cognition_not_ready"):
        effective_change_payload(_record(status="skipped"), _source())


def test_missing_record_degrades_when_enabled(monkeypatch):
    monkeypatch.setattr(settings, "ALLOW_COGNITION_DEGRADED_CONTINUATION", True, raising=False)
    assert effective_change_payload(None, _source()) == []


@pytest.mark.parametrize("status", ["skipped", "failed", "pending"])
def test_unfinished_record_degrades_when_enabled(monkeypatch, status):
    monkeypatch.setattr(settings, "ALLOW_COGNITION_DEGRADED_CONTINUATION", True, raising=False)
    assert effective_change_payload(_record(status=status), _source()) == []


def test_completed_record_is_unaffected(monkeypatch):
    monkeypatch.setattr(settings, "ALLOW_COGNITION_DEGRADED_CONTINUATION", True, raising=False)
    changes = [{"character": "周芸", "learns": "钱不够", "evidence": "信封压在碗下"}]
    record = _record(status="completed", changes=changes)
    assert effective_change_payload(record, _source()) == changes


def test_receipt_mismatch_on_completed_record_is_not_degraded(monkeypatch):
    """凭据不符是真实损坏,不是"提取没成功",必须照旧抛错。"""
    monkeypatch.setattr(settings, "ALLOW_COGNITION_DEGRADED_CONTINUATION", True, raising=False)
    source = _source()
    source.receipt = {"title": "改过的章"}          # 与 record.source_json 不符
    with pytest.raises(RequiredContextUnavailable, match="character_cognition_author_decision_invalid"):
        effective_change_payload(_record(status="completed"), source)