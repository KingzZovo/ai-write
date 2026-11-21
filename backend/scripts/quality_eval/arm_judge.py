"""双盲 A/B 评审:比较两套生成配置产出的**同一批章节**,判断改参数后正文是不是更好。

为什么需要它:

- 已有的 ``judge.py`` 判的是"结果是否交付"(对着清单),``cross_judge.py`` 比的是两个评审
  在**同一批稿**上的一致率。两者都回答不了"换了参数之后哪一版正文更好"。
- 直接问模型"这篇和那篇哪个好"会把**位置**当信号(评审模型普遍偏爱第一篇),所以每对、每个
  评审都独立随机化两篇的呈现顺序,并把顺序记下来用于还原。
- 单个评审模型的分歧可能只是偏好。两(或更多)个模型各判一遍,才谈得上"结论不是某个模型的
  口味"。

做法:

1. 从 ``--a``/``--b`` 两个长跑产物目录取**同一章号**的正文(``ch01.txt``…),只保留两边都有的章;
2. 每对、每个评审用 ``sha256(seed|章号|评审名)`` 派生一个可复现的掷硬币,决定哪一臂当"文稿一";
3. 发给评审的只有两篇文稿,标注为【文稿一】【文稿二】,**不含任何配置名、参数、臂标签**;
4. 评审必须回 JSON:``{"preference": "1"|"2"|"tie", "reason": "...", "evidence": "..."}``;
   解析复用 ``judge._loads_lenient``(严格 JSON 优先,失败再补转义内部引号);解析失败计入
   ``skipped`` 并继续,不让一条坏输出毁掉整批;
5. 证据片段再用 ``judge.locate`` 回到胜出稿里定位一次 —— 判"这篇更好"却说不出原句的,证据等级
   记 ``unlocated``,不篡改偏好、只标注。

产物(``--out``):

- ``verdicts.json``:逐 (章, 评审) 判定 + 解析失败清单 + 汇总;
- ``summary.json``:每个评审的胜负/胜率、两评审一致率;
- ``REVIEW.md``:中文摘要表 + 分歧最多的 3 章(带双方理由原文)。

用法::

    python -m scripts.quality_eval.arm_judge \\
        --a ../docs/evaluation/longrun-100-20261117 \\
        --b ../docs/evaluation/longrun-100-<改参后> \\
        --out ../docs/evaluation/ab-20261118 \\
        --sample 20 --judges deepseek-chat,claude-sonnet-4-6 --seed 17

    # 完全不联网,用内置假文本走完整流程
    python -m scripts.quality_eval.arm_judge --out /tmp/ab-dry --dry-run

也可经单一入口:``python -m scripts.quality_eval arm-judge --a ... --b ... --out ...``

调用量:``章对数 × 评审数``。``--dry-run`` 为 0。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import sys
from pathlib import Path
from typing import Any, Sequence

from scripts.quality_eval.judge import _loads_lenient, locate
from scripts.quality_eval.runner import DEFAULT_ENDPOINT, QualityEvalError, resolve_api_key

DRY_RUN_JUDGES = ("dry-judge-a", "dry-judge-b")

SYSTEM_PROMPT = """你是中文小说的编辑。你会拿到同一部小说的同一章、在两个不同生成条件下
写出的两篇候选正文,标注为【文稿一】和【文稿二】。你的任务是比较这两篇正文本身。

不许猜测、不许询问它们分别来自哪个版本或哪套参数;你也不会被告知。

判断标准(按重要性):
1. 这一章作为小说章节是否成立:场景具体、人物行动有因果,而不是概述或流水账;
2. 人物的语言与动作是否和其所处情境一致,有没有明显的常识/时间/空间错误;
3. 收束是否落在具体的一刻(动作、对话、决定),而不是"接下来还有更多"的空话;
4. 语言:句子是否具体、有画面,是否有明显重复、翻译腔、总结式旁白。

规则:
- 必须选一篇,或者判平局(两篇在以上标准上确实难分高下)。平局只给"分不出来"的情况,不要
  为了礼貌平均分配。
- ``evidence`` 必须是**胜出那篇里直接复制的一段连续原文**:不改写、不概括、不拼接、连标点一起抄。
  判平局时 ``evidence`` 写空字符串。
