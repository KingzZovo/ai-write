"""结果兑现的独立评审(不是词表)。

为什么需要它:`outcome_unfulfilled` 原先用"要求里的字面词/正则是否出现"判断,实测
假阳性率与效应量同阶(例如要求"姐姐留在岸上",正文写"周芸站在面棚边…她没有动",
词表判未兑现;要求"苏姐先处理阿六",正文写"吩咐道:把桌子收拾干净",词表也判未兑现)。
用这种指标比较两个 prompt 版本,比的是措辞习惯,不是交付率。

这里的做法:

- 换一个**不同的模型**当评审(默认 ``deepseek-v4.1-flash``,与生成模型不同);
- 只给它"本章必须完成的结果清单"和正文,**不告诉它这篇属于哪一组**;
- 要求逐条给出 ``delivered`` 与**逐字引用**;引用必须在正文中逐字出现,否则记为
  ``unverified``。判"已交付"却说不出原句的,一律不算交付。

于是得到两个口径(都报出来,不挑好看的):
- **strict**:``unverified`` 记为未交付(保守);
- **lenient**:``unverified`` 记为已交付(宽松)。

产物:``judged.<name>.json``(逐篇逐条判定 + 原句 + token 用量)。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from scripts.quality_eval.cases import load_case_set
from scripts.quality_eval.runner import DEFAULT_ENDPOINT, QualityEvalError, resolve_api_key

JUDGE_TASK = "required_outcome_judgement"

SYSTEM_PROMPT = """你是中文小说的校对员。你会拿到一段小说正文和一份"本场必须完成的结果"清单。
逐条判断**正文里是否真的把这件事写出来了**。

规则:
- 只看正文实际写了什么。打算、承诺、计划、暗示都不算完成;要在正文里找到已经发生的动作、已经说出的话或已经成立的状态。
- 只要意思到了就算完成,不要求出现清单里的原词。换了说法但事情发生了,算完成。
- 判"已完成"时,必须从正文里**直接复制连续的一段**作为依据:不改写、不概括、不加省略号、不拼接两处;连标点一起抄。说不出逐字依据的,判 false。
- 判 false 时,quote 写空字符串。
- 清单有 N 条,就必须返回 N 条,id 原样带回。

