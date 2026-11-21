#!/usr/bin/env python3
"""把若干部参考书均匀采样成一个"文笔样本"文本,供蒸馏使用。

为什么要采样而不是整本导入:一本 200 万字的书按 8000 字切片是 ~250 片、每片 3 个模型
分支 —— 光一部作品就是几百次调用,而**文笔蒸馏看的是句式/节奏/用词的统计特征**,
不需要读完每一个字。均匀采样(跨全书取章)比只取开头更能代表整体风格(开头往往只有铺垫)。

用法::

    python -m scripts.ops.make_style_sample --out /tmp/style_sample.txt \
      "/root/ai-write/data/reference/龙族全套....txt" "/root/ai-write/data/reference/天之炽.txt"

输出文件保留章标题,便于蒸馏时看到章节边界与场景切换。
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

_CHAPTER_RE = re.compile(r"^\s*(第[零一二三四五六七八九十百千0-9]{1,6}[章回节卷]|序\s*章|楔子|尾声)\s*(.*)$")


def read_text(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8", "gb18030", "utf-16"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def split_chapters(text: str) -> list[tuple[str, str]]:
    """(标题, 正文) 列表;没有可识别标题时按 4000 字硬切。"""
    lines = text.splitlines()
    chapters: list[tuple[str, str]] = []
    title = ""
    buffer: list[str] = []

    def flush() -> None:
        nonlocal buffer
        body = "\n".join(buffer).strip()
        if body:
            chapters.append((title, body))
        buffer = []

    for line in lines:
        match = _CHAPTER_RE.match(line)
        if match:
            flush()
            title = (match.group(1) + (" " + match.group(2) if match.group(2) else "")).strip()
        else:
            buffer.append(line)
    flush()
    if not chapters:
        body = text.strip()
        chapters = [("", body[i:i + 4000]) for i in range(0, len(body), 4000)]
    return chapters


def sample_book(path: Path, *, chapters_wanted: int, chars_per_chapter: int) -> list[tuple[str, str]]:
    chapters = split_chapters(read_text(path))
    if not chapters:
        return []
    if len(chapters) <= chapters_wanted:
        chosen = chapters
    else:
        # 均匀取:跨全书等距,避免"只看开头"。
        step = len(chapters) / chapters_wanted
        chosen = [chapters[min(len(chapters) - 1, int(i * step))] for i in range(chapters_wanted)]
    return [(title, body[:chars_per_chapter]) for title, body in chosen]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="+", help="参考书文本(txt/md)")
    parser.add_argument("--out", required=True, help="合并后的样本输出路径")
    parser.add_argument("--chapters", type=int, default=25, help="每本书取多少章(默认 25)")
    parser.add_argument("--chars-per-chapter", type=int, default=1600, help="每章取多少字(默认 1600)")
    args = parser.parse_args(argv)

    parts: list[str] = []
    report = []
    for name in args.files:
        path = Path(name)
        if not path.is_file():
            print(json.dumps({"skip": str(path), "reason": "not found"}, ensure_ascii=False))
            continue
        picked = sample_book(path, chapters_wanted=args.chapters,
                             chars_per_chapter=args.chars_per_chapter)
        chars = sum(len(body) for _, body in picked)
        parts.append(f"\n\n===== 参考书:{path.stem} =====\n")
        for title, body in picked:
            parts.append(f"\n\n{title}\n{body}\n")
        report.append({"book": path.stem, "chapters_taken": len(picked), "chars": chars})

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(parts), encoding="utf-8")
    print(json.dumps({"out": str(out), "total_chars": sum(item["chars"] for item in report),
                      "books": report}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())