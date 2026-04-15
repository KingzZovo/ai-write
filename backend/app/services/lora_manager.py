"""
LoRA Fine-tuning Manager

Manages the lifecycle of LoRA adapters for style-specific model fine-tuning:
1. Training data export (from existing TextChunks + PlotFeatures)
2. Training job management (via Unsloth/LLaMA-Factory)
3. Adapter loading and switching at inference time

Hardware target: RTX 5080 (16GB) with QLoRA 4-bit quantization
Recommended base model: Qwen2.5-7B

Training data format: Alpaca JSON
[
  {
    "instruction": "根据以下大纲和上下文，以[风格名]的写作风格生成小说正文",
    "input": "大纲：...\\n上下文：...",
    "output": "实际小说原文（来自参考书）"
  }
]
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Default paths
LORA_BASE_DIR = Path("/root/ai-write/lora")
TRAINING_DATA_DIR = LORA_BASE_DIR / "datasets"
ADAPTER_DIR = LORA_BASE_DIR / "adapters"
LOGS_DIR = LORA_BASE_DIR / "logs"

# QLoRA config for RTX 5080 (16GB)
DEFAULT_TRAINING_CONFIG = {
    "base_model": "Qwen/Qwen2.5-7B",
    "quantization": "4bit",  # QLoRA
    "lora_rank": 64,
    "lora_alpha": 128,
    "lora_dropout": 0.05,
    "target_modules": ["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
    "learning_rate": 2e-4,
    "num_epochs": 3,
    "per_device_batch_size": 2,
    "gradient_accumulation_steps": 4,
    "max_seq_length": 4096,
    "warmup_ratio": 0.1,
    "optimizer": "adamw_8bit",
    "fp16": True,
    "logging_steps": 10,
    "save_steps": 100,
    "estimated_vram_gb": 14,  # Fits in 16GB RTX 5080
}


@dataclass
class TrainingDataset:
    """A prepared training dataset for LoRA fine-tuning."""
    name: str
    book_ids: list[str]
    style_name: str
    sample_count: int = 0
    file_path: str = ""
    format: str = "alpaca"  # alpaca, sharegpt


@dataclass
class LoRAAdapter:
    """A trained LoRA adapter."""
    id: str
    name: str
    base_model: str
    style_name: str
    training_config: dict = field(default_factory=dict)
    adapter_path: str = ""
    status: str = "pending"  # pending, training, ready, error
    metrics: dict = field(default_factory=dict)  # loss, eval metrics


@dataclass
class TrainingSample:
    """A single training sample in Alpaca format."""
    instruction: str
    input_text: str
    output: str

    def to_dict(self) -> dict:
        return {
            "instruction": self.instruction,
            "input": self.input_text,
            "output": self.output,
        }


class LoRADataExporter:
    """
    Exports existing knowledge base data into LoRA training format.

    Uses:
    - TextChunks: Original novel text → training "output"
    - PlotFeatures: Chapter summaries → training "input" (as context/outline)
    - StyleProfile: Style name → training "instruction"
    """

    async def export_training_data(
        self,
        book_ids: list[str],
        style_name: str,
        output_path: str | None = None,
        max_samples: int = 5000,
        min_chunk_chars: int = 200,
    ) -> TrainingDataset:
        """
        Export training data from reference books.

        Strategy:
        For each text chunk, create a training sample where:
        - instruction = "以{style_name}的风格，根据以下情节要点生成小说正文"
        - input = chapter summary / plot points (from PlotFeatures or previous chunk)
        - output = actual chunk text (the ground truth)

        This teaches the model to generate text in the target style
        given plot instructions.
        """
        from sqlalchemy import select
        from app.db.session import async_session_factory
        from app.models.project import TextChunk, ReferenceBook

        dataset = TrainingDataset(
            name=f"style_{style_name}_{len(book_ids)}books",
            book_ids=book_ids,
            style_name=style_name,
        )

        samples: list[dict] = []

        async with async_session_factory() as db:
            for book_id in book_ids:
                book = await db.get(ReferenceBook, book_id)
                if not book:
                    continue

                result = await db.execute(
                    select(TextChunk)
                    .where(TextChunk.book_id == book_id)
                    .where(TextChunk.char_count >= min_chunk_chars)
                    .order_by(TextChunk.sequence_id)
                )
                chunks = result.scalars().all()

                for i, chunk in enumerate(chunks):
                    if len(samples) >= max_samples:
                        break

                    # Build context from previous chunk
                    prev_context = ""
                    if i > 0:
                        prev_chunk = chunks[i - 1]
                        # Use last 200 chars of previous chunk as context
                        prev_context = prev_chunk.content[-200:]

                    # Build instruction
                    instruction = (
                        f"\u4ee5{style_name}\u7684\u5199\u4f5c\u98ce\u683c\uff0c"
                        f"\u6839\u636e\u4ee5\u4e0b\u60c5\u8282\u4e0a\u4e0b\u6587\u7ee7\u7eed\u5199\u4f5c\u5c0f\u8bf4\u6b63\u6587\u3002"
                    )

                    # Build input
                    input_parts = []
                    if chunk.chapter_title:
                        input_parts.append(f"\u7ae0\u8282\uff1a{chunk.chapter_title}")
                    if prev_context:
                        input_parts.append(f"\u524d\u6587\uff1a{prev_context}")
                    input_text = "\n".join(input_parts)

                    sample = TrainingSample(
                        instruction=instruction,
                        input_text=input_text,
                        output=chunk.content,
                    )
                    samples.append(sample.to_dict())

        # Save to file
        if not output_path:
            TRAINING_DATA_DIR.mkdir(parents=True, exist_ok=True)
            output_path = str(TRAINING_DATA_DIR / f"{dataset.name}.json")

        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(samples, f, ensure_ascii=False, indent=2)

        dataset.sample_count = len(samples)
        dataset.file_path = output_path

        logger.info(
            "Exported %d training samples for style '%s' to %s",
            len(samples), style_name, output_path,
        )
        return dataset

    async def export_sharegpt_format(
        self,
        book_ids: list[str],
        style_name: str,
        output_path: str | None = None,
        max_samples: int = 5000,
    ) -> TrainingDataset:
        """
        Export in ShareGPT multi-turn format for chat-style fine-tuning.

        Format:
        {"conversations": [
            {"from": "human", "value": "instruction + input"},
            {"from": "gpt", "value": "output"}
        ]}
        """
        from sqlalchemy import select
        from app.db.session import async_session_factory
        from app.models.project import TextChunk

        dataset = TrainingDataset(
            name=f"style_{style_name}_sharegpt",
            book_ids=book_ids,
            style_name=style_name,
            format="sharegpt",
        )

        samples: list[dict] = []

        async with async_session_factory() as db:
            for book_id in book_ids:
                result = await db.execute(
                    select(TextChunk)
                    .where(TextChunk.book_id == book_id)
                    .where(TextChunk.char_count >= 200)
                    .order_by(TextChunk.sequence_id)
                )
                chunks = result.scalars().all()

                for i, chunk in enumerate(chunks):
                    if len(samples) >= max_samples:
                        break

                    prev_context = chunks[i - 1].content[-200:] if i > 0 else ""
                    human_msg = (
                        f"\u8bf7\u4ee5{style_name}\u7684\u98ce\u683c\u7ee7\u7eed\u5199\u4f5c\u3002"
                    )
                    if chunk.chapter_title:
                        human_msg += f"\n\u7ae0\u8282\uff1a{chunk.chapter_title}"
                    if prev_context:
                        human_msg += f"\n\u524d\u6587\uff1a{prev_context}"

                    samples.append({
                        "conversations": [
                            {"from": "human", "value": human_msg},
                            {"from": "gpt", "value": chunk.content},
                        ]
                    })

        if not output_path:
            TRAINING_DATA_DIR.mkdir(parents=True, exist_ok=True)
            output_path = str(TRAINING_DATA_DIR / f"{dataset.name}.json")

        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(samples, f, ensure_ascii=False, indent=2)

        dataset.sample_count = len(samples)
        dataset.file_path = output_path
        return dataset


class LoRATrainingManager:
    """
    Manages LoRA training jobs.

    Integrates with Unsloth for QLoRA training on consumer GPUs.
    Target: RTX 5080 (16GB) with Qwen2.5-7B 4-bit quantization.
    """

    def __init__(self, base_dir: Path = LORA_BASE_DIR):
        self.base_dir = base_dir
        self.adapter_dir = base_dir / "adapters"
        self.adapter_dir.mkdir(parents=True, exist_ok=True)

    def generate_training_script(
        self,
        dataset_path: str,
        adapter_name: str,
        config: dict | None = None,
    ) -> str:
        """
        Generate a Python training script for Unsloth QLoRA.

        Returns the script content. User can run it manually or
        we can execute it via subprocess.
        """
        cfg = {**DEFAULT_TRAINING_CONFIG, **(config or {})}
        output_dir = str(self.adapter_dir / adapter_name)

        script = f'''#!/usr/bin/env python3
"""
Auto-generated LoRA training script for: {adapter_name}
Base model: {cfg["base_model"]}
Quantization: {cfg["quantization"]}
Estimated VRAM: ~{cfg["estimated_vram_gb"]}GB
"""

from unsloth import FastLanguageModel
import torch
from datasets import load_dataset
from trl import SFTTrainer
from transformers import TrainingArguments

# Load base model with 4-bit quantization
model, tokenizer = FastLanguageModel.from_pretrained(
    model_name="{cfg["base_model"]}",
    max_seq_length={cfg["max_seq_length"]},
    dtype=None,  # Auto-detect
    load_in_4bit=True,
)

# Configure LoRA
model = FastLanguageModel.get_peft_model(
    model,
    r={cfg["lora_rank"]},
    lora_alpha={cfg["lora_alpha"]},
    lora_dropout={cfg["lora_dropout"]},
    target_modules={cfg["target_modules"]},
    bias="none",
    use_gradient_checkpointing="unsloth",
)

# Load dataset
dataset = load_dataset("json", data_files="{dataset_path}", split="train")

# Alpaca prompt template
alpaca_prompt = """Below is an instruction that describes a task, paired with an input that provides further context. Write a response that appropriately completes the request.

