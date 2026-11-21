"""每角色每章允许多条状态观测:唯一键改为按来源+内容摘要去重。

旧键 `uq_character_states_key = (project_id, character_id, chapter_start)` 假定
"一个角色一章只有一条状态"。实测(2026-11-16)这站不住:

* 同一章里角色可以有两处位置(先船舱后甲板),PG 与图谱还可能各给一条;
* `tests/test_character_location_context.py` 明确要求两条同章观测都被保留。

改成 `uq_character_states_source = (project_id, character_id, source_chapter_id, status_digest)`:
同一来源重放仍幂等(摘要不变 → 命中唯一键),同章两条不同观测可以共存。
历史行 `status_digest` 为空,不受约束(PostgreSQL 允许多个 NULL)。

Revision ID: a1001959
Revises: a1001958
"""
from alembic import op
import sqlalchemy as sa

revision = 'a1001959'
# 必须挂在 a1001958 之后:a1001957→a1001958 与本迁移若都从 a1001956 分叉,
# 仓库就会有两个 head,`alembic upgrade head` 直接报 "Multiple head revisions"
# (2026-11-17 实测)。本迁移此前从未在任何库应用过,所以改父节点是安全的。
down_revision = 'a1001958'
branch_labels = None
depends_on = None

OLD_KEY = 'uq_character_states_key'
NEW_KEY = 'uq_character_states_source'


def _columns(inspector) -> set[str]:
    return {column["name"] for column in inspector.get_columns("character_states")}


def _uniques(inspector) -> set[str]:
    return {item["name"] for item in inspector.get_unique_constraints("character_states")}


def upgrade() -> None:
    """加 `status_digest` 并把唯一键换成"来源 + 内容摘要"。

    **每一步都先查现状再动手**:实测有库(例如评估库)是 `Base.metadata.create_all`
    直接建的,`status_digest` 与新的唯一键已经存在,而 alembic 仍以为停在旧 revision。
    原来的写法会直接 `DuplicateColumnError: column "status_digest" ... already exists`,
    也就是**这种库根本 upgrade 不了**。这里改成幂等:已有的列/约束不重复创建,
    该换的旧约束照换。
    """
    inspector = sa.inspect(op.get_bind())
    if "status_digest" not in _columns(inspector):
        op.add_column("character_states",
                      sa.Column("status_digest", sa.String(length=64), nullable=True))
    # 约束可能在本次会话里刚变过,重新拿一次现状,避免用旧快照做判断。
    inspector = sa.inspect(op.get_bind())
    names = _uniques(inspector)
    if OLD_KEY in names:
        op.drop_constraint(OLD_KEY, "character_states", type_="unique")
    if NEW_KEY not in names:
        op.create_unique_constraint(
            NEW_KEY, "character_states",
            ["project_id", "character_id", "source_chapter_id", "status_digest"])


def downgrade() -> None:
    """回到"每角色每章一条"的旧键。

    降级会丢掉同章第二条观测,所以先按 (角色, chapter_start) 去重,只留最早一条。
    去重用窗口函数而不是 `a.created_at > b.created_at` 自连接:实测 `created_at`
    可能为 NULL,自连接写法在那种行上一条都删不掉,随后建旧唯一键必然
    `UniqueViolationError`(降级失败)。`ORDER BY created_at ASC NULLS LAST, id ASC`
    对 NULL 也给出一致的次序。
    """
    op.execute(
        "DELETE FROM character_states WHERE id IN ("
        " SELECT id FROM ("
        "  SELECT id, row_number() OVER ("
        "   PARTITION BY project_id, character_id, chapter_start"
        "   ORDER BY created_at ASC NULLS LAST, id ASC) AS rn"
        "  FROM character_states) ranked WHERE ranked.rn > 1)")
    inspector = sa.inspect(op.get_bind())
    names = _uniques(inspector)
    if NEW_KEY in names:
        op.drop_constraint(NEW_KEY, "character_states", type_="unique")
    if OLD_KEY not in names:
        op.create_unique_constraint(
            OLD_KEY, "character_states", ["project_id", "character_id", "chapter_start"])
    if "status_digest" in _columns(sa.inspect(op.get_bind())):
        op.drop_column("character_states", "status_digest")
