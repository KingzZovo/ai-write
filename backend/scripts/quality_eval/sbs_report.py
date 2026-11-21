"""把两次长跑逐章并排成一张可读的对照页(HTML)。

用途:比较"改参数前后"的同一本书 —— 左右两栏是同一章号的两次产出,顶部是逐章
字数/比值/时序词命中表。默认给阅读用,不做任何判定(juror 的工作交给 arm_judge)。

    python -m scripts.quality_eval.sbs_report \
        --old ../docs/evaluation/longrun-100-20261117 \
        --new ../docs/evaluation/longrun-100-095-20261117 \
        --out ../docs/evaluation/_public/index.html

只读两个目录里的 `chNN.txt` 与(若存在)`continuity.json`;页首横幅会写明当前
各自到第几章、是否跑完,以及两边的中位篇幅比,避免"看到一半的页面被当成结论"。
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import statistics

TARGET_FALLBACK = 1200

# 时段词与相对推进词:高亮用。相对推进词同时用于表里的"时序词"计数。
DAY_PARTS = ("清晨", "一早", "早上", "上午", "中午", "午后", "下午", "傍晚",
             "黄昏", "入夜", "夜里", "深夜", "凌晨", "天亮", "当晚", "当夜")
PROGRESS = ("次日", "翌日", "第二天", "隔天", "隔了一", "过了", "天后", "周后",
            "月后", "前几天", "几天前", "这些天", "头一天", "前一天", "昨天")


def load_chapters(directory: str) -> dict[int, str]:
    out: dict[int, str] = {}
    if not os.path.isdir(directory):
        return out
    for name in sorted(os.listdir(directory)):
        match = re.fullmatch(r"ch(\d+)\.txt", name)
        if match:
            with open(os.path.join(directory, name), encoding="utf-8") as handle:
                out[int(match.group(1))] = handle.read()
    return out


def load_titles(directory: str) -> dict[int, str]:
    """章标题:探针写出的 titles.json(chNN.txt -> 标题)。没有就返回空。"""
    path = os.path.join(directory, "titles.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except Exception:  # noqa: BLE001
        return {}
    out: dict[int, str] = {}
    for name, title in (raw or {}).items():
        match = re.fullmatch(r"ch(\d+)\.txt", str(name))
        if match and title:
            out[int(match.group(1))] = str(title)
    return out


def load_continuity(directory: str) -> dict | None:
    path = os.path.join(directory, "continuity.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:  # noqa: BLE001 - 报告工具不该因为坏文件失败
        return None


def targets(chapters: dict[int, str]) -> dict[int, int]:
    """每章目标字数:探针按 1200 字/章跑,这里保持同一口径。"""
    return {idx: TARGET_FALLBACK for idx in chapters}


def detected_target(directory: str) -> int | None:
    data = load_continuity(directory)
    if not data:
        return None
    for value in data.get("word_counts", {}).values():
        if isinstance(value, dict) and value.get("target"):
            return int(value["target"])
    return None


def ratio(text: str, target: int) -> float:
    return len(text) / target if target else 0.0


def progress_hits(text: str) -> int:
    return sum(text.count(word) for word in PROGRESS)


def render_prose(text: str) -> str:
    escaped = html.escape(text)
    for word in sorted(set(DAY_PARTS) | set(PROGRESS), key=len, reverse=True):
        escaped = escaped.replace(word, f"<mark>{word}</mark>")
    lines = []
    for para in escaped.split("\n"):
        if not para.strip():
            lines.append('<p class="gap"></p>')
        elif any(para.startswith(f"<mark>{word}</mark>") for word in PROGRESS):
            lines.append(f'<p class="scene">{para}</p>')
        else:
            lines.append(f"<p>{para}</p>")
    return "\n".join(lines)


def continuity_line(name: str, directory: str) -> str:
    data = load_continuity(directory)
    if not data:
        return f"{name}:检查器结果尚未生成(跑完才写 continuity.json)"
    counts: dict[str, int] = {}
    for finding in data.get("findings", []):
        counts[finding.get("check", "?")] = counts.get(finding.get("check", "?"), 0) + 1
    top = " · ".join(f"{k} {v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])[:6])
    return f"{name}:{data.get('violations', 0)} 条 findings({top or '无'})"


def load_ab(directory: str | None) -> dict | None:
    """读取 arm_judge 的产物;没有就返回 None(页面照旧)。"""
    if not directory:
        return None
    summary_path = os.path.join(directory, "summary.json")
    if not os.path.exists(summary_path):
        return None
    try:
        with open(summary_path, encoding="utf-8") as handle:
            summary = json.load(handle)
    except Exception:  # noqa: BLE001
        return None
    examples: list[dict] = []
    verdicts_path = os.path.join(directory, "verdicts.json")
    if os.path.exists(verdicts_path):
        try:
            with open(verdicts_path, encoding="utf-8") as handle:
                verdicts = json.load(handle)
            seen: set[str] = set()
            for row in verdicts.get("pairs", []):
                model = row.get("judge")
                if model in seen or not row.get("reason"):
                    continue
                seen.add(model)
                examples.append(row)
        except Exception:  # noqa: BLE001
            examples = []
    return {"summary": summary, "examples": examples, "dir": directory}


def render_ab(ab: dict | None) -> str:
    if not ab:
        return ""
    summary = ab["summary"]
    rows = "".join(
        f"<tr><td>{html.escape(str(j.get('model')))}</td><td>{j.get('a_wins', 0)}</td>"
        f"<td>{j.get('b_wins', 0)}</td><td>{j.get('ties', 0)}</td>"
        f"<td>{(1 - j.get('a_win_rate', 0)) * 100:.0f}%</td></tr>"
        for j in summary.get("judges", [])
    )
    agreement = summary.get("agreement")
    agreement_text = f"{agreement * 100:.0f}%(共同 {summary.get('compared_pairs', 0)} 章)" \
        if isinstance(agreement, (int, float)) else "只有单个评审,无法计算"
    examples = "".join(
        f"<li><b>第 {row.get('chapter')} 章 · {html.escape(str(row.get('judge')))}</b>"
        f"(选 {html.escape(str(row.get('preference')))}):{html.escape(str(row.get('reason')))}</li>"
        for row in ab["examples"]
    )
    return f"""