### Instruction:
{{}}

### Input:
{{}}

### Response:
{{}}"""

def formatting_prompts_func(examples):
    instructions = examples["instruction"]
    inputs = examples["input"]
    outputs = examples["output"]
    texts = []
    for inst, inp, out in zip(instructions, inputs, outputs):
        text = alpaca_prompt.format(inst, inp, out) + tokenizer.eos_token
        texts.append(text)
    return {{"text": texts}}

dataset = dataset.map(formatting_prompts_func, batched=True)

# Training
trainer = SFTTrainer(
    model=model,
    tokenizer=tokenizer,
    train_dataset=dataset,
    dataset_text_field="text",
    max_seq_length={cfg["max_seq_length"]},
    dataset_num_proc=2,
    packing=False,
    args=TrainingArguments(
        output_dir="{output_dir}",
        per_device_train_batch_size={cfg["per_device_batch_size"]},
        gradient_accumulation_steps={cfg["gradient_accumulation_steps"]},
        warmup_ratio={cfg["warmup_ratio"]},
        num_train_epochs={cfg["num_epochs"]},
        learning_rate={cfg["learning_rate"]},
        fp16={str(cfg["fp16"]).lower() == "true"},
        logging_steps={cfg["logging_steps"]},
        save_steps={cfg["save_steps"]},
        optim="{cfg["optimizer"]}",
        seed=42,
        report_to="none",
    ),
)

print("Starting training...")
trainer_stats = trainer.train()
print(f"Training complete. Loss: {{trainer_stats.training_loss:.4f}}")

# Save adapter
model.save_pretrained("{output_dir}")
tokenizer.save_pretrained("{output_dir}")
print(f"Adapter saved to {output_dir}")
'''
        return script

    def list_adapters(self) -> list[dict]:
        """List all available LoRA adapters."""
        adapters = []
        if self.adapter_dir.exists():
            for d in self.adapter_dir.iterdir():
                if d.is_dir() and (d / "adapter_config.json").exists():
                    try:
                        with open(d / "adapter_config.json") as f:
                            config = json.load(f)
                        adapters.append({
                            "name": d.name,
                            "path": str(d),
                            "base_model": config.get("base_model_name_or_path", ""),
                            "rank": config.get("r", 0),
                        })
                    except Exception:
                        adapters.append({"name": d.name, "path": str(d)})
        return adapters


class LoRAInferenceProvider:
    """
    LLM provider that loads a base model + LoRA adapter for inference.

    Can be registered with ModelRouter as an additional provider.
    Uses vLLM or Ollama for serving.
    """

    def __init__(self, base_model: str, adapter_path: str, backend: str = "vllm"):
        self.base_model = base_model
        self.adapter_path = adapter_path
        self.backend = backend
        self._client = None

    async def generate(self, messages: list[dict], **kwargs) -> str:
        """Generate text using the fine-tuned model."""
        if self.backend == "vllm":
            return await self._generate_vllm(messages, **kwargs)
        elif self.backend == "ollama":
            return await self._generate_ollama(messages, **kwargs)
        else:
            raise ValueError(f"Unknown backend: {self.backend}")

    async def _generate_vllm(self, messages: list[dict], **kwargs) -> str:
        """Generate via vLLM OpenAI-compatible server."""
        import openai
        if self._client is None:
            self._client = openai.AsyncOpenAI(
                base_url="http://localhost:8001/v1",
                api_key="not-needed",
            )
        response = await self._client.chat.completions.create(
            model=self.base_model,
            messages=messages,
            temperature=kwargs.get("temperature", 0.7),
            max_tokens=kwargs.get("max_tokens", 4096),
        )
        return response.choices[0].message.content or ""

    async def _generate_ollama(self, messages: list[dict], **kwargs) -> str:
        """Generate via Ollama API."""
        import httpx
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "http://localhost:11434/api/chat",
                json={
                    "model": self.base_model,
                    "messages": messages,
                    "options": {
                        "temperature": kwargs.get("temperature", 0.7),
                        "num_predict": kwargs.get("max_tokens", 4096),
                    },
                    "stream": False,
                },
                timeout=120,
            )
            data = response.json()
            return data.get("message", {}).get("content", "")
