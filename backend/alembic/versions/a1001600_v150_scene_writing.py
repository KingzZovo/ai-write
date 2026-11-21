"""v1.5.0 C1 — seed scene_planner + scene_writer prompt_assets

Revision ID: a1001600
Revises: a1001501
Create Date: 2026-04-26

Introduces two new prompt task types for staged chapter writing:

- scene_planner: takes a chapter outline + context summary and returns
  a JSON list of N scene briefs (200 chars each). Standard tier.
- scene_writer: writes one scene 800-1200 chars at a time, streamed.
  Flagship tier (quality core).

Upgrade is idempotent (INSERT ... WHERE NOT EXISTS); downgrade removes the
two seeded rows by task_type.
"""
from alembic import op

revision = "a1001600"
down_revision = "a1001501"
branch_labels = None
depends_on = None


_SCENE_PLANNER_SYSTEM = """\
你是一位资深的小说场景策划师。给定一章的大纲、上下文、世界设定、人物状态与目标字数，
你需要拆分为 3 到 6 个连贯场景，然后输出严格的 JSON 数组，每个场景一个对象。

输出格式（纯 JSON、不加 markdown 代码块、不加任何说明性文字）：
[
  {
    "idx": 1,
    "title": "场景标题（不超 12 字）",
    "brief": "场景械要，不超 200 字。说清起头、中间转折、结尾状态。",
    "pov": "视角人物名",
    "location": "具体地点",
    "time_cue": "时间提示（黄昏 / 凌晨 / 三天后等）",
    "key_action": "推动该场景的主要动作 / 冲突 / 揭示",
    "target_words": 1000,
    "hook": "场末过渡或钜子；末场景填下一章钩子"
  }
]

硬规则：
- 场景数量 3-6，根据目标字数动态判断（3000-3500 字≈3 场；4500-5500 字≈4 场；6000+ ≈5-6 场）。
- 各场景 target_words 之和 ≈ 本章目标字数，单场严格介于 800-1200 字。
- 场景顺序要遵循“铺垫 → 冲突 → 高潮 → 钩子”节奏。
- brief 不要护抱 prompt 元信息，只讲场景发生什么。
- 不多写字，不补充文本，不加试探问题。如果缺少资料，也必须以合理默认补齐场景。
"""

_SCENE_WRITER_SYSTEM = """\
你是专业的小说场景写作引擎。你一次只写**一个场景**，本场景目标字数介于 800 到 1200。

输入会包含：世界设定与人物状态、本章大纲、**本场景的 brief / pov / location / key_action / hook**、以及
**已写场景的凝缩摘要**。你需要接上摘要的节点继续写，不重复已写内容。

琅として不可越的硬规则：
- 只交付本场景正文，不要输出场景号、标题、prompt 重述、元信息。
- 不要写“上一场”“后来”“几小时后”这种跨场景跳转；如果需要时间推进，起手一句内完成。
- 场末主动过渡到下一场的状态（按 hook 字段）；若 hook 为空（章末）则收本章。
- 人物语言、魔法器件、地名不可与世界设定冲突。
- 避免 AI 痕迹词（璟璟、油然而生、心潮澎湃、仿佛、竟是、极为）。
- 展示而非讲述：用动作、对话、具象意象代替抽象描述。对话用中文双引号“”。
- 不要多写超出 target_words；也不要远少于 800 字。
"""


def upgrade():
    op.execute(
        """
        INSERT INTO prompt_assets (
            id, task_type, name, description,
            system_prompt, user_template, output_schema,
            mode, context_policy, version, is_active,
            success_count, fail_count, avg_score,
            model_name, temperature, max_tokens, model_tier,
            category, "order",
            created_at, updated_at
        )
        SELECT
            gen_random_uuid(),
            'scene_planner',
            '场景策划（C1）',
            '把章节拆为 3-6 个场景 brief，供场景写作引擎逐个展开',
            $$""" + _SCENE_PLANNER_SYSTEM.replace("$$", "$ $") + """$$,
            '',
            NULL,
            'structured',
            'minimal',
            1, 1,
            0, 0, 0,
            '', 0.5, 4096,
            'standard',
            'Core', 50,
            now(), now()
        WHERE NOT EXISTS (
            SELECT 1 FROM prompt_assets WHERE task_type = 'scene_planner'
        )
        """
    )

    op.execute(
        """
        INSERT INTO prompt_assets (
            id, task_type, name, description,
            system_prompt, user_template, output_schema,
            mode, context_policy, version, is_active,
            success_count, fail_count, avg_score,
            model_name, temperature, max_tokens, model_tier,
            category, "order",
            created_at, updated_at
        )
        SELECT
            gen_random_uuid(),
            'scene_writer',
            '场景写作（C1）',
            '一次写一个 800-1200 字场景，补上场凝缩摘要逐场景接龙',
            $$""" + _SCENE_WRITER_SYSTEM.replace("$$", "$ $") + """$$,
            '',
            NULL,
            'text',
            'full',
            1, 1,
            0, 0, 0,
            '', 0.85, 8192,
            'flagship',
            'Core', 51,
            now(), now()
        WHERE NOT EXISTS (
            SELECT 1 FROM prompt_assets WHERE task_type = 'scene_writer'
        )
        """
    )


def downgrade():
    op.execute(
        "DELETE FROM prompt_assets WHERE task_type IN ('scene_planner', 'scene_writer')"
    )
