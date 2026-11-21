"""用模型从正文里抽"金额 + 名目",用于跨章账目一致性核对。

纯函数(解析金额、区分单价/总额、比对计划值)**在产品层**:``app/services/numeric_ledger.py``,
这里只保留系统提示、模型调用与本机 CLI。产品代码(写后核对)与 A/B 计量必须用同一份实现,
不允许有第二套解析。

为什么不用正则:单价与总额同属一个名目,正则分不开。实测(2026-11-16)
`_nearby_amounts` 会把"每套校服二十八块"里的"二十八"当成总额,而工钱总额在另一句里,
名目还被抽成了"呢""的铺租""周芸"这类碎片。

本工具只做**测量**,不做自动改写。

实测限制(2026-11-16,`deepseek-chat`,`max_tokens=1500`):11 次调用里 **4 次返回空正文**
(推理吃满预算),所以 ``extract_amounts`` 带重试,调用方必须把空正文当失败而不是当"没有金额"。

CLI::

    python -m scripts.quality_eval.amounts --dir <run_dir> --chapters 7,8 \
        --total-keyword 工钱 [--planned "八百二十 八百"] \
        [--model deepseek-chat] [--max-tokens 3000] [--retries 2]
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from app.services.numeric_ledger import (
    AMOUNT_EXTRACTION_SYSTEM,
    conflicts,
    looks_like_total,  # noqa: F401  (供测试断言"与产品层同一实现")
    parse_amount,  # noqa: F401
    parse_amounts,
    planned_amounts_from_text,
    totals_for,
)

SYSTEM = AMOUNT_EXTRACTION_SYSTEM


def extract_amounts(client, *, model: str, text: str,
                    max_tokens: int = 3000, retries: int = 2) -> tuple[list[dict] | None, str]:
    """返回 (rows, raw)。rows 为 None 表示"重试后仍失败"。

    空正文与解析失败都要重试:实测推理模型在小 ``max_tokens`` 下会整段返回空,
    而"空"**不能**被当成"本章没有金额"。
    """
    last_raw = ""
    for _ in range(max(1, retries + 1)):
        resp = client.chat.completions.create(
            model=model, temperature=0, max_tokens=max_tokens,
            messages=[{"role": "system", "content": SYSTEM},
                      {"role": "user", "content": text}],
        )
        last_raw = (resp.choices[0].message.content or "").strip()
        rows = parse_amounts(last_raw)
        if rows is not None:
            return rows, last_raw
    return None, last_raw


def _read_chapter(run_dir: Path, idx: int) -> str | None:
    for pattern in (f"ch{idx:02d}.txt", f"ch{idx}.txt", f"*ch{idx:02d}*.txt"):
        for path in sorted(run_dir.glob(pattern)):
            text = path.read_text(encoding="utf-8")
            if text.strip():
                return text
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="模型抽取金额,核对跨章账目")
    parser.add_argument("--dir", required=True, help="运行目录(含 chNN.txt)")
    parser.add_argument("--chapters", default="7,8")
    parser.add_argument("--total-keyword", default="", help="只看名目含该词的总额")
    parser.add_argument("--planned", default="",
                        help="计划里已定的数额文本(如 \"八百二十 八百\");给出则列出冲突")
    parser.add_argument("--model", default="gemini-3.1-pro")
    parser.add_argument("--endpoint", default="http://127.0.0.1:8317/v1")
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--max-tokens", type=int, default=3000)
    parser.add_argument("--retries", type=int, default=2)
    args = parser.parse_args(argv)

    from openai import OpenAI

    api_key = args.api_key or os.environ.get("OPENAI_API_KEY") or os.environ.get("CPA_API_KEY")
    client = OpenAI(base_url=args.endpoint, api_key=api_key or "unset")

    planned = planned_amounts_from_text(args.planned) if args.planned else set()
    run_dir = Path(args.dir)
    indices = [int(x) for x in args.chapters.split(",") if x.strip()]
    failures = 0
    for idx in indices:
        text = _read_chapter(run_dir, idx)
        print(f"=== {run_dir.name} ch{idx:02d} chars={len(text) if text else 0}")
        if not text:
            print("    (无正文)")
            continue
        rows, raw = extract_amounts(client, model=args.model, text=text,
                                    max_tokens=args.max_tokens, retries=args.retries)
        if rows is None:
            failures += 1
            print("    抽取失败(重试后仍为空或无法解析):", raw[:200].replace("\n", " "))
            continue
        for row in rows:
            print(f"    [{row['item']}] {row['value']} -> {row['amount']} | {row['quote'][:40]}")
        if args.total_keyword:
            picked = totals_for(rows, args.total_keyword)
            print(f"    -> {args.total_keyword} 判定值: "
                  f"{[r['amount'] for r in picked] or '未给出'}")
        if args.planned:
            found = conflicts(rows, planned)
            print(f"    -> 与计划值 {sorted(planned)} 比对: "
                  f"{found if found else '无冲突'}")
    print(f"[summary] 抽取失败 {failures}/{len(indices)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
