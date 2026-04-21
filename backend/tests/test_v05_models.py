"""Tests for v0.5 model additions."""
import uuid

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
