"""自动保存前的"作者有没有先改过"判定(2026-11-17)。

背景:桌面流式生成在结束时会无条件把整段正文写回 `chapters.content_text`。而生成一章
实测要几十秒到几分钟,这段时间里作者很可能已经在编辑器里改完并保存过 —— 作者的保存是
带前提的成功请求(界面显示"已保存"),随后却被流结束时的自动保存覆盖掉。

做法:请求开始时就记下该章正文的 sha256 基线;保存前重新算一次,不一致就**不覆盖**,
把 AI 稿落成未激活的候选版本,并发一条事件告知。这里只放可单测的纯逻辑。
"""

from __future__ import annotations

import hashlib


def content_hash(text: str | None) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def author_edited_since(baseline_hash: str | None, current_text: str | None) -> bool:
    """正文在生成期间被改过吗?

    ``baseline_hash is None`` 表示请求开始时拿不到基线(新章/未传 chapter_id),
    此时不做判定 —— 不能因为"读不到"就把正常保存挡掉。
    """
    if baseline_hash is None:
        return False
    return content_hash(current_text) != baseline_hash