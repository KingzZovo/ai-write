"""v1.0 chunk 11 — seed 10 genre profiles into genre_profiles.

Idempotent on code. Run inside backend container or via docker cp.
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path

# Allow running the script from /root/ai-write or from /tmp inside the container.
_ROOT = Path(__file__).resolve().parent.parent
_BACKEND = _ROOT / "backend"
for p in (str(_BACKEND), "/app"):
    if p not in sys.path and Path(p).exists():
        sys.path.insert(0, p)

from sqlalchemy import select  # noqa: E402

from app.db.session import async_session_factory  # noqa: E402
from app.models.writing_engine import GenreProfile  # noqa: E402


SEEDS: list[dict] = [
    {
        "code": "xianxia_honghuang",
        "name": "䵙侠·洪荒",
        "description": "洪荒无量劫的䵙侠背景，重道侣、天地棋局、神话体系。",
    },
    {
        "code": "xianxia_modern",
        "name": "䵙侠·现代",
        "description": "现代都市背景下的䵙侠修行，科学与䵙迹并存，修真者隐于与市。",
    },
    {
        "code": "urban_zhuixu",
        "name": "都市·赘婿",
        "description": "赘婿翻身套路，隐藏实力·家族重视·脸疼戚成长。",
    },
    {
        "code": "urban_rebirth",
        "name": "都市·重生",
        "description": "重回过去的都市文，金融/互联网/娱乐圈先知资信累积。",
    },
    {
        "code": "scifi_apocalypse",
        "name": "科幻·末世",
        "description": "末日危机下的人性与秩序，异变/计划/资源三大主轴。",
    },
    {
        "code": "scifi_space",
        "name": "科幻·星际",
        "description": "星际穿梭与星国铁血，大型机械/星间战争/文明迭代。",
    },
    {
        "code": "xuanhuan_isekai",
        "name": "玄幻·异世界",
        "description": "穿越至异世界，剑与魔法/祖巫传承/国战羽翼。",
    },
    {
        "code": "mystery_detective",
        "name": "悬疑·探案",
        "description": "硬核推理与心理博弈，谜题·证据链·犯罪动机三步案构。",
    },
    {
        "code": "history_alt",
        "name": "历史·架空",
        "description": "架空王朝与穿越主角，政治/军事/民生三线宏叙事。",
    },
    {
        "code": "game_infinite",
        "name": "游戏·无限流",
        "description": "穿梭副本与主神空间，团队/任务/积分经营核心节奏。",
    },
]


async def main() -> None:
    inserted = 0
    skipped = 0
    async with async_session_factory() as db:
        existing = await db.execute(select(GenreProfile.code))
        codes = {r[0] for r in existing.all()}
        for row in SEEDS:
            if row["code"] in codes:
                skipped += 1
                continue
            db.add(
                GenreProfile(
                    id=uuid.uuid4(),
                    code=row["code"],
                    name=row["name"],
                    description=row["description"],
                    default_beat_pattern_ids=[],
                    default_writing_rule_ids=[],
                    is_active=True,
                )
            )
            inserted += 1
        if inserted:
            await db.commit()
    print(f"[seed_genre_profiles] inserted={inserted} skipped={skipped}")


if __name__ == "__main__":
    asyncio.run(main())
