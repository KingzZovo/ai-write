"""Retain author cognition decisions and distinguish belief from knowledge."""
from alembic import op
import sqlalchemy as sa

revision = 'a1001948'
down_revision = 'a1001947'
branch_labels = None
depends_on = None

OLD_PROMPT = '''从完整章节正文抽取影响后续剧情的认知变化，只输出 JSON 数组。
每项是 {"character":"人物名或__reader__","learns":"本章明确得知的信息","evidence":"连续逐字原文"}
或 {"character":"人物名","still_unknown":"正文明确表明其仍不知道的信息","evidence":"连续逐字原文"}。
区分真实事实、人物猜测与误解，不能把怀疑写成得知；旁人说话也不等于角色相信。
读者已知不等于角色已知；没有描写人物获知，不足以证明其不知道。
只提取本章实际建立的变化，不引用大纲或累计账本补事实。不限制条目数量，无变化输出 []。
每项必须有能支持该人物知情状态的原文依据；逐字引文用于定位，不能用无关引文凑证据。'''
NEW_PROMPT = '''从完整章节正文抽取影响后续剧情的认知变化，只输出 JSON 数组。
每项是 {"character":"人物名或__reader__","learns":"本章明确得知的信息","evidence":"连续逐字原文"}
或 {"character":"人物名","still_unknown":"正文明确表明其仍不知道的信息","evidence":"连续逐字原文"}
或 {"character":"人物名","believes":"人物当时确信或误信的内容，保留主观限定","evidence":"连续逐字原文"}
或 {"character":"人物名","suspects":"人物当时怀疑的内容，保留不确定性","evidence":"连续逐字原文"}。
每项只用一种认知字段。learns 仅用于正文明确建立的获知；确信、误信用 believes，怀疑用 suspects。
不能把人物的误信判为作者写错，也不能替正文揭穿悬念；旁人说话不等于角色相信。
例如“他以为信已寄出”应记 believes，不是 learns；“她怀疑信被拆过”应记 suspects。
__reader__ 只允许 learns，记录正文明确提供给读者的信息，不推测真实读者的判断。
读者已知不等于角色已知；没有描写人物获知，不足以证明其不知道。
只提取本章实际建立的变化，不引用大纲或累计账本补事实。不限制条目数量，无变化输出 []。
每项必须有能支持该人物知情状态的原文依据。evidence 必须从正文里**直接复制连续的一段**:不改写、不概括、不加省略号、不把不相邻的两处拼在一起;复制时连标点一起抄。片段尽量短(10–40 字),只要能定位这条认知即可。写不出能逐字找到的依据,就不要提这一条。'''


def _schema(kinds):
    return {'type': 'array', 'items': {'oneOf': [
        {'type': 'object', 'required': ['character', field, 'evidence'], 'additionalProperties': False,
         'properties': {name: {'type': 'string', 'minLength': 1} for name in ('character', field, 'evidence')}}
        for field in kinds]}}


OLD_SCHEMA = _schema(('learns', 'still_unknown'))
NEW_SCHEMA = _schema(('learns', 'still_unknown', 'believes', 'suspects'))


def _replace_default(connection, old_prompt, old_schema, new_prompt, new_schema):
    # PostgreSQL JSON has no ordinary equality operator. Decode the typed
    # value and compare the whole original prompt/schema pair under row lock.
    prompts = sa.table('prompt_assets', sa.column('id', sa.UUID()), sa.column('task_type', sa.String()),
        sa.column('system_prompt', sa.Text()), sa.column('output_schema', sa.JSON()))
    rows = connection.execute(sa.select(prompts.c.id, prompts.c.output_schema).where(
        prompts.c.task_type == 'chapter_cognition', prompts.c.system_prompt == old_prompt).with_for_update()).all()
    for row in rows:
        if row.output_schema == old_schema:
            connection.execute(prompts.update().where(prompts.c.id == row.id).values(
                system_prompt=new_prompt, output_schema=new_schema))


def upgrade():
    op.add_column('chapter_cognition_extractions', sa.Column('source_mode', sa.String(16),
        nullable=False, server_default='auto'))
    op.add_column('chapter_cognition_extractions', sa.Column('author_revision_id', sa.UUID(), nullable=True))
    op.create_table('cognition_revisions',
        sa.Column('id', sa.UUID(), primary_key=True),
        sa.Column('project_id', sa.UUID(), sa.ForeignKey('projects.id', ondelete='CASCADE'), nullable=False),
        sa.Column('chapter_id', sa.UUID(), nullable=False),
        sa.Column('record_id', sa.UUID(), nullable=False),
        sa.Column('previous_id', sa.UUID(), nullable=True),
        sa.Column('source_hash', sa.String(64), nullable=False),
        sa.Column('source_json', sa.JSON(), nullable=False),
        sa.Column('base_version', sa.String(64), nullable=False),
        sa.Column('mode', sa.String(16), nullable=False),
        sa.Column('changes_json', sa.JSON(), nullable=True),
        sa.Column('model_snapshot_json', sa.JSON(), nullable=False),
        sa.Column('actor', sa.String(256), nullable=True),
        sa.Column('payload_hash', sa.String(64), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.create_index('ix_cognition_revision_chapter', 'cognition_revisions',
        ['project_id', 'chapter_id', 'created_at', 'id'])
    _replace_default(op.get_bind(), OLD_PROMPT, OLD_SCHEMA, NEW_PROMPT, NEW_SCHEMA)


def downgrade():
    connection = op.get_bind()
    if connection.scalar(sa.text('SELECT count(*) FROM cognition_revisions')):
        raise RuntimeError('不能丢弃已有作者校订历史；请先完成明确的数据保留迁移')
    _replace_default(connection, NEW_PROMPT, NEW_SCHEMA, OLD_PROMPT, OLD_SCHEMA)
    op.drop_table('cognition_revisions')
    op.drop_column('chapter_cognition_extractions', 'author_revision_id')
    op.drop_column('chapter_cognition_extractions', 'source_mode')