- ``reason`` 一句话,说清具体差异(哪一处、差在哪),不要复述标准。

只输出 JSON 对象,不要解释、不要 Markdown 围栏:
{"preference": "1" 或 "2" 或 "tie", "reason": "一句话", "evidence": "胜出稿中的连续原文"}"""


# ---------------------------------------------------------------------------
# 顺序随机化:可复现,且不从产物里泄漏臂信息
# ---------------------------------------------------------------------------

def order_for(seed: int, chapter: int, judge: str) -> tuple[str, str]:
    """返回 ``(文稿一对应的臂, 文稿二对应的臂)``。

    用 ``sha256(f"{seed}|{chapter}|{judge}")`` 而不是 ``random.seed(seed+chapter)``:
    章号相加会碰撞(1+3 == 2+2),哈希把三者当整体,且不依赖进程内随机状态,
    同 seed 重跑逐字一致。
    """
    digest = hashlib.sha256(f"{seed}|{chapter}|{judge}".encode("utf-8")).hexdigest()
    rng = random.Random(int(digest[:16], 16))
    return ("A", "B") if rng.randrange(2) == 0 else ("B", "A")


def map_preference(preference: str, order: tuple[str, str]) -> str:
    """把评审的 ``"1"/"2"/"tie"`` 映射回 ``"A"/"B"/"tie"``。"""
    if preference == "tie":
        return "tie"
    if preference == "1":
        return order[0]
    if preference == "2":
        return order[1]
    raise ValueError(f"unknown preference {preference!r}")


# ---------------------------------------------------------------------------
# 章节配对
# ---------------------------------------------------------------------------

_CHAPTER_RE = re.compile(r"^ch(\d+)\.txt$", re.IGNORECASE)


def chapter_texts(directory: str | Path) -> dict[int, str]:
    """``章号 -> 正文``,只认 ``chNN.txt``(与 compare_arms/longrun 产物一致)。"""
    root = Path(directory)
    if not root.is_dir():
        raise QualityEvalError(f"{root} 不是目录")
    out: dict[int, str] = {}
    for path in sorted(root.glob("ch*.txt")):
        match = _CHAPTER_RE.match(path.name)
        if not match:
            continue
        try:
            out[int(match.group(1))] = path.read_text(encoding="utf-8")
        except OSError as exc:  # 读不到就停,不把缺章当成"两边都没有"
            raise QualityEvalError(f"读取失败 {path}: {exc}") from None
    return out


def pair_chapters(dir_a: str | Path, dir_b: str | Path,
                  chapters: Sequence[int] | None = None,
                  sample: int = 20) -> list[tuple[int, str, str]]:
    """只取**两边都有**的章号;给出 ``chapters`` 时按它过滤,否则取前 ``sample`` 对。

    空正文(长跑里出错章会留空文件)不算"有",否则评审会被要求比较两段空文本。
    """
    texts_a = chapter_texts(dir_a)
    texts_b = chapter_texts(dir_b)
    common = sorted(
        index for index in set(texts_a) & set(texts_b)
        if (texts_a[index] or "").strip() and (texts_b[index] or "").strip()
    )
    if chapters:
        wanted = {int(item) for item in chapters}
        common = [index for index in common if index in wanted]
    else:
        common = common[:max(0, int(sample))]
    return [(index, texts_a[index], texts_b[index]) for index in common]


# ---------------------------------------------------------------------------
# 评审输出解析(容错)
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)
_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)

_TIE_WORDS = {"tie", "平", "平局", "打平", "平手", "不分上下", "same", "equal", "draw"}
_ONE_WORDS = {"1", "一", "文稿一", "第一篇", "前者", "first", "a"}
_TWO_WORDS = {"2", "二", "文稿二", "第二篇", "后者", "second", "b"}


def normalize_preference(value: Any) -> str:
    """把评审实际写出来的东西收敛成 ``"1"/"2"/"tie"``;认不出就抛 ``ValueError``。

    实测评审会写 ``1``(数字)、``"文稿一"``、``"tie"``;这些都不改变它指的是哪一篇。
    """
    if isinstance(value, bool):
        raise ValueError(f"preference must be 1/2/tie, got {value!r}")
    if isinstance(value, int):
        value = str(value)
    if not isinstance(value, str):
        raise ValueError(f"preference must be 1/2/tie, got {type(value).__name__}")
    text = value.strip().lower().strip("。.!！?？\"'“”「」")
    if text in _TIE_WORDS:
        return "tie"
    if text in _ONE_WORDS:
        return "1"
    if text in _TWO_WORDS:
        return "2"
    raise ValueError(f"unrecognized preference {value!r}")


def parse_verdict(raw: str) -> dict[str, Any]:
    """解析评审输出。``{"preference","reason","evidence"}``,失败抛异常由调用方计入 skipped。

    解析顺序与 ``judge._parse`` 一致:去 Markdown 围栏 → 截取 JSON 对象 → 严格 ``json.loads``
    → 失败再用 ``judge._loads_lenient`` 补转义内部引号(评审会把对白引号原样写进 JSON 值)。
    """
    text = _FENCE_RE.sub("", (raw or "").strip()).strip()
    match = _OBJECT_RE.search(text)
    if match:
        text = match.group(0)
    data = _loads_lenient(text)
    if not isinstance(data, dict):
        raise ValueError("judge output is not a JSON object")
    preference = normalize_preference(data.get("preference"))
    reason = data.get("reason") or ""
    evidence = data.get("evidence") or ""
    if not isinstance(reason, str) or not isinstance(evidence, str):
        raise ValueError("reason/evidence must be strings")
    return {"preference": preference,
            "reason": " ".join(reason.split())[:300],
            "evidence": evidence.strip()[:300]}


# ---------------------------------------------------------------------------
# 提示词:只给正文,不给任何臂/参数信息
# ---------------------------------------------------------------------------

def build_user(text_one: str, text_two: str) -> str:
    return (f"【文稿一】\n{text_one}\n\n"
            f"【文稿二】\n{text_two}\n\n"
            f"请判断这两篇正文哪一篇更好,只输出 JSON 对象"
            f"({{\"preference\": \"1\" | \"2\" | \"tie\", \"reason\": \"一句话\", "
            f"\"evidence\": \"胜出稿中的连续原文\"}})。")


# ---------------------------------------------------------------------------
# 调用
# ---------------------------------------------------------------------------

def _call(system: str, user: str, *, model: str, endpoint: str, api_key: str,
          max_tokens: int = 1200, timeout: float = 180.0) -> str:
    """一次评审调用,返回模型文本。与 ``judge._call`` 同形,只多 ``max_tokens`` 参数。

    ``import httpx`` 放在函数里:本模块在 ``--dry-run`` 与单测里必须能在无网络环境下导入。
    """
    import httpx

    payload = {"model": model, "temperature": 0.0, "max_tokens": int(max_tokens),
               "messages": [{"role": "system", "content": system},
                            {"role": "user", "content": user}]}
    url = endpoint.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}"}
    with httpx.Client(timeout=timeout) as client:
        response = client.post(url, json=payload, headers=headers)
        # 2026-11-19:部分新模型(实测 claude-sonnet-5.5)拒绝 temperature 参数
        # ("temperature: Extra inputs are not permitted")。评审要的是同一份提示词与
        # 同一批稿,采样温度不是判据,所以自动去掉该字段重试一次,而不是让整批评审
        # 因一个可选字段全废。
        if response.status_code == 400 and "temperature" in response.text:
            payload.pop("temperature", None)
            response = client.post(url, json=payload, headers=headers)
    if response.status_code != 200:
        body = re.sub(r"sk-[A-Za-z0-9\-_]+", "<redacted>", response.text[:400])
        raise QualityEvalError(f"judge call failed status={response.status_code}: {body}")
    data = response.json()
    message = (data.get("choices") or [{}])[0].get("message") or {}
    text = message.get("content")
    if not isinstance(text, str) or not text.strip():
        raise QualityEvalError("judge returned empty text")
    return text


# ---------------------------------------------------------------------------
# dry-run:内置两段假文本 + 假响应,完全不联网
# ---------------------------------------------------------------------------

DRY_TEXT_A = """雨是从后半夜开始下的。周芸把最后一盏油灯的芯剪短，火苗缩成豆子大。
她数了两遍抽屉里的钱，两千一，差的那三百还压在渡口的账上。她把钱分作两叠，一叠塞进
棉袄内袋，一叠留在抽屉里。

