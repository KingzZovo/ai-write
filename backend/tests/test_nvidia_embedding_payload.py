"""回归:NVIDIA embeddings 的批量请求必须给逐条 modality。

实测(2026-11-14,真实链连续章第 4 章):

    NVIDIA embeddings HTTP 400:
    {"error":"Your request cannot be validated, it is incorrect.
     detail=Modality must be a list of the same length as input if input is a list."}

`NvidiaEmbeddingProvider._post` 无论输入几条都发 ``modality=["text"]``,于是
**批量**向量化全部 400:认知索引按 32 条一批 passages,语义召回因此不可用,表现为
第 4 章起 `character_cognition_retrieval_unavailable`;而单条调用正常,所以只在
有上下文积累之后才暴露。

对同一个官方端点直接验证过:2 条输入 + ``["text"]`` → 400;2 条 + ``["text","text"]``
→ 200 且返回 2 个 2048 维向量。
"""
from __future__ import annotations

import pytest

from app.services.model_router import nvidia_embedding_payload

KW = dict(model="nvidia/llama-nemotron-embed-vl-1b-v2", input_type="passage",
          encoding_format="float", truncate="NONE")


@pytest.mark.parametrize("count", [1, 2, 5, 32])
def test_modality_matches_input_length(count):
    inputs = [f"第{i}段" for i in range(count)]
    payload = nvidia_embedding_payload(inputs=inputs, modality=["text"], **KW)
    assert len(payload["modality"]) == count
    assert set(payload["modality"]) == {"text"}
    assert len(payload["input"]) == count


def test_single_element_modality_is_still_correct_for_single_input():
    payload = nvidia_embedding_payload(inputs=["一段"], modality=["text"], **KW)
    assert payload["modality"] == ["text"]


def test_explicit_per_input_modality_is_preserved():
    payload = nvidia_embedding_payload(inputs=["图", "文"], modality=["image", "text"], **KW)
    assert payload["modality"] == ["image", "text"]


def test_missing_or_blank_modality_falls_back_to_text():
    assert nvidia_embedding_payload(inputs=["a", "b"], modality=None, **KW)["modality"] == ["text", "text"]
    assert nvidia_embedding_payload(inputs=["a"], modality=["", "  "], **KW)["modality"] == ["text"]


def test_payload_keeps_other_fields_untouched():
    payload = nvidia_embedding_payload(inputs=["a"], modality=["text"], **KW)
    assert payload["model"] == KW["model"]
    assert payload["input_type"] == "passage"
    assert payload["encoding_format"] == "float"
    assert payload["truncate"] == "NONE"
