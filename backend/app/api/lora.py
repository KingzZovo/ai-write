"""LoRA fine-tuning management endpoints."""

import logging
import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.project import ReferenceBook

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/lora", tags=["lora"])


class ExportDatasetRequest(BaseModel):
    book_ids: list[str]
    style_name: str
    format: str = "alpaca"  # alpaca or sharegpt
    max_samples: int = 5000


class GenerateScriptRequest(BaseModel):
    dataset_path: str
    adapter_name: str
    base_model: str = "Qwen/Qwen2.5-7B"
    lora_rank: int = 64
    num_epochs: int = 3
    learning_rate: float = 2e-4
    max_seq_length: int = 4096


class DatasetResponse(BaseModel):
    name: str
    style_name: str
    sample_count: int
    file_path: str
    format: str


@router.post("/export-dataset")
async def export_dataset(
    req: ExportDatasetRequest,
    db: AsyncSession = Depends(get_db),
) -> DatasetResponse:
    """Export training data from reference books for LoRA fine-tuning."""
    # Verify books exist and have quality score
    for book_id in req.book_ids:
        book = await db.get(ReferenceBook, book_id)
        if not book:
            raise HTTPException(status_code=404, detail=f"Book {book_id} not found")
        if book.status == "low_quality":
            raise HTTPException(
                status_code=400,
                detail=f"Book '{book.title}' has low quality score, not suitable for training",
            )

    from app.services.lora_manager import LoRADataExporter
    exporter = LoRADataExporter()

    if req.format == "sharegpt":
        dataset = await exporter.export_sharegpt_format(
            book_ids=req.book_ids,
            style_name=req.style_name,
            max_samples=req.max_samples,
        )
    else:
        dataset = await exporter.export_training_data(
            book_ids=req.book_ids,
            style_name=req.style_name,
            max_samples=req.max_samples,
        )

    return DatasetResponse(
        name=dataset.name,
        style_name=dataset.style_name,
        sample_count=dataset.sample_count,
        file_path=dataset.file_path,
        format=dataset.format,
    )


@router.post("/generate-script")
async def generate_training_script(req: GenerateScriptRequest) -> dict:
    """Generate a training script for LoRA fine-tuning."""
    from app.services.lora_manager import LoRATrainingManager

    manager = LoRATrainingManager()
    config = {
        "base_model": req.base_model,
        "lora_rank": req.lora_rank,
        "num_epochs": req.num_epochs,
        "learning_rate": req.learning_rate,
        "max_seq_length": req.max_seq_length,
    }

    script = manager.generate_training_script(
        dataset_path=req.dataset_path,
        adapter_name=req.adapter_name,
        config=config,
    )

    # Save script to file
    script_dir = Path("/root/ai-write/lora/scripts")
    script_dir.mkdir(parents=True, exist_ok=True)
    # 2026-11-17:adapter_name 来自请求体且此前只受 str 约束,`../../tmp/x` 就能把文件
    # 写到任意可写路径。这里同时做字符白名单与 resolve 后的父目录校验。
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", req.adapter_name or ""):
        raise HTTPException(status_code=422, detail="adapter_name 只允许字母/数字/下划线/点/短横,长度 1-64")
    script_path = (script_dir / f"train_{req.adapter_name}.py").resolve()
    if script_dir.resolve() not in script_path.parents:
        raise HTTPException(status_code=422, detail="adapter_name 非法")
    script_path.write_text(script, encoding="utf-8")

    return {
        "script_path": str(script_path),
        "adapter_name": req.adapter_name,
        "base_model": req.base_model,
        "message": f"Training script saved. Run: python {script_path}",
    }


@router.get("/adapters")
async def list_adapters() -> list[dict]:
    """List all available LoRA adapters."""
    from app.services.lora_manager import LoRATrainingManager

    manager = LoRATrainingManager()
    return manager.list_adapters()


@router.get("/config")
async def get_default_config() -> dict:
    """Get default training configuration for RTX 5080."""
    from app.services.lora_manager import DEFAULT_TRAINING_CONFIG
    return {
        "config": DEFAULT_TRAINING_CONFIG,
        "recommended_models": [
            {
                "name": "Qwen/Qwen2.5-7B",
                "vram": "~14GB (QLoRA 4bit)",
                "chinese_quality": "excellent",
                "recommended": True,
            },
            {
                "name": "01-ai/Yi-1.5-6B",
                "vram": "~12GB (QLoRA 4bit)",
                "chinese_quality": "good",
                "recommended": False,
            },
            {
                "name": "THUDM/glm-4-9b",
                "vram": "~16GB (QLoRA 4bit)",
                "chinese_quality": "good",
                "recommended": False,
            },
        ],
        "hardware_note": "Optimized for RTX 5080 (16GB VRAM) with QLoRA 4-bit quantization",
    }