“明天一早船就来。”她说给自己听。

门外有人磕掉鞋底的泥。她没开门，只把手按在抽屉上，等那阵脚步声过去。雨声反而更清楚了，
像是有人在屋顶上一粒一粒地数沙子。她想起白天那句话——船票要涨，涨了就没人肯坐渡船。
"""

DRY_TEXT_B = """后半夜落了雨。许舟把船绳又绕了一圈,绳子吃水,勒得手心发红。
他听见岸上有灯灭了一盏。灭灯的地方是面棚,苏姐每晚最后收的就是那盏灯。
许舟低声道:"这回真成穷光蛋了。"林叶没说话,只把一个布包推过来,包角还沾着面。
布包里是三百块,不多不少,正好够补上渡口那笔账。他捏着钱,忽然不知道要不要道谢。
雨打在船篷上,一声接一声,他数到第十七声才开口。"""


def _dry_evidence(text: str) -> str:
    """从假文本里抄一段连续原文当证据(``locate`` 会判成 exact)。"""
    for line in (text or "").splitlines():
        line = line.strip()
        if len(line) >= 12:
            return line[:24]
    return (text or "").strip()[:24]


def _dry_raw(first_text: str, second_text: str, preference: str) -> str:
    """构造假响应。偏好 ``"1"`` 的那条故意把对白引号写成未转义(实测评审就这样),走容错路径。"""
    if preference == "1":
        evidence = _dry_evidence(first_text).replace("\n", " ")
        return ('{"preference": "1", "reason": "把"守着渡口"这一步写在明面上,因果清楚", '
                '"evidence": "' + evidence + '"}')
    evidence = _dry_evidence(second_text)
    return json.dumps({"preference": "2", "reason": "第二篇的冲突落得更早,收束在具体动作上",
                       "evidence": evidence}, ensure_ascii=False)


def _dry_pairs() -> list[tuple[int, str, str]]:
    return [(1, DRY_TEXT_A, DRY_TEXT_B)]


def _dry_preference(judge: str) -> str:
    # 评审名里字符和的奇偶决定偏好:默认两个 dry-judge 会给出相反的判定,顺带演示分歧表。
    return "1" if sum(map(ord, judge)) % 2 == 0 else "2"


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------

def _judge_stats(rows: list[dict], judges: Sequence[str], skipped: list[dict]) -> list[dict]:
    stats = []
    for judge in judges:
        mine = [row for row in rows if row["judge"] == judge]
        a_wins = sum(1 for row in mine if row["preference"] == "A")
        b_wins = sum(1 for row in mine if row["preference"] == "B")
        ties = sum(1 for row in mine if row["preference"] == "tie")
        decided = a_wins + b_wins
        stats.append({
            "model": judge,
            "judged": len(mine),
            "a_wins": a_wins,
            "b_wins": b_wins,
            "ties": ties,
            # 胜率不含平局:平局既不是 A 赢也不是 B 赢,混进分母会把"分不出来"读成"打平即各半"
            "a_win_rate": round(a_wins / decided, 4) if decided else None,
            "tie_rate": round(ties / len(mine), 4) if mine else None,
            "skipped": sum(1 for item in skipped if item["judge"] == judge),
            "evidence_unlocated": sum(1 for row in mine if row.get("evidence_grade") == "unlocated"),
        })
    return stats


def _preferences_by_chapter(rows: list[dict]) -> dict[int, dict[str, str]]:
    out: dict[int, dict[str, str]] = {}
    for row in rows:
        out.setdefault(row["chapter"], {})[row["judge"]] = row["preference"]
    return out


def agreement(rows: list[dict], judges: Sequence[str]) -> dict[str, Any]:
    """两个(或更多)评审的一致率。

    - 只统计**所有评审都判过**的章对:少判一章是覆盖率差异,不是分歧(与 cross_judge 的口径一致);
    - 双方都判"平局"算一致;
    - 只有一个评审判过的章对不计入分母,单独报 ``incomplete_pairs``。
    """
    by_chapter = _preferences_by_chapter(rows)
    if len(judges) < 2:
        # 一个评审的"一致率"恒为 1,没有信息量;宁可报 None 也不给一个假的 100%。
        return {"agreement": None, "agreed_pairs": 0, "compared_pairs": 0,
                "incomplete_pairs": [], "pairwise": {}}
    complete = {chapter: prefs for chapter, prefs in by_chapter.items()
                if all(judge in prefs for judge in judges)}
    agreed = [chapter for chapter, prefs in complete.items()
              if len(set(prefs[judge] for judge in judges)) == 1]
    result: dict[str, Any] = {
        "agreement": round(len(agreed) / len(complete), 4) if complete else None,
        "agreed_pairs": len(agreed),
        "compared_pairs": len(complete),
        "incomplete_pairs": sorted(chapter for chapter in by_chapter
                                   if chapter not in complete),
        "pairwise": {},
    }
    for left_index, left in enumerate(judges):
        for right in judges[left_index + 1:]:
            common = [chapter for chapter, prefs in by_chapter.items()
                      if left in prefs and right in prefs]
            same = [chapter for chapter in common
                    if by_chapter[chapter][left] == by_chapter[chapter][right]]
            result["pairwise"][f"{left}|{right}"] = {
                "compared": len(common),
                "agreed": len(same),
                "rate": round(len(same) / len(common), 4) if common else None,
            }
    return result


def summarize(rows: list[dict], judges: Sequence[str], skipped: list[dict]) -> dict[str, Any]:
    """``summary.json`` 的主体。``pairs`` 是**有判定结果**的章对数,不是两目录的交集大小。"""
    agreed = agreement(rows, judges)
    return {
        "pairs": len({row["chapter"] for row in rows}),
        "judged": len(rows),
        "skipped": len(skipped),
        "judges": _judge_stats(rows, judges, skipped),
        "agreement": agreed["agreement"],
        "agreed_pairs": agreed["agreed_pairs"],
        "compared_pairs": agreed["compared_pairs"],
        "incomplete_pairs": agreed["incomplete_pairs"],
        "pairwise": agreed["pairwise"],
    }


# ---------------------------------------------------------------------------
# REVIEW.md
# ---------------------------------------------------------------------------

def _fmt_rate(value: Any) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def disputed_chapters(rows: list[dict], limit: int = 3) -> list[dict]:
    """按"分歧人数"排出最值得人看的那几章;同分按章号升序(保证报告可复现)。"""
    by_chapter = _preferences_by_chapter(rows)
    out = []
    for chapter, prefs in by_chapter.items():
        if len(set(prefs.values())) <= 1:
            continue
        out.append({"chapter": chapter,
                    "verdicts": [row for row in rows if row["chapter"] == chapter],
                    "spread": len(set(prefs.values()))})
    out.sort(key=lambda item: (-item["spread"], item["chapter"]))
    return out[:limit]


def render_review(*, rows: list[dict], skipped: list[dict], summary: dict[str, Any],
                  judges: Sequence[str], dir_a: str, dir_b: str,
                  chapters: Sequence[int], seed: int, dry_run: bool) -> str:
    lines = ["# 双盲 A/B 评审", ""]
    if dry_run:
        lines += ["> **dry-run**:正文为内置假文本、评审为假响应,不含任何真实模型调用。", ""]
    lines += [
        f"- 稿本 A:`{dir_a}`",
        f"- 稿本 B:`{dir_b}`",
        f"- 章对:{summary['pairs']} 个有判定(章号 {', '.join(str(c) for c in chapters) or '无'})",
        f"- 评审模型:{', '.join(judges)}",
        f"- 顺序随机化:seed={seed},按 `sha256(seed|章号|评审)` 派生,同 seed 可复现",
        f"- 解析失败跳过:{summary['skipped']} 次",
        "",
        "评审只看到【文稿一】【文稿二】两篇正文,不知道各自来自哪套配置,也不知道参数差异。",
        "",
        "## 每个评审的胜负",
        "",
        "| 评审模型 | A 胜 | B 胜 | 平局 | A 胜率(不含平局) | 平局率 | 证据未定位 | 解析失败 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for stats in summary["judges"]:
        lines.append(
            f"| {stats['model']} | {stats['a_wins']} | {stats['b_wins']} | {stats['ties']} | "
            f"{_fmt_rate(stats['a_win_rate'])} | {_fmt_rate(stats['tie_rate'])} | "
            f"{stats['evidence_unlocated']} | {stats['skipped']} |")

    lines += ["", "## 两个评审是否互相支持", ""]
    if len(judges) < 2:
        lines.append("只给了一个评审模型 —— 一致率无从计算,也无法区分\"正文更好\"和\"这个模型的口味\"。")
    elif summary["agreement"] is None:
        lines.append("没有任何一章被所有评审都判过,一致率不可计算。")
    else:
        lines.append(
            f"- 共同判定 {summary['compared_pairs']} 章,一致 {summary['agreed_pairs']} 章,"
            f"一致率 **{_fmt_rate(summary['agreement'])}**;")
        for key, stats in summary["pairwise"].items():
            left, right = key.split("|")
            lines.append(f"  - {left} vs {right}:共同 {stats['compared']} 章、"
                         f"一致 {stats['agreed']} 章({_fmt_rate(stats['rate'])})")
        if summary["incomplete_pairs"]:
            lines.append(f"- 只有部分评审判过(不计入一致率):章 "
                         f"{', '.join(str(c) for c in summary['incomplete_pairs'])}")
        if summary["agreement"] < 0.6 and summary["compared_pairs"] >= 5:
            lines.append("")
            lines.append("一致率偏低:**不能**把胜负当成结论,下面分歧章需要人读。")

    lines += ["", "## 分歧最多的章节", ""]
    disputes = disputed_chapters(rows)
    if not disputes:
        lines.append("没有分歧(或只有一个评审判过)。")
    for item in disputes:
        lines.append(f"### 第 {item['chapter']} 章")
        lines.append("")
        for row in item["verdicts"]:
            arm = {"A": "A 胜", "B": "B 胜", "tie": "平局"}.get(row["preference"], row["preference"])
            lines.append(f"- **{row['judge']}**:{arm} —— {row['reason'] or '(未给理由)'}")
            if row.get("evidence"):
                lines.append(f"  - 证据({row.get('evidence_grade') or 'n/a'}):{row['evidence']}")
        lines.append("")

    lines += ["", "## 逐章判定", "", "| 章 | 评审 | 判定 | 文稿一来自 | 证据等级 | 理由 |",
              "|---|---|---|---|---|---|"]
    for row in sorted(rows, key=lambda r: (r["chapter"], r["judge"])):
        arm = {"A": "A 胜", "B": "B 胜", "tie": "平局"}.get(row["preference"], row["preference"])
        reason = (row["reason"] or "").replace("|", "\\|")
        lines.append(f"| {row['chapter']} | {row['judge']} | {arm} | {row['arm_1']} | "
                     f"{row.get('evidence_grade') or 'n/a'} | {reason} |")

    if skipped:
        lines += ["", "## 解析失败(已跳过,不计入胜负)", "",
                  "| 章 | 评审 | 错误 |", "|---|---|---|"]
        for item in skipped:
            lines.append(f"| {item['chapter']} | {item['judge']} | "
                         f"{item.get('error_type', '')}: {item.get('error', '')} |")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def run(*, dir_a: str | None = None, dir_b: str | None = None, out_dir: str | Path,
        chapters: Sequence[int] | None = None, sample: int = 20,
        judges: Sequence[str] | None = None, seed: int = 17, max_tokens: int = 1200,
        dry_run: bool = False, endpoint: str = DEFAULT_ENDPOINT,
        api_key: str | None = None, timeout: float = 180.0) -> dict[str, Any]:
    out = Path(out_dir)
    names = list(judges or (DRY_RUN_JUDGES if dry_run else ()))
    if not names:
        raise QualityEvalError("需要 --judges(至少一个评审模型)")
    if dry_run:
        pairs = _dry_pairs()
        key = ""
    else:
        if not dir_a or not dir_b:
            raise QualityEvalError("需要 --a 与 --b 两个产物目录")
        pairs = pair_chapters(dir_a, dir_b, chapters=chapters, sample=sample)
        if not pairs:
            raise QualityEvalError(f"{dir_a} 与 {dir_b} 没有共同章号(或两边都是空正文)")
        key = resolve_api_key(api_key)

    rows: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for chapter, text_a, text_b in pairs:
        for judge in names:
            order = order_for(seed, chapter, judge)
            texts = {"A": text_a, "B": text_b}
            first, second = texts[order[0]], texts[order[1]]
            try:
                if dry_run:
                    raw = _dry_raw(first, second, _dry_preference(judge))
                else:
                    raw = _call(SYSTEM_PROMPT, build_user(first, second), model=judge,
                                endpoint=endpoint, api_key=key, max_tokens=max_tokens,
                                timeout=timeout)
                verdict = parse_verdict(raw)
            except Exception as exc:  # noqa: BLE001 —— 一条坏输出不能毁掉整批
                skipped.append({"chapter": chapter, "judge": judge,
                                "error_type": type(exc).__name__,
                                "error": str(exc)[:240]})
                print(json.dumps({"chapter": chapter, "judge": judge, "status": "skipped",
                                  "error": str(exc)[:120]}, ensure_ascii=False), flush=True)
                continue
            preference = map_preference(verdict["preference"], order)
            winner = text_a if preference == "A" else text_b if preference == "B" else None
            grade = locate(verdict["evidence"], winner)[0] if winner else "n/a"
            row = {"chapter": chapter, "judge": judge, "arm_1": order[0],
                   "preference": preference, "reason": verdict["reason"],
                   "evidence": verdict["evidence"], "evidence_grade": grade}
            rows.append(row)
            print(json.dumps({key_: row[key_] for key_ in
                              ("chapter", "judge", "preference", "evidence_grade")},
                             ensure_ascii=False), flush=True)

    summary = summarize(rows, names, skipped)
    out.mkdir(parents=True, exist_ok=True)
    (out / "verdicts.json").write_text(
        json.dumps({"pairs": rows, "skipped": skipped, "summary": summary},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "REVIEW.md").write_text(
        render_review(rows=rows, skipped=skipped, summary=summary, judges=names,
                      dir_a=str(dir_a or "(dry-run 内置文本)"),
                      dir_b=str(dir_b or "(dry-run 内置文本)"),
                      chapters=[chapter for chapter, _, _ in pairs], seed=seed,
                      dry_run=dry_run),
        encoding="utf-8")
    return {"out": str(out), "pairs": summary["pairs"], "judged": len(rows),
            "skipped": len(skipped), "calls": 0 if dry_run else len(rows) + len(skipped),
            "summary": summary}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="双盲 A/B 评审(两套配置产出同一批章)")
    parser.add_argument("--a", dest="dir_a", default=None, help="稿本 A 的产物目录(chNN.txt)")
    parser.add_argument("--b", dest="dir_b", default=None, help="稿本 B 的产物目录(chNN.txt)")
    parser.add_argument("--out", required=True, help="输出目录(verdicts.json/summary.json/REVIEW.md)")
    parser.add_argument("--chapters", default=None, help="只判这些章号,例 1,2,3")
    parser.add_argument("--sample", type=int, default=20, help="两目录共有章号里取前 N 对(默认 20)")
    parser.add_argument("--judges", default=None, help="评审模型,逗号分隔;dry-run 默认两个假评审")
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--max-tokens", type=int, default=1200)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--dry-run", action="store_true",
                        help="不联网:内置两段假文本 + 假响应,跑完整流程")
    args = parser.parse_args(argv)

    chapters = None
    if args.chapters:
        try:
            chapters = [int(item) for item in args.chapters.split(",") if item.strip()]
        except ValueError:
            raise SystemExit("--chapters 必须是逗号分隔的整数,例 1,2,3") from None
    judges = [item.strip() for item in (args.judges or "").split(",") if item.strip()] or None
    try:
        result = run(dir_a=args.dir_a, dir_b=args.dir_b, out_dir=args.out,
                     chapters=chapters, sample=args.sample, judges=judges, seed=args.seed,
                     max_tokens=args.max_tokens, dry_run=args.dry_run,
                     endpoint=args.endpoint, api_key=args.api_key, timeout=args.timeout)
    except QualityEvalError as exc:
        print(f"arm_judge failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({key: result[key] for key in ("out", "pairs", "judged", "skipped", "calls")},
                     ensure_ascii=False))
    for stats in result["summary"]["judges"]:
        print(f"[{stats['model']}] A胜={stats['a_wins']} B胜={stats['b_wins']} "
              f"平局={stats['ties']} A胜率={stats['a_win_rate']}")
    print(f"一致率={result['summary']['agreement']} "
          f"(共同 {result['summary']['compared_pairs']} 章)")
    return 0


if __name__ == "__main__":
    sys.exit(main())