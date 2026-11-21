"""Shared author-edit preconditions and accidental content-loss protection."""


class ChapterContentError(ValueError):
    def __init__(self, code: str, message: str, **context):
        super().__init__(message)
        self.code = code
        self.detail = {'code': code, 'message': message, **context}


def check_chapter_replacement(
    current: str | None, replacement: str | None, *, expected: str | None = None, force: bool = False,
) -> None:
    """Force permits an intentional reduction, never a stale source version.

    None replacement means that the author is only changing other metadata;
    an explicit expected manuscript still applies to that edit.
    """
    current = current or ''
    if expected is not None and current != expected:
        raise ChapterContentError('chapter_edit_conflict',
            '正文已在其他位置更新，未覆盖新稿。请保留当前编辑并重新核对。')
    if replacement is None or force:
        return
    previous_size, next_size = len(current.strip()), len(replacement.strip())
    if previous_size >= 200 and next_size == 0:
        raise ChapterContentError('chapter_protect_empty_content',
            '这次替换会清空已有正文，请核对后明确确认。', existing_chars=len(current))
    if previous_size >= 1000 and 0 < next_size < previous_size * 0.4:
        raise ChapterContentError('chapter_protect_shrink',
            '这次替换会删去超过六成正文，请核对是否为完整稿后明确确认。',
            existing_chars=len(current), new_chars=len(replacement))
