"""质量验收运行器:固定样本集 + 真实模型 + 确定性检测。

产物目录(拒绝覆盖已存在目录):

- ``cases.json``       本次使用的样本集副本(含 sha256)
- ``<case>-<variant>.txt``          模型正文原文
- ``<case>-<variant>.request.json`` 本次调用元信息(模型/端点/温度/max_tokens/
  prompt 哈希/耗时/token 数/finish_reason/正文 sha256)
- ``detections.json``  每个产出的确定性检测结果
- ``summary.json``     按题材/变体汇总的计数与语言指标
- ``RUN.md``           机器生成的事实清单(命令、commit、模型、逐项计数)

检测器是纯函数,``--detect-only`` 可以对已有目录重跑一遍,用来证明同一输入的检测结论一致。
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.quality_eval import detectors
from scripts.quality_eval.cases import CaseSet, load_case_set
from scripts.quality_eval.prompt_assembly import build_prompt, production_contract_snapshot

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_ENDPOINT = "http://127.0.0.1:8317/v1"
DEFAULT_KEY_CONFIG = Path("/root/migration-deploy/cliproxyapi/config.yaml")


class QualityEvalError(RuntimeError):
    """验收运行失败;调用方必须停止,不得把它当成"没发现问题"。"""


def _git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except Exception:
        return "unknown"


def resolve_api_key(explicit: str | None = None) -> str:
    """取模型网关 key:显式参数 > 环境变量 > 本地网关配置。绝不写入产物。"""
    import os

    if explicit:
        return explicit.strip()
    env = os.environ.get("QUALITY_EVAL_API_KEY", "").strip()
    if env:
        return env
    config = Path(os.environ.get("QUALITY_EVAL_KEY_CONFIG", DEFAULT_KEY_CONFIG))
    try:
        import yaml

        data = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        raise QualityEvalError(f"cannot read gateway config {config}: {exc}") from None
    for candidate in (
        (data.get("access") or {}).get("api-keys") if isinstance(data.get("access"), dict) else None,
        data.get("api-keys"),
    ):
        if isinstance(candidate, list):
            for item in candidate:
                if isinstance(item, str) and item.strip():
                    return item.strip()
    raise QualityEvalError("no API key found; pass --api-key or set QUALITY_EVAL_API_KEY")


@dataclass(frozen=True)
class ModelRequest:
    endpoint: str
    model: str
    api_key: str
    temperature: float
    max_tokens: int
    timeout: float


def call_model(system: str, user: str, req: ModelRequest) -> dict[str, Any]:
    import httpx

    url = req.endpoint.rstrip("/") + "/chat/completions"
    payload = {
        "model": req.model,
        "temperature": req.temperature,
        "max_tokens": req.max_tokens,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    start = time.monotonic()
    with httpx.Client(timeout=req.timeout) as client:
        response = client.post(
            url, json=payload, headers={"Authorization": f"Bearer {req.api_key}"}
        )
    elapsed = round(time.monotonic() - start, 3)
    if response.status_code != 200:
        # 只保留状态码与错误类型,不回显可能含凭据的响应体
        body = response.text[:400]
        body = re.sub(r"sk-[A-Za-z0-9\-_]+", "<redacted>", body)
        raise QualityEvalError(f"model call failed status={response.status_code}: {body}")
    data = response.json()
    choices = data.get("choices") or []
    if not choices:
        raise QualityEvalError("model returned no choices")
    choice = choices[0]
    message = choice.get("message") or {}
    text = message.get("content")
    if not isinstance(text, str) or not text.strip():
        raise QualityEvalError("model returned empty text")
    usage = data.get("usage") or {}
    return {
        "text": text,
        "elapsed_seconds": elapsed,
        "model_returned": data.get("model"),
        "finish_reason": choice.get("finish_reason"),
        "input_tokens": usage.get("prompt_tokens"),
        "output_tokens": usage.get("completion_tokens"),
    }


def _prose_guard_status(text: str) -> str:
    """复用生产门禁判断这段文字能不能算正文。

    网关可能用 HTTP 200 返回拒答话术、截断残稿或基础设施报错。这三种都必须
    当成失败重试,不能计入质量证据——否则验收数字会被"没写出来"的稿子污染。
    """
    from app.services.chapter_quality_gate import (
        looks_like_refusal,
        looks_like_relay_error,
        looks_truncated,
    )

    if not (text or "").strip():
        return "empty"
    if looks_like_relay_error(text):
        return "relay_error"
    if looks_like_refusal(text):
        return "refusal"
    if looks_truncated(text):
        return "truncated"
    return "ok"


def call_model_guarded(system: str, user: str, req: ModelRequest,
                       retries: int = 2) -> tuple[dict[str, Any], str, list[str]]:
    """调用模型并做正文门禁;不合格自动重试,返回最后一次的判定与尝试记录。"""
    attempts: list[str] = []
    last_status = "error"
    result: dict[str, Any] = {"text": ""}
    for _ in range(max(1, retries + 1)):
        result = call_model(system, user, req)
        status = _prose_guard_status(result["text"])
        attempts.append(status)
        last_status = status
        if status == "ok":
            return result, status, attempts
    return result, last_status, attempts


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")


def run(
    *,
    cases_path: str | Path,
    out_dir: str | Path,
    variants: tuple[str, ...],
    model: str,
    endpoint: str = DEFAULT_ENDPOINT,
    temperature: float = 0.3,
    max_tokens: int = 4096,
    timeout: float = 300.0,
    api_key: str | None = None,
    repeats: int = 1,
    retries: int = 2,
    only_case: str | None = None,
    snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    case_set: CaseSet = load_case_set(cases_path)
    out = Path(out_dir)
    if out.exists():
        raise QualityEvalError(f"refusing to overwrite existing output directory: {out}")
    key = resolve_api_key(api_key)
    out.mkdir(parents=True)
    cases_text = Path(cases_path).read_text(encoding="utf-8")
    (out / "cases.json").write_text(cases_text, encoding="utf-8")

    selected = [c for c in case_set.cases if only_case is None or c.id == only_case]
    if not selected:
        raise QualityEvalError(f"no case matched --only-case {only_case}")

    req = ModelRequest(endpoint=endpoint, model=model, api_key=key,
                       temperature=temperature, max_tokens=max_tokens, timeout=timeout)
    manifest: list[dict[str, Any]] = []
    detections: dict[str, Any] = {}
    errors: list[dict[str, Any]] = []

    for case in selected:
        for variant in variants:
            bundle = build_prompt(case, variant, snapshot=snapshot)
            for repeat in range(repeats):
                stem = f"{case.id}-{variant}" + (f"-r{repeat + 1}" if repeats > 1 else "")
                entry: dict[str, Any] = {
                    "case_id": case.id,
                    "genre": case.genre,
                    "variant": variant,
                    "repeat": repeat + 1,
                    "requested_model": model,
                    "endpoint": endpoint,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "case": case.to_metadata(),
                    "prompt": bundle.to_metadata(),
                    "snapshot_rev": (snapshot or {}).get("rev"),
                    "commit": _git_commit(),
                }
                try:
                    result, guard, attempts = call_model_guarded(
                        bundle.system, bundle.user, req, retries=retries)
                except Exception as exc:  # noqa: BLE001
                    entry.update(status="error", error_type=type(exc).__name__,
                                 attempts=[])
                    errors.append(entry)
                    _write_json(out / f"{stem}.request.json", entry)
                    manifest.append(entry)
                    continue
                if guard != "ok":
                    entry.update(status=guard, guard_status=guard, attempts=attempts,
                                 chars=len(result["text"]),
                                 finish_reason=result.get("finish_reason"),
                                 model_returned=result.get("model_returned"))
                    errors.append(entry)
                    _write_json(out / f"{stem}.request.json", entry)
                    manifest.append(entry)
                    continue
                entry["attempts"] = attempts
                text = result["text"]
                (out / f"{stem}.txt").write_text(text, encoding="utf-8")
                entry.update(
                    status="returned",
                    text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    chars=len(text),
                    elapsed_seconds=result["elapsed_seconds"],
                    model_returned=result["model_returned"],
                    finish_reason=result["finish_reason"],
                    input_tokens=result["input_tokens"],
                    output_tokens=result["output_tokens"],
                )
                detection = detectors.detect(case, text).to_dict()
                # detections.json 只保存纯函数的原始输出,保证"同一输入两次检测逐字一致"
                detections[stem] = detection
                # 请求元信息里保留提示词哈希与分块长度,不保存正文副本
                _write_json(out / f"{stem}.request.json", entry)
                manifest.append(entry)

    _write_json(out / "results.json", manifest)
    _write_json(out / "detections.json", detections)
    summary = summarize(manifest, detections)
    _write_json(out / "summary.json", summary)
    (out / "RUN.md").write_text(
        render_run_md(out, case_set, manifest, summary, endpoint, model, errors),
        encoding="utf-8",
    )
    return summary

def _stem_for(item: dict[str, Any]) -> str:
    stem = f"{item['case_id']}-{item['variant']}"
    if item.get("repeat", 1) > 1:
        stem += f"-r{item['repeat']}"
    return stem




def summarize(manifest: list[dict[str, Any]], detections: dict[str, Any]) -> dict[str, Any]:
    """按变体和题材汇总计数;只汇总,不做加权评分。"""
    by_variant: dict[str, dict[str, Any]] = {}
    by_genre: dict[str, dict[str, Any]] = {}
    totals: dict[str, int] = {name: 0 for name in detectors.CATEGORIES}
    hard_totals: dict[str, int] = {name: 0 for name in detectors.CATEGORIES}
    unmet: dict[str, int] = {}
    returned_index = {_stem_for(m): m for m in manifest if m.get("status") == "returned"}
    for stem, det in sorted(detections.items()):
        meta = returned_index.get(stem, {})
        variant = meta.get("variant", "unknown")
        genre = meta.get("genre", "unknown")
        for bucket, key in ((by_variant, variant), (by_genre, genre)):
            slot = bucket.setdefault(key, {"cases": 0, "counts": {n: 0 for n in detectors.CATEGORIES},
                                           "hard_counts": {n: 0 for n in detectors.CATEGORIES},
                                           "chars": 0})
            slot["cases"] += 1
            slot["chars"] += det["chars"]
            for name in detectors.CATEGORIES:
                slot["counts"][name] += det["counts"].get(name, 0)
                slot["hard_counts"][name] += det["hard_counts"].get(name, 0)
        for name in detectors.CATEGORIES:
            totals[name] += det["counts"].get(name, 0)
            hard_totals[name] += det["hard_counts"].get(name, 0)
        for group in ("required_outcomes", "must_keep", "forbidden"):
            for row in det["requirements"].get(group, []):
                if not row["ok"]:
                    unmet[f"{group}:{row['id']}"] = unmet.get(f"{group}:{row['id']}", 0) + 1

    returned = [m for m in manifest if m.get("status") == "returned"]
    failed = [m for m in manifest if m.get("status") != "returned"]
    return {
        "contract": production_contract_snapshot(),
        "cases": len({m["case_id"] for m in manifest}),
        "calls": len(manifest),
        "returned": len(returned),
        "failed": len(failed),
        "chars": sum(d["chars"] for d in detections.values()),
        "counts": totals,
        "hard_counts": hard_totals,
        "unmet": dict(sorted(unmet.items())),
        "by_variant": by_variant,
        "by_genre": by_genre,
        "tokens": {
            "input": sum(m.get("input_tokens") or 0 for m in returned),
            "output": sum(m.get("output_tokens") or 0 for m in returned),
        },
        "elapsed_seconds": round(sum(m.get("elapsed_seconds") or 0 for m in returned), 3),
    }


def render_run_md(out: Path, case_set: CaseSet, manifest: list[dict[str, Any]],
                  summary: dict[str, Any], endpoint: str, model: str,
                  errors: list[dict[str, Any]]) -> str:
    lines = [
        "# 质量验收运行记录(机器生成)",
        "",
        "本文件只陈述本次实际执行的事实。文学判断见同目录 `REVIEW.md`。",
        "",
        f"- 样本集:{case_set.version}({len(case_set.cases)} 个场景,{len({c.genre for c in case_set.cases})} 个题材)",
        f"- 网关:{endpoint}",
        f"- 模型:{model}",
        f"- 生产契约:{json.dumps(summary['contract'], ensure_ascii=False)}",
        f"- 调用:{summary['calls']} 次,成功 {summary['returned']},失败 {summary['failed']}",
        f"- 正文总字数:{summary['chars']}",
        f"- token 用量:输入 {summary['tokens']['input']},输出 {summary['tokens']['output']}",
        f"- 累计耗时:{summary['elapsed_seconds']} 秒",
        "",
        "## 确定性检测计数",
        "",
        "| 类别 | 命中数 | 其中 hard |",
        "|---|---|---|",
    ]
    for name in detectors.CATEGORIES:
        lines.append(f"| {name} | {summary['counts'][name]} | {summary['hard_counts'][name]} |")
    lines += ["", "## 未兑现的合同条目", ""]
    if summary["unmet"]:
        lines += ["| 条目 | 出现次数 |", "|---|---|"]
        lines += [f"| {k} | {v} |" for k, v in summary["unmet"].items()]
    else:
        lines.append("(无)")
    lines += ["", "## 按变体", "", "| 变体 | 场景数 | 字数 | hard 合计 |", "|---|---|---|---|"]
    for variant, slot in sorted(summary["by_variant"].items()):
        lines.append(
            f"| {variant} | {slot['cases']} | {slot['chars']} | {sum(slot['hard_counts'].values())} |"
        )
    lines += ["", "## 逐次调用", "",
              "| 产出 | 模型返回 | finish_reason | 字数 | 耗时(s) | 输出 token | 正文 sha256 |",
              "|---|---|---|---|---|---|---|"]
    for item in manifest:
        if item.get("status") != "returned":
            lines.append(f"| {item['case_id']}-{item['variant']} | - | - | - | - | - | 失败:"
                         f"{item.get('error_type')} |")
            continue
        lines.append(
            f"| {item['case_id']}-{item['variant']} | {item.get('model_returned')} | "
            f"{item.get('finish_reason')} | {item['chars']} | {item['elapsed_seconds']} | "
            f"{item.get('output_tokens')} | `{item['text_sha256'][:16]}` |"
        )
    if errors:
        lines += ["", "## 失败调用", ""]
        lines += [f"- {e['case_id']}-{e['variant']}:{e.get('error_type')}" for e in errors]
    lines.append("")
    return "\n".join(lines)


def detect_only(out_dir: str | Path, detect_dir: str | Path | None = None,
                compare_with: str | Path | None = None) -> dict[str, Any]:
    """对已有产出目录重跑检测,用于证明同一输入的检测结论一致。

    比较对象默认是同目录的 ``detections.json``;重跑时写入 ``detections.recalled.json``,
    因此不会覆盖原结果。检测是纯函数,任何差异都说明代码或输入被改动过。
    """
    out = Path(out_dir)
    if not (out / "cases.json").exists():
        raise QualityEvalError(f"{out} is not a quality_eval output directory")
    case_set = load_case_set(out / "cases.json")
    fresh: dict[str, Any] = {}
    for case in case_set.cases:
        for txt in sorted(out.glob(f"{case.id}-*.txt")):
            fresh[txt.stem] = detectors.detect(case, txt.read_text(encoding="utf-8")).to_dict()
    target = Path(detect_dir) if detect_dir else out / "detections.recalled.json"
    _write_json(target, fresh)
    reference = Path(compare_with) if compare_with else out / "detections.json"
    consistent = None
    diffs: list[str] = []
    if reference.exists():
        previous = json.loads(reference.read_text(encoding="utf-8"))
        consistent = json.dumps(previous, sort_keys=True) == json.dumps(fresh, sort_keys=True)
        if not consistent:
            diffs = sorted(set(previous) ^ set(fresh)) or ["same keys, different content"]
    return {"written": str(target), "compared_with": str(reference), "files": len(fresh),
            "consistent_with_previous": consistent, "diff_keys": diffs}


def rescore(out_dir: str | Path, name: str, cases_path: str | Path | None = None) -> dict[str, Any]:
    """用当前检测器(以及可选的新样本集)重新给已有正文打分,不重跑模型。

    正文不变、检测规则变了的场景必须走这里。新结果写成 ``detections.<name>.json`` /
    ``summary.<name>.json`` / ``cases.<name>.json``,原产物保持不动,两份都可查。
    """
    out = Path(out_dir)
    results = json.loads((out / "results.json").read_text(encoding="utf-8"))
    effective = Path(cases_path) if cases_path else out / "cases.json"
    case_set = load_case_set(effective)
    if effective.resolve() != (out / "cases.json").resolve():
        (out / f"cases.{name}.json").write_text(
            effective.read_text(encoding="utf-8"), encoding="utf-8")
    detections: dict[str, Any] = {}
    for case in case_set.cases:
        for txt in sorted(out.glob(f"{case.id}-*.txt")):
            detections[txt.stem] = detectors.detect(case, txt.read_text(encoding="utf-8")).to_dict()
    summary = summarize(results, detections)
    _write_json(out / f"detections.{name}.json", detections)
    _write_json(out / f"summary.{name}.json", summary)
    return {"detections": str(out / f"detections.{name}.json"),
            "summary": str(out / f"summary.{name}.json"),
            "counts": summary["counts"], "hard_counts": summary["hard_counts"],
            "unmet": summary["unmet"]}
