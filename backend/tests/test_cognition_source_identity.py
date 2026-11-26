"""认知 source 身份里不许包含可变的评审态 `status`。

根因(实测卷 1 ch65 `3095935d-…`):`status` 曾被写进回执,而
`source_hash = digest(receipt)`。正文先落库(`needs_review`)、认知随后提取,
评审再把章节提升为 `completed` —— 身份凭空改变,台账按新身份查不到记录,
`effective_change_payload` 见 `record is None` 便抛
`character_cognition_not_ready`;又因为该章此刻正是 `completed`,走的是严格
路径(非 completed 章才降级跳过),于是**同一批次里后续所有章一起 failed**。

实测三组推导(修复前):
    digest(status='completed')    = 82a5a42b…   ← 与当时的存储记录一致
    digest(status='needs_review') = a339b8bc…   ← 状态一变就失配
    digest(status='draft')        = d7247741…

`title` 与位置字段**必须留在身份里** —— 这是既有契约:改题、重排、换卷都让旧
source 失效(`test_previous_proof_cannot_authorize_a_changed_current_source[title]`、
`test_changed_source_or_record_is_never_reused[chapter_order|volume_order]`)。
"""
import ast
from pathlib import Path
from types import SimpleNamespace as NS

from app.services.entity_sources import _receipt_from_row, digest

MIGRATION = (Path(__file__).parents[1] / 'alembic' / 'versions'
             / 'a1001963_cognition_source_identity_without_status.py')

EXPECTED_KEYS = {'chapter_id', 'project_id', 'volume_id', 'title',
                 'chapter_idx', 'global_idx', 'volume_idx', 'content_hash'}


def _row(status='completed', content_hash='a' * 64, title='床板暗格'):
    return NS(id='11111111-1111-1111-1111-111111111111',
              project_id='22222222-2222-2222-2222-222222222222',
              volume_id='33333333-3333-3333-3333-333333333333',
              title=title, chapter_idx=65, global_idx=65, volume_idx=1,
              status=status, content_hash=content_hash)


def test_receipt_excludes_review_status_only():
    """评审态不进身份;标题与位置留着。"""
    receipt = _receipt_from_row(_row(), 'a' * 64)
    assert 'status' not in receipt
    assert 'title' in receipt
    assert set(receipt) == EXPECTED_KEYS


def test_status_flip_does_not_change_source_identity():
    """同一个正文,状态怎么翻,身份都不许变。"""
    hashes = {digest(_receipt_from_row(_row(status=s), 'a' * 64))
              for s in ('completed', 'needs_review', 'draft', 'failed')}
    assert len(hashes) == 1, f'评审态改变了身份:{hashes}'


def test_body_change_still_changes_identity():
    """正文真变了必须换身份,否则台账会把旧事实当成本章的。"""
    assert digest(_receipt_from_row(_row(), 'a' * 64)) != \
           digest(_receipt_from_row(_row(), 'b' * 64))


def test_title_and_position_changes_still_change_identity():
    """既有契约:改题、重排、换卷都让旧 source 失效。"""
    base = digest(_receipt_from_row(_row(), 'a' * 64))
    altered = _row(title='改题')
    assert digest(_receipt_from_row(altered, 'a' * 64)) != base, 'title'
    for field, value in (('global_idx', 66), ('chapter_idx', 66), ('volume_idx', 2)):
        moved = _row()
        setattr(moved, field, value)
        assert digest(_receipt_from_row(moved, 'a' * 64)) != base, field


def test_migration_identity_keys_match_receipt():
    """迁移里的键表必须与 _receipt_from_row 逐字一致,否则迁移后记录会失配。"""
    tree = ast.parse(MIGRATION.read_text())
    keys = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == 'IDENTITY_KEYS' for t in node.targets):
            keys = tuple(ast.literal_eval(node.value))
    assert keys is not None, '迁移里找不到 IDENTITY_KEYS'
    assert set(keys) == EXPECTED_KEYS == set(_receipt_from_row(_row(), 'a' * 64))