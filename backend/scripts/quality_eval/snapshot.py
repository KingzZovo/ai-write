"""契约快照:把某次提交里的写作契约文本冻下来,用于同条件 A/B。

改了 ``narrative_contract`` / ``narrative_quality_gates`` 之后,仓库里只剩新版契约,
无法再跑"改动前"。快照把旧版渲染结果按块存成 JSON,``run --snapshot`` 时按块替换,
于是前后两组可以在同一模型、同一温度、同一采样次数下对照。

快照只保存 prompt 文本(已经在 git 里),不保存任何凭据。
"""
from __future__ import annotations

import json
import subprocess
import types
from pathlib import Path
from typing import Any

from scripts.quality_eval.prompt_assembly import build_prompt

REPO_ROOT = Path(__file__).resolve().parents[3]
_SOURCES = (
    "backend/app/services/narrative_contract.py",
    "backend/app/services/narrative_quality_gates.py",
)


def _load_module_from_rev(rev: str, path: str) -> types.ModuleType:
    """把某个提交里的模块源码加载成独立模块对象。

    dataclasses 需要模块出现在 ``sys.modules`` 里才能解析注解,所以先注册再 exec;
    用完即弃,不污染当前进程的 app.services。
    """
    import sys

    source = subprocess.check_output(["git", "show", f"{rev}:{path}"], cwd=REPO_ROOT, text=True)
    name = f"_contract_snapshot_{Path(path).stem}_{abs(hash(rev)) % 10000}"
    module = types.ModuleType(name)
    module.__dict__["__file__"] = str(REPO_ROOT / path)
    sys.modules[name] = module
    try:
        exec(compile(source, f"{rev}:{path}", "exec"), module.__dict__)  # noqa: S102
    except Exception:
        sys.modules.pop(name, None)
        raise
    return module

def capture(rev: str, cases_path: str | Path, variants: tuple[str, ...],
            out_path: str | Path) -> dict[str, Any]:
    """渲染 ``rev`` 版本的契约块并存成快照。"""
    from scripts.quality_eval.cases import load_case_set

    case_set = load_case_set(cases_path)
    case = case_set.cases[0]
    old = {path: _load_module_from_rev(rev, path) for path in _SOURCES}
    snapshot: dict[str, Any] = {
        "rev": rev,
        "sources": {path: subprocess.check_output(
            ["git", "rev-parse", f"{rev}:{path}"], cwd=REPO_ROOT, text=True).strip()
            for path in _SOURCES},
        "blocks": {},
    }
    for variant in variants:
        case_bundle = build_prompt(case, variant)
        blocks: dict[str, str] = {}
        for block in case_bundle.blocks:
            name = block["name"]
            if name == "writer_contract":
                blocks[name] = old[_SOURCES[0]].WRITER_CONTRACT_PROMPT
            elif name == "preflight_blueprint":
                blocks[name] = old[_SOURCES[1]].preflight_scene_blueprint_prompt(chapter_idx=1)
        if blocks:
            snapshot["blocks"][variant] = blocks
    Path(out_path).write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return snapshot


def load(path: str | Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "blocks" not in data:
        raise ValueError(f"invalid contract snapshot: {path}")
    return data
