"""llm_call_logs 标记"这条 token 是估算的":新增 `usage_estimated`。

背景(2026-11-15 实测):经中转调 `gemini-3.1-pro` 时,即使请求带了
`stream_options={"include_usage": True}`,上游也完全不返回 usage(同一中转下
`deepseek-v4-flash` 正常)。结果 100 章长跑里 2,664 条调用日志的
`input_tokens` / `output_tokens` 全是 0,"AI 用量"页在主模型上全显示 0 ——
比没有更糟。

修法:供应商**完全没报** usage 时,用字符数估算 token 并打上本列的标记;
供应商只要报了任一数字就原样落库、本列为 false。这样:

* 估算值不会被当成真实计量(汇总可以单独报"估算行条数/估算 token 数");
* 真实用量永远不会被覆盖。

`llm_call_logs` 是 PG 按月 RANGE 分区的父子表(a1001916)。`ADD COLUMN` 加在
**父表**上,PG 会自动级联到已有及后续分区,不必逐分区处理。

幂等:`upgrade()` 先查列是否存在再加。本仓库有"schema 由
`Base.metadata.create_all` 直接建好、alembic 记账落后"的库(见
`scripts/ops/alembic_sync.py`),那种库上这一列已经存在,直接
`add_column` 会 `DuplicateColumnError` 而让整个 upgrade 失败。

Revision ID: a1001961
Revises: a1001960
"""
from alembic import op
import sqlalchemy as sa

revision = 'a1001961'
down_revision = 'a1001960'
branch_labels = None
depends_on = None

TABLE = "llm_call_logs"
COLUMN = "usage_estimated"


def _columns() -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(TABLE)}


def upgrade() -> None:
    """加 `usage_estimated BOOLEAN NOT NULL DEFAULT false`(已有行读 false)。"""
    if COLUMN in _columns():
        return
    op.add_column(
        TABLE,
        sa.Column(COLUMN, sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )


def downgrade() -> None:
    """去掉该列。同样先查现状:手工建库/半途失败的库上可能本来就没有。"""
    if COLUMN not in _columns():
        return
    op.drop_column(TABLE, COLUMN)