只输出 JSON 数组,不要解释、不要 Markdown 围栏:
[{"id": "清单里的 id", "delivered": true, "quote": "正文中逐字连续的一段"}, ...]"""


def _build_user(case: Any, text: str) -> str:
    items = "\n".join(
        f"- id: {row.id}\n  要求: {row.desc}" for row in case.required_outcomes)
    return (f"【本场必须完成的结果】\n{items}\n\n"
            f"【正文】\n{text}\n\n"
            f"请对上面 {len(case.required_outcomes)} 条逐条给出 delivered 与 quote,只输出 JSON 数组。")


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)

_KEEP_RE = re.compile(r"[^\u3400-\u4dbf\u4e00-\u9fffA-Za-z0-9]")
_MIN_LOCATABLE = 12


def _normalize(text: str) -> str:
    """只留下汉字/字母/数字。

    评审模型经常把全角引号、逗号写成半角,或者补一个省略号;这些都不改变"它指的是
    哪一段原文"。逐字相等在这种标点上太脆,所以标点一律不参与比较,内容字符顺序必须一致。
    """
    return _KEEP_RE.sub("", text or "")




def _longest_common_substring(a: str, b: str) -> int:
    """最长公共子串长度(滚动数组,输入都在千字级)。"""
    if not a or not b:
        return 0
    previous = [0] * (len(b) + 1)
    best = 0
    for i in range(1, len(a) + 1):
        current = [0] * (len(b) + 1)
        ai = a[i - 1]
        for j in range(1, len(b) + 1):
            if ai == b[j - 1]:
                current[j] = previous[j - 1] + 1
                if current[j] > best:
                    best = current[j]
        previous = current
    return best


def locate(quote: str, text: str) -> tuple[str, int]:
    """判断评审给的原句能不能在正文里定位。

    返回 (等级, 最长公共子串长度):
    - ``exact``      逐字包含;
    - ``normalized`` 去空白+标点归一后包含;
    - ``partial``    最长公共子串 ≥ 阈值(评审在同一段里抄了一部分);
    - ``unlocated``  其余(改写或编造)。
    分级而不是二值,是因为"逐字"在实测中连评审模型自己都做不到;但它指向哪一段是可验证的。
    """
    if not quote:
        return "unlocated", 0
    if quote in text:
        return "exact", len(quote)
    nq, nt = _normalize(quote), _normalize(text)
    if nq and nq in nt:
        return "normalized", len(nq)
    length = _longest_common_substring(nq, nt)
    return ("partial" if length >= _MIN_LOCATABLE else "unlocated"), length


def _escape_inner_quotes(text: str) -> str:
    """把 JSON 字符串值里**未转义**的引号补上转义。

    实测(2026-11-17,`claude-sonnet-4-6`):它会把人名对白里的引号原样写进 JSON 值,
    例如 ``"quote": "...低声道:"这回......真成穷光蛋了。"林叶没说话..."`` —— 分隔符位置合法、
    内容不合法,`json.loads` 直接 `JSONDecodeError`。6 篇里有 4 篇这样失败,
    评审工具本身把"换一个模型家族"变成了不可用。

    判定规则:字符串内的 ``"`` 只有在它后面(跳过空白)紧跟 ``,``/``}``/``]``/``:`` 时
    才算字符串结束;否则它一定是在值内部的引号,补转义。
    (``:`` 必须算结束符,否则对象键 ``"id":`` 会被误转义;值后面合法地跟 ``:`` 的情况不存在。)
    """
    out: list[str] = []
    in_string = False
    index = 0
    while index < len(text):
        char = text[index]
        if not in_string:
            out.append(char)
            if char == '"':
                in_string = True
            index += 1
            continue
        if char == "\\":
            out.append(text[index:index + 2])
            index += 2
            continue
        if char == '"':
            look = index + 1
            while look < len(text) and text[look] in " \t\r\n":
                look += 1
            if look < len(text) and text[look] in ',}]:':
                out.append(char)
                in_string = False
            else:
                out.append('\\"')
            index += 1
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _loads_lenient(text: str):
    """先按严格 JSON 解;失败再补转义内部引号解一次(不再成功就照原错误抛)。"""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return json.loads(_escape_inner_quotes(text))
def _parse(raw: str) -> list[dict[str, Any]]:
    text = _FENCE_RE.sub("", (raw or "").strip()).strip()
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if match:
        text = match.group(0)
    data = _loads_lenient(text)
    if not isinstance(data, list):
        raise ValueError("judge output is not a list")
    out = []
    for item in data:
        if not isinstance(item, dict) or "id" not in item or "delivered" not in item:
            raise ValueError("judge item missing id/delivered")
        delivered = item["delivered"]
        if not isinstance(delivered, bool):
            raise ValueError("delivered must be a boolean")
        quote = item.get("quote") or ""
        if not isinstance(quote, str):
            raise ValueError("quote must be a string")
        out.append({"id": str(item["id"]), "delivered": delivered, "quote": quote})
    return out


def _call(system: str, user: str, *, model: str, endpoint: str, api_key: str,
          timeout: float = 180.0) -> dict[str, Any]:
    import httpx

    payload = {"model": model, "temperature": 0.0, "max_tokens": 4000,
               "messages": [{"role": "system", "content": system},
                            {"role": "user", "content": user}]}
    started = time.monotonic()
    with httpx.Client(timeout=timeout) as client:
        response = client.post(endpoint.rstrip("/") + "/chat/completions", json=payload,
                               headers={"Authorization": f"Bearer {api_key}"})
    if response.status_code != 200:
        body = re.sub(r"sk-[A-Za-z0-9\-_]+", "<redacted>", response.text[:200])
        raise QualityEvalError(f"judge call failed status={response.status_code}: {body}")
    data = response.json()
    message = (data.get("choices") or [{}])[0].get("message") or {}
    text = message.get("content")
    if not isinstance(text, str) or not text.strip():
        raise QualityEvalError("judge returned empty text")
    usage = data.get("usage") or {}
    return {"text": text, "model": data.get("model"),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "input_tokens": usage.get("prompt_tokens"),
            "output_tokens": usage.get("completion_tokens")}


def judge_dir(*, run_dir: str | Path, cases_path: str | Path, name: str,
              model: str, endpoint: str = DEFAULT_ENDPOINT, api_key: str | None = None,
              only_case: str | None = None) -> dict[str, Any]:
    out = Path(run_dir)
    if not (out / "results.json").exists():
        raise QualityEvalError(f"{out} is not a quality_eval output directory")
    case_set = load_case_set(cases_path)
    key = api_key or resolve_api_key(None)
    records: list[dict[str, Any]] = []
    # 断点续判:已经判成功的篇目跳过。评审是逐篇独立的,中断后不必整批重来。
    done: set[str] = set()
    existing = out / f"judged.{name}.json"
    if existing.exists():
        try:
            for item in json.loads(existing.read_text(encoding="utf-8")):
                if item.get("status") == "ok":
                    done.add(item["stem"])
            records = [item for item in json.loads(existing.read_text(encoding="utf-8"))]
        except (OSError, json.JSONDecodeError):
            done, records = set(), []
    # 逐篇判定:评审只看到 case id 与正文,没有任何分组信息
    chapter_file = re.compile(r"^(?P<case>[a-z_]+)-scene_writer(?:-r\d+)?\.txt$")
    for path in sorted(out.glob("*.txt")):
        match = chapter_file.match(path.name)
        if not match:
            continue
        case_id = match.group("case")
        if case_id not in case_set.case_index:
            continue
        if only_case and case_id != only_case:
            continue
        case = case_set.by_id(case_id)
        stem = path.stem
        if stem in done:
            continue
        text = path.read_text(encoding="utf-8")
        entry: dict[str, Any] = {"stem": stem, "case_id": case_id,
                                 "requested_model": model, "chars": len(text)}
        try:
            result = _call(SYSTEM_PROMPT, _build_user(case, text), model=model,
                           endpoint=endpoint, api_key=key)
            parsed = _parse(result["text"])
        except Exception as exc:  # noqa: BLE001
            entry.update(status="error", error_type=type(exc).__name__,
                         error_message=str(exc)[:240])
            records.append(entry)
            (out / f"judged.{name}.json").write_text(
                json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
            continue
        by_id = {item["id"]: item for item in parsed}
        expected = [row.id for row in case.required_outcomes]
        verdicts = []
        for item_id in expected:
            item = by_id.get(item_id)
            if item is None:
                verdicts.append({"id": item_id, "delivered": False, "verified": False,
                                 "quote_ok": False, "reason": "judge_missing_item"})
                continue
            grade, common = locate(item["quote"], text)
            delivered = bool(item["delivered"])
            verdicts.append({
                "id": item_id,
                "delivered": delivered,
                # 判"已交付"必须指向正文里真实存在的一段;判未交付不需要引用
                "locatable": grade,
                "common_substring": common,
                "verified": (not delivered) or grade in ("exact", "normalized", "partial"),
                "quote": item["quote"][:120],
            })
        entry.update(
            status="ok",
            model_returned=result["model"],
            elapsed_seconds=result["elapsed_seconds"],
            output_tokens=result["output_tokens"],
            verdicts=verdicts,
            delivered=sum(1 for v in verdicts if v["delivered"] and v["verified"]),
            unverified=sum(1 for v in verdicts if v["delivered"] and not v["verified"]),
            missing=sum(1 for v in verdicts if v.get("reason") == "judge_missing_item"),
            items=len(expected),
        )
        records.append(entry)
        (out / f"judged.{name}.json").write_text(
            json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({k: entry[k] for k in
                          ("stem", "status", "items", "delivered", "unverified", "missing")},
                         ensure_ascii=False), flush=True)
    return {"records": len(records),
            "ok": sum(1 for r in records if r.get("status") == "ok"),
            "delivered": sum(r.get("delivered", 0) for r in records),
            "items": sum(r.get("items", 0) for r in records)}


def summarize(run_dir: str | Path, name: str) -> dict[str, Any]:
    records = json.loads((Path(run_dir) / f"judged.{name}.json").read_text(encoding="utf-8"))
    ok = [r for r in records if r.get("status") == "ok"]
    items = sum(r["items"] for r in ok)
    delivered = sum(r["delivered"] for r in ok)
    unverified = sum(r["unverified"] for r in ok)
    grades = {}
    for record in ok:
        for verdict in record["verdicts"]:
            if verdict["delivered"]:
                grades[verdict["locatable"]] = grades.get(verdict["locatable"], 0) + 1
    return {
        "documents": len(records),
        "judged_documents": len(ok),
        "failed_documents": len(records) - len(ok),
        "items": items,
        "delivered": delivered,
        "delivered_verified": delivered - unverified,
        "unverified_delivered": unverified,
        "not_delivered": items - delivered,
        "locatable_grades": grades,
        "delivered_rate": round(delivered / items, 4) if items else None,
        "verified_delivered_rate": round((delivered - unverified) / items, 4) if items else None,
    }
