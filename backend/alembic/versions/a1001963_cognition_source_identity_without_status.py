"""Drop `status` from the cognition source identity.

背景(实测):`_receipt_from_row` 把章节的 `status` 也写进了回执,而
`source_hash = digest(receipt)`。`status` 是**评审态**,会随审核动作反复翻转
(`needs_review` ↔ `completed`)而正文一个字都没变。于是:

1. 提取时章节是 `needs_review` → 记录按"含 status=needs_review"的身份存下;
2. 评审把它提升为 `completed` → 台账 `_ledger_headers` 按新身份查记录 → 查不到
   → `effective_change_payload` 见 `record is None` → 抛
   `character_cognition_not_ready`;
3. 因为它的 `status` 恰是 `completed`,走的是**严格**路径(非 completed 章才降级跳过)
   → 同一批次里只要有一章如此,**后续所有章**一起 failed 级联停摆。

实测卷 1 的 ch65(`3095935d-…`):正文 `dda97cf219…`、记录 `completed@82a5a42b`。
按现有身份推导 `digest(status='completed')=82a5a42b`、`digest(status='needs_review')=a339b8bc`
—— 完全对应上。

本迁移把既有记录的身份收敛到"不含 status"的形态,与新的 `_receipt_from_row` 对齐:
重写 `source_json`(去掉 status)、重算 `source_hash`、并在有 `payload_hash` 时按
`digest([source_hash, changes_json])` 重算它。

安全性:迁移前核对过**同一章的记录两两 content_hash 都不同**(1120 章 / 1148 条,
`recs == distinct content_hash`),所以去掉 status 不会让任何两行撞上
`uq_chapter_cognition_source`。
"""
import hashlib
import json

from alembic import op
from sqlalchemy import text

revision = 'a1001963'
down_revision = 'a1001962'
branch_labels = None
depends_on = None

# 与 app/services/entity_sources.py 的 `_receipt_from_row` 保持同一组键与顺序。
# 身份里**只有 status 被剔除**:它随审核动作翻转而正文一字未动。
# title 与位置字段按既有契约保留(改题/重排都让旧 source 失效)。
IDENTITY_KEYS = ('chapter_id', 'project_id', 'volume_id', 'title',
                 'chapter_idx', 'global_idx', 'volume_idx', 'content_hash')
TABLES = ('chapter_cognition_extractions', 'cognition_revisions')


def _digest(value):
    """与 app.services.entity_sources.digest 逐字一致(这里不 import 应用代码)。"""
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def upgrade():
    conn = op.get_bind()
    for table in TABLES:
        rows = conn.execute(text(
            f"SELECT id, source_json, changes_json, payload_hash FROM {table}"
        )).fetchall()
        for row in rows:
            src = row.source_json
            # JSON 列在驱动层已是 dict;不是 dict 就跳过。
            if isinstance(src, str):
                src = json.loads(src)
            if not isinstance(src, dict):
                continue
            reduced = {key: src.get(key) for key in IDENTITY_KEYS}
            # 幂等:已经是目标形态就不再动它(本迁移的键表在开发期调整过一次)。
            if src == reduced:
                continue
            new_hash = _digest(reduced)
            params = {'id': row.id, 'src': json.dumps(reduced), 'sh': new_hash}
            if row.payload_hash is None:
                # 未完成提取(pending/failed)没有 payload 回执,保持 NULL。
                conn.execute(text(
                    f"UPDATE {table} SET source_json = CAST(:src AS json), source_hash = :sh "
                    "WHERE id = :id"), params)
            else:
                params['ph'] = _digest([new_hash, row.changes_json])
                conn.execute(text(
                    f"UPDATE {table} SET source_json = CAST(:src AS json), source_hash = :sh, "
                    "payload_hash = :ph WHERE id = :id"), params)


def downgrade():
    """不可逆:提取当时的评审态没有被保存下来,无法还原(也不需要)。

    展示用的章节状态一律现读 `chapters.status`(见 cognition_reader._chapter_item),
    所以丢掉回执里的 status 不损失任何可展示信息。
    """
