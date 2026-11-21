"""llm_call_logs 记录缓存命中 token:新增 `cached_input_tokens`。

作者要看"这一章花了多少 token、缓存命中率多少",但缓存命中数此前只进了
Prometheus 计数器(model_router 的 `LLM_CACHE_TOKEN_TOTAL`),库里没有。
本迁移补上这一列,供 `/api/usage/...` 做 SQL 聚合。

语义:`cached_input_tokens` 是 `input_tokens` 的**子集**(命中缓存的那部分
输入 token,供应商计价更低),不是独立桶 —— 汇总总 token 时只算
`input_tokens + output_tokens`,不能再加一遍缓存数。

`llm_call_logs` 是 PG 按月 RANGE 分区的父子表(a1001916)。`ADD COLUMN` 加在
**父表**上,PG 会自动级联到已有及后续分区,不必逐分区处理。

幂等:`upgrade()` 先查列是否存在再加。本仓库有"schema 由
`Base.metadata.create_all` 直接建好、alembic 记账落后"的库(见
`scripts/ops/alembic_sync.py`),那种库上这一列已经存在,直接
`add_column` 会 `DuplicateColumnError` 而让整个 upgrade 失败。

Revision ID: a1001960
Revises: a1001959
"""
from alembic import op
import sqlalchemy as sa

revision = 'a1001960'
down_revision = 'a1001959'
branch_labels = None
depends_on = None

TABLE = "llm_call_logs"
COLUMN = "cached_input_tokens"


def _columns() -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(TABLE)}


def upgrade() -> None:
    """加 `cached_input_tokens INTEGER NOT NULL DEFAULT 0`(已有行读 0,不是 NULL)。"""
    if COLUMN in _columns():
        return
    op.add_column(
        TABLE,
        sa.Column(COLUMN, sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    """去掉该列。同样先查现状:手工建库/半途失败的库上可能本来就没有。"""
    if COLUMN not in _columns():
        return
    op.drop_column(TABLE, COLUMN)