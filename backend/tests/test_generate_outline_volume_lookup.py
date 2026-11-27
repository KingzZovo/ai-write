"""`generate/outline` 取书纲必须带 order_by + limit(1)。

根因(实测 2026-11-26):volume 分支只用
``select(Outline).where(project_id, level=='book')`` 再 ``scalar_one_or_none()``。
作品重新生成过书纲就会有多份 book 级 outline(《白蜡树下》4 份、1 份 confirmed),
于是 ``MultipleResultsFound`` → POST /api/generate/outline 直接 500,表现为
**任何重生成过书纲的作品都再也建不出新卷**(卷 5 就是这么卡住的)。

同一个文件的 chapter 分支早已改成
``order_by(is_confirmed.desc(), version.desc(), created_at.desc(), id.desc()).limit(1)`` ——
当时只修了一半。本用例保证两个分支不会再次跑偏。
"""
from pathlib import Path

SOURCE = Path(__file__).parents[1] / 'app' / 'api' / 'generate.py'


def _block_after(header: str, span: int = 18) -> str:
    """取以 `header` 开头的那段源码;按**含缩进的整行**匹配,避免同名的嵌套分支撞车。"""
    lines = SOURCE.read_text().splitlines()
    hits = [i for i, line in enumerate(lines) if line == header]
    assert len(hits) == 1, f'`{header}` 应恰好出现一次,实际 {len(hits)} 次'
    start = hits[0]
    return '\n'.join(lines[start:start + span])


VOLUME_HEADER = '    if req.level == "volume":'
CHAPTER_HEADER = '    elif req.level == "chapter":'


def test_volume_branch_limits_book_outline_lookup():
    block = _block_after(VOLUME_HEADER)
    assert '.limit(1)' in block, (
        'volume 分支取书纲没有 limit(1) → 作品有多份书纲时 scalar_one_or_none() '
        '抛 MultipleResultsFound,新卷永远建不出来')
    assert 'is_confirmed' in block, 'volume 分支取书纲没有优先 confirmed,可能拿到未确认的旧书纲'
    assert 'order_by' in block, 'volume 分支取书纲没有确定性排序'


def test_chapter_branch_keeps_the_same_lookup_shape():
    """chapter 分支必须保持同一套取法 —— 它是当年修好的那一半,别被改回去。"""
    block = _block_after(CHAPTER_HEADER)
    for marker in ('.limit(1)', 'is_confirmed', 'order_by'):
        assert marker in block, f'chapter 分支缺少 {marker}'