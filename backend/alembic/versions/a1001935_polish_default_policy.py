"""Replace only the untouched legacy default polish instruction.

Downgrade deliberately retains the corrected prose policy; no schema changes.
"""
from alembic import op
import sqlalchemy as sa

revision = 'a1001935'
down_revision = 'a1001934'
branch_labels = None
depends_on = None

OLD_PROMPT = '你是一位专业的文学润色编辑。你的任务是对初稿进行风格润色，在保持剧情和逻辑不变的前提下，提升文学表现力。\n\n润色方向：\n- 替换AI高频词为更自然的表达\n- 增加五感细节（视觉之外至少两种感官）\n- 优化长短句交替节奏\n- 用生理反应代替情绪名词\n- 关键时刻放慢叙述（子弹时间）'
NEW_PROMPT = '你是中文小说与大纲的文字编辑。结合完整原稿和作者要求，只修明确的病句、指代歧义与无意重复。保留作者叙述口吻、人物各自说话方式、视角、段落关系、情节次序、事实和结尾结果。大纲保持原层级与事件，不扩写成正文。自然省略、短句、对称句和情绪直述不自动视为缺点。不统一加口语，不按感官数量补细节，不编造动作、经历或动机，不强行放慢节奏。没有可靠改进就原样返回。输出完整候选原文，不附说明、标题或代码围栏。'


def upgrade():
    op.execute(sa.text(
        "UPDATE prompt_assets SET system_prompt = :new "
        "WHERE task_type = 'polishing' AND system_prompt = :old"
    ).bindparams(new=NEW_PROMPT, old=OLD_PROMPT))


def downgrade():
    # Do not reintroduce a known defective prompt or overwrite later edits.
    pass
