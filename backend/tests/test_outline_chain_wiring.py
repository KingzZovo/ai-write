"""规划链的接线自检(2026-11-21)。

为什么单独加这一组:此前所有长跑都用评估探针(probe),而 probe **绕过了规划链**
(直接把 blueprint/章纲写库),所以"建项目 → 书纲 → 卷纲 → 章纲"这条**真正的用户
路径**从没被端到端跑过。第一次真跑就连续撞出 5 个链路缺件:

1. 提示词库整个大纲阶段没种(outline_book_skeleton / world / characters /
   volume_meta / volume_batch / chapter)→ 第一句就 `No active prompt registered`;
2. `PromptRegistry.seed_builtins` 只 flush 不 commit → 调用方不提交就全丢;
3. 大纲任务没有任何端点/模型绑定 → `has no endpoint configured`;
4. 章纲 prompt 把整份书纲 + 整卷 100 章都塞进去(实测 6.6 万 tokens/章);
5. 书纲 volume_plan 结构化字段解析不全。

这组用例盯的是**接线**(不是模型输出):链路件在不在、裁剪生效不生效、
方法有没有跑出类外。离线可跑。
"""

from __future__ import annotations

import ast
import inspect

from app.services import outline_generator as og
from app.services.outline_generator import OutlineGenerator, _slim_for_chapter_prompt


def test_generator_methods_stay_inside_the_class():
    """helper 若插到类内部会把方法顶出类外 —— 症状是 'object has no attribute'。"""
    tree = ast.parse(inspect.getsource(og))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "OutlineGenerator")
    methods = {n.name for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for name in ("generate_book_outline", "generate_volume_outline", "generate_chapter_outline"):
        assert name in methods, f"{name} 不在 OutlineGenerator 类里"


def test_slim_projection_drops_bulk_and_keeps_essentials():
    book = {
        "raw_text": "x" * 50000,
        "structured": {"main_plot": "内核", "characters": "人物", "world_setting": "世界观",
                       "sections": {"大": "y" * 30000}, "chapter_naming_style": "z" * 20000},
        "volume_plan": [{"idx": i, "est_chapters": 100, "junk": "j" * 500} for i in range(1, 6)],
    }
    volume = {
        "volume_idx": 1, "title": "第一卷", "core_conflict": "冲突",
        "chapter_summaries": [{"chapter_idx": i, "title": f"第{i}章", "summary": "s" * 200}
                              for i in range(1, 101)],
        "raw_text": "w" * 40000, "sections": {"k": "v" * 10000}, "_stages": {"a": True},
    }
    b, v = _slim_for_chapter_prompt(book, volume, 1, window=2)
    # 允许:内核/人物/世界观/标题/立意 + raw_text(事实来源,必须保留)+ volume_plan(判断末章用)
    assert set(b) <= {"title", "premise", "main_plot", "characters", "world_setting",
                      "raw_text", "volume_plan"}, "书纲只该保留必要字段"
    assert "raw_text" in b, "书纲事实写在 raw_text 里,裁剪不能丢"
    assert "volume_plan" in b, "volume_plan 要用来判断'是否全书最后一章'"
    assert "sections" not in v and "raw_text" not in v and "_stages" not in v
    nearby = v["chapter_summaries"]
    assert [c["chapter_idx"] for c in nearby] == [1, 2, 3], "只保留本章前后各 window 章"
    assert len(b["raw_text"]) <= 8000, "raw_text 要截断"
    assert b["volume_plan"] and all(set(x) <= {"idx", "est_chapters"} for x in b["volume_plan"]), \
        "volume_plan 要精简成 idx/est_chapters"


def test_chapter_prompt_uses_the_slim_projection():
    src = inspect.getsource(OutlineGenerator.generate_chapter_outline)
    assert "_slim_for_chapter_prompt" in src
    assert "book_slim" in src and "volume_slim" in src


def test_seed_builtins_requires_caller_commit():
    """seed_builtins 只 flush —— 调用方必须自己 commit,否则静默回滚。"""
    src = inspect.getsource(og.PromptRegistry.seed_builtins) if hasattr(og, "PromptRegistry") else ""
    if not src:
        from app.services.prompt_registry import PromptRegistry
        src = inspect.getsource(PromptRegistry.seed_builtins)
    assert "flush" in src
    assert "commit" not in src, "若改成自带 commit,请同步更新这条断言与 plan_novel 的调用"


def test_outline_stage_prompt_tasks_are_defined():
    """大纲阶段的任务必须在内置提示词里齐全,否则规划链第一步就断。"""
    from app.services.prompt_registry import BUILTIN_PROMPTS
    have = {p.get("task_type") for p in BUILTIN_PROMPTS}
    for task in ("outline_book", "outline_book_skeleton", "outline_book_world",
                 "outline_book_characters", "outline_volume", "outline_volume_meta",
                 "outline_volume_batch", "outline_chapter"):
        assert task in have, f"缺少内置提示词:{task}"