<section id="ab"><h2>双盲 A/B(两评审,同章对读)</h2>
<div class="banner" style="border-left-color:#17556b">
 <b>结论</b>:新配置在 20 对配对里胜出更多 —— 每行是新版的胜率。
 <br>章对 {summary.get('pairs', 0)} · 有效判定 {summary.get('judged', 0)} · 跳过 {summary.get('skipped', 0)}
 · 两评审一致率 {agreement_text}
 <br><span style="color:#8b5a00"><b>必须打住的偏差</b>:新版正文更长,评审可能偏好更长的文本;
 这组数字还不足以证明"文笔更好",只证明"在盲评里更常被选中"。</span>
</div>
<table style="max-width:560px"><thead><tr><th>评审</th><th>旧胜</th><th>新胜</th><th>平局</th><th>新版胜率</th></tr></thead>
<tbody>{rows}</tbody></table>
{f'<ul class="meta" style="margin-top:12px">{examples}</ul>' if examples else ''}
</section>"""


CSS = """
 body{font-family:system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
   margin:0;padding:24px;background:#f6f7f9;color:#1c2430}
 h1{font-size:20px;margin:0 0 6px}
 .banner{background:#fff;border:1px solid #d9dee5;border-left:4px solid #b54708;border-radius:8px;
   padding:12px 16px;margin:0 0 14px;font-size:13px;line-height:1.7}
 .banner.done{border-left-color:#067647}
 .meta{color:#5b6675;font-size:12.5px;margin-bottom:16px;line-height:1.8}
 .tablewrap{overflow:auto;max-height:44vh;border:1px solid #d9dee5;border-radius:8px;background:#fff;margin-bottom:22px}
 table{border-collapse:collapse;width:100%;font-size:12.5px;font-variant-numeric:tabular-nums}
 th,td{padding:5px 9px;border-bottom:1px solid #eef1f4;text-align:right;white-space:nowrap}
 th:first-child,td:first-child{text-align:left}
 thead th{position:sticky;top:0;background:#eef2f6;font-weight:600}
 tr:hover td{background:#f8fafc}
 td.bad{color:#b42318;font-weight:600} td.up{color:#067647} td.down{color:#b54708}
 .cols{display:grid;grid-template-columns:1fr 1fr;gap:16px}
 .col{background:#fff;border:1px solid #d9dee5;border-radius:8px;padding:14px 18px;
   font-family:Georgia,"Songti SC","Noto Serif CJK SC",serif;font-size:16px;line-height:1.95}
 .col h3{font-family:system-ui,sans-serif;font-size:12px;color:#5b6675;margin:0 0 10px;font-weight:600}
 .col.new{border-color:#b8d8c5} .col.old{border-color:#e3d3c7}
 p{margin:0 0 .55em;text-indent:2em} p.gap{height:.4em;margin:0}
 p.scene{border-left:3px solid #067647;padding-left:10px;text-indent:0}
 mark{background:#fff3b0;padding:0 1px}
 section{margin-bottom:22px} h2{font-size:15px;margin:0 0 8px}
 h2 a.top{font-size:12px;color:#8a94a2;text-decoration:none;margin-left:8px}
 nav.toc{font-size:12.5px;line-height:2;margin-bottom:18px;color:#5b6675}
 nav.toc a{display:inline-block;min-width:52px;color:#1c2430;text-decoration:none;border:1px solid #d9dee5;
   border-radius:6px;padding:0 6px;margin:0 4px 4px 0;background:#fff}
 @media(max-width:900px){.cols{grid-template-columns:1fr}}
"""


def build(old_dir: str, new_dir: str, *, old_label: str, new_label: str,
          ab_dir: str | None = None) -> str:
    old, new = load_chapters(old_dir), load_chapters(new_dir)
    old_target = detected_target(old_dir) or TARGET_FALLBACK
    new_target = detected_target(new_dir) or TARGET_FALLBACK
    titles = {**load_titles(old_dir), **load_titles(new_dir)}
    common = sorted(set(old) & set(new))
    rows, sections = [], []
    for idx in common:
        o, n = old[idx], new[idx]
        ro, rn = ratio(o, old_target), ratio(n, new_target)
        rows.append(
            f"<tr><td>ch{idx:02d}</td><td>{len(o)}</td>"
            f"<td class='{'bad' if ro < 0.85 else ''}'>{ro:.2f}</td>"
            f"<td>{len(n)}</td><td class='{'bad' if rn < 0.85 else ''}'>{rn:.2f}</td>"
            f"<td>{progress_hits(o)}</td><td>{progress_hits(n)}</td>"
            f"<td class='{'up' if rn > ro else 'down'}'>{rn - ro:+.2f}</td></tr>"
        )
        heading = f"第 {idx} 章"
        if titles.get(idx):
            heading += f" · {html.escape(titles[idx])}"
        sections.append(
            f'<section id="ch{idx}"><h2>{heading} <a class="top" href="#top">↑</a></h2>'
            f'<div class="cols"><div class="col old"><h3>{html.escape(old_label)}</h3>'
            f"{render_prose(o)}</div>"
            f'<div class="col new"><h3>{html.escape(new_label)}</h3>{render_prose(n)}</div>'
            "</div></section>"
        )

    ro_all = [ratio(old[i], old_target) for i in common]
    rn_all = [ratio(new[i], new_target) for i in common]
    finished = len(new) >= 100 and len(old) >= 100
    banner = (
        f"<b>{'两边都跑完了' if finished else '仍在跑'}</b>:"
        f"旧 {len(old)} 章 / 新 {len(new)} 章,已可对照 {len(common)} 章。"
        + (f" 左栏中位篇幅比 {statistics.median(ro_all):.3f} / 右栏 {statistics.median(rn_all):.3f};"
           f"低于 85% 目标:旧 {sum(r < 0.85 for r in ro_all)} 章 / 新 {sum(r < 0.85 for r in rn_all)} 章。"
           if common else "")
        + ("<br>跑完后本页会自动重生成,直接刷新即可。" if not finished else "")
    )
    toc = "".join(f'<a href="#ch{i}">{i}</a>' for i in common)

    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>新旧 100 章对照</title><style>{CSS}</style></head><body id="top">
<h1>新旧章节对照(逐章并排)</h1>
<div class="banner{' done' if finished else ''}">{banner}</div>
<table style="max-width:900px;margin-bottom:14px"><thead><tr>
<th>栏位</th><th>配置</th><th>目录</th><th>已有章数</th><th>目标字数</th></tr></thead><tbody>
<tr><td>左栏</td><td>{html.escape(old_label)}</td><td><code>{html.escape(os.path.basename(os.path.abspath(old_dir)))}</code></td>
<td>{len(old)}</td><td>{old_target}</td></tr>
<tr><td>右栏</td><td>{html.escape(new_label)}</td><td><code>{html.escape(os.path.basename(os.path.abspath(new_dir)))}</code></td>
<td>{len(new)}</td><td>{new_target}</td></tr>
</tbody></table>
<div class="meta">
 {html.escape(continuity_line('旧', old_dir))}<br>
 {html.escape(continuity_line('新', new_dir))}<br>
 <span style="color:#8a94a2">黄底=时间/时段词;绿色左边框=该段以声明的相对推进开头。字数比 = 本章字数 ÷ 该侧目标字数。</span>
</div>
<nav class="toc">跳转:<a href="#ab">A/B</a>{toc}</nav>
{render_ab(load_ab(ab_dir))}
<div class="tablewrap"><table><thead><tr>
<th>章</th><th>旧字数</th><th>旧比</th><th>新字数</th><th>新比</th>
<th>旧时序词</th><th>新时序词</th><th>比值变化</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></div>
{''.join(sections)}
</body></html>"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sbs_report", description="两次长跑的逐章并排对照页")
    parser.add_argument("--old", required=True)
    parser.add_argument("--new", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--ab", default=None, help="arm_judge 输出目录(有就渲染进页面)")
    parser.add_argument("--old-label", default="旧:0.85 阈值,无时间钉子")
    parser.add_argument("--new-label", default="新:0.95 阈值 + 时间钉子")
    args = parser.parse_args(argv)

    page = build(args.old, args.new, old_label=args.old_label, new_label=args.new_label,
                 ab_dir=args.ab)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        handle.write(page)
    print(f"wrote {args.out} ({len(page)} bytes)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())