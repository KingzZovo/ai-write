"""Seed Team multi-agent PromptAssets (v1.0 chunk 9).

Idempotent upsert of 3 PromptAsset rows keyed by (task_type, name):
  - team_planner / Planner Agent
  - team_writer  / Writer Agent
  - team_editor  / Editor Agent

Run inside backend container:
    docker compose exec backend python scripts/seed_multi_agent_prompts.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# Ensure backend/ is on sys.path when invoked from repo root
_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE.parent / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from sqlalchemy import select  # noqa: E402

from app.db.session import async_session_factory  # noqa: E402
from app.models.prompt import PromptAsset  # noqa: E402


SEEDS: list[dict] = [
    {
        "task_type": "team_planner",
        "name": "Planner Agent",
        "description": "Multi-agent Team Planner — 大纲→细化→任务分发。",
        "system_prompt": (
            "你是团队中的 Planner Agent。任务：\n"
            "1. 从完整故事设定/大纲出发，拆解为本章细化节点。\n"
            "2. 将写作块分配给 Writer Agent，标明每块的视角、场景、关键冲突。\n"
            "3. 给出一段结构化 JSON（scenes: [...]）+ 一段纯文本摘要。\n"
            "4. 不要直接生成正文，遵守分工边界。"
        ),
        "category": "Team",
        "order": 200,
    },
    {
        "task_type": "team_writer",
        "name": "Writer Agent",
        "description": "Multi-agent Team Writer — 按分工稿件续写。",
        "system_prompt": (
            "你是团队中的 Writer Agent。任务：\n"
            "1. 按 Planner 分配的场景分工负责续写正文。\n"
            "2. 保持角色声音、时间线与前文一致。\n"
            "3. 只输出正文，不做大纲/规划调整；有相逵组件请标注【?】。\n"
            "4. 遵守绚文风格与字数线。"
        ),
        "category": "Team",
        "order": 201,
    },
    {
        "task_type": "team_editor",
        "name": "Editor Agent",
        "description": "Multi-agent Team Editor — 改写+文风统一+硬伤修补。",
        "system_prompt": (
            "你是团队中的 Editor Agent。任务：\n"
            "1. 接收 Writer 的多块稿件，统一文风、口吻、节奏。\n"
            "2. 修补 Critic 报告中的 hard/soft/ai_trap 问题。\n"
            "3. 不改情节走向，只做文本层改写。\n"
            "4. 输出整章终稿文本。"
        ),
        "category": "Team",
        "order": 202,
    },
]


async def _upsert() -> dict[str, int]:
    inserted = 0
    skipped = 0
    async with async_session_factory() as session:
        for row in SEEDS:
            stmt = select(PromptAsset).where(
                PromptAsset.task_type == row["task_type"],
                PromptAsset.name == row["name"],
            )
            existing = (await session.execute(stmt)).scalar_one_or_none()
            if existing is not None:
                skipped += 1
                continue
            obj = PromptAsset(
                task_type=row["task_type"],
                name=row["name"],
                description=row["description"],
                mode="text",
                system_prompt=row["system_prompt"],
                user_template="",
                category=row["category"],
                order=row["order"],
                always_enabled=0,
                is_active=1,
            )
            session.add(obj)
            inserted += 1
        await session.commit()
    return {"inserted": inserted, "skipped": skipped}


def main() -> int:
    result = asyncio.run(_upsert())
    print(f"[seed_multi_agent_prompts] inserted={result['inserted']} skipped={result['skipped']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
