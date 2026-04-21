"""Tests for v0.5 model additions."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest


def test_prompt_asset_has_v05_fields():
    from app.models.prompt import PromptAsset

    asset = PromptAsset(
        task_type="generation",
        name="test",
        system_prompt="x",
        endpoint_id=None,
        model_name="",
        temperature=0.8,
        max_tokens=2048,
        category="Core Writing",
        order=10,
        always_enabled=0,
        name_en="Test",
        description_en="",
    )
    assert asset.temperature == 0.8
    assert asset.category == "Core Writing"
    assert asset.always_enabled == 0


def test_llm_call_log_construction():
    from app.models.call_log import LLMCallLog

    log = LLMCallLog(
        task_type="generation",
        messages_json=[{"role": "user", "content": "x"}],
        rag_hits_json=[
            {"collection": "chapter_summaries", "score": 0.5, "payload": {"summary": "y"}}
        ],
        response_text="ok",
        input_tokens=10,
        output_tokens=5,
        latency_ms=123,
        model="claude-sonnet-4",
        status="ok",
    )
    assert log.status == "ok"
    assert log.rag_hits_json[0]["collection"] == "chapter_summaries"
    assert log.input_tokens == 10

