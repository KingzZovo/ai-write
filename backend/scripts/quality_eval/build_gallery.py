#!/usr/bin/env python3
"""把所有长跑产物整理成一个可浏览、可筛选、可并排对比的总页面。

用户视角:一句话说清"哪个题材、哪一版、哪个模型,写出来是什么样"。
所以页面按三种维度筛选 —— **题材 / 新旧版 / 模型** —— 每张卡片给统计,
点开能逐章读正文,两个跑批还能逐章左右并排。

用法::

    python -m scripts.quality_eval.build_gallery --out ../docs/evaluation/_public/gallery.html

产物是**自包含单文件**(数据内联,无外部依赖),可以直接被静态服务/隧道对外提供。
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

EVAL_ROOT = Path(__file__).resolve().parents[3] / "docs/evaluation"

# 显式登记要展示的跑批:目录、题材、模型、版本标签、配置说明。
# 目录不存在就跳过(还未跑完的批次不会让页面残缺)。
RUNS: list[dict] = [
    # ---- 题材一:现实生活《渡口旧账》 ----
    {"dir": "longrun-100-20261117", "genre": "现实生活 · 渡口旧账", "model": "gemini-3.1-pro",
     "version": "旧版", "config": "1200 字/章 · 篇幅修复前"},
    {"dir": "longrun-100-095-20261117", "genre": "现实生活 · 渡口旧账", "model": "gemini-3.1-pro",
     "version": "旧版", "config": "1200 字/章 · 场级续写阈值 0.95"},
    {"dir": "longrun-100-gemini-2000-20261117", "genre": "现实生活 · 渡口旧账", "model": "gemini-3.1-pro",
     "version": "2000 字档", "config": "目标 2000 · 超分配关 / 无补足"},
    {"dir": "longrun-10-gemini-2000-overalloc", "genre": "现实生活 · 渡口旧账", "model": "gemini-3.1-pro",
     "version": "2000 字档", "config": "10 章 · 只开超分配 1.08"},
    {"dir": "longrun-10-gemini-2000-topup", "genre": "现实生活 · 渡口旧账", "model": "gemini-3.1-pro",
     "version": "2000 字档", "config": "10 章 · 超分配 + 补足(停止线 1900)"},
    {"dir": "longrun-10-gemini-2000-topup100", "genre": "现实生活 · 渡口旧账", "model": "gemini-3.1-pro",
     "version": "新版", "config": "10 章 · 超分配 + 补足(停止线 2000)"},
    {"dir": "longrun-100-gemini-2000-final", "genre": "现实生活 · 渡口旧账", "model": "gemini-3.1-pro",
     "version": "新版", "config": "100 章 · 最终方案(超分配 1.08 + 补足到 2000 + 验收 0.95)"},
    {"dir": "longrun-100-dsv4flash-2000-20261117", "genre": "现实生活 · 渡口旧账", "model": "deepseek-v4-flash",
     "version": "2000 字档", "config": "16 章 · 关 thinking · 流式"},
    {"dir": "longrun-30-dsv4flash-2000-final", "genre": "现实生活 · 渡口旧账", "model": "deepseek-v4-flash",
     "version": "新版", "config": "29 章 · 最终方案 · 关 thinking · 流式"},
    {"dir": "arm-dsv4-thinking-20", "genre": "现实生活 · 渡口旧账", "model": "deepseek-v4-flash",
     "version": "新版", "config": "20 章 · 最终方案 · **开 thinking** · 非流式"},
    # ---- 题材二:都市悬疑《回声档案》 ----
    {"dir": "genre2-gemini-29", "genre": "都市悬疑 · 回声档案", "model": "gemini-3.1-pro",
     "version": "新版", "config": "29 章 · 最终方案"},
    {"dir": "genre2-dsv4-29", "genre": "都市悬疑 · 回声档案", "model": "deepseek-v4-flash",
     "version": "新版", "config": "29 章 · 最终方案 · 关 thinking · 流式"},
    # ---- 2026-11-20 自洽机制验证批 ----
    # 这两批用于验证"已确立事实注入 + 评审自洽性判据 + 确定性兜底"三层是否生效。
    # 现实生活题材有债务/约定线,事实链连续 20 章不断;都市悬疑题材没有这类内容,
    # 事实清单为空 —— 两批一起看才知道机制在"有料"与"无料"时的表现。
    {"dir": "verify-facts-20", "genre": "现实生活 · 渡口旧账", "model": "gemini-3.1-pro",
     "version": "自洽机制", "config": "20 章 · 写手注入已确立事实 + 评审自洽性判据"},
    {"dir": "verify-consistency-20", "genre": "都市悬疑 · 回声档案", "model": "gemini-3.1-pro",
     "version": "自洽机制", "config": "20 章 · 同上(该题材无债务线,事实链为空)"},
]

MAX_CHARS_PER_CHAPTER = 12000


def _chars(text: str) -> int:
    """去空白字符数 —— 中文"字数"的习惯口径(与报告一致)。"""
    return len(re.sub(r"\s", "", text))


def scan(root: Path, spec: dict) -> dict | None:
    directory = root / spec["dir"]
    if not directory.is_dir():
        return None
    titles: dict[str, str] = {}
    title_file = directory / "titles.json"
    if title_file.exists():
        try:
            titles = json.loads(title_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            titles = {}
    chapters = []
    # 只认严格的 chNN.txt:长跑会留下 ch03.incomplete.txt 之类的中间文件,
    # 把它们的数字前缀当章号会把同一章算两遍(并让"中位字数"偏低)。
    files = [p for p in directory.glob("ch*.txt") if re.fullmatch(r"ch\d+\.txt", p.name)]
    for path in sorted(files, key=lambda p: int(re.findall(r"\d+", p.stem)[0])):
        index = int(re.findall(r"\d+", path.stem)[0])
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        chapters.append({
            "idx": index,
            "title": titles.get(path.name, f"第 {index} 章"),
            "chars": _chars(text),
            "text": text[:MAX_CHARS_PER_CHAPTER],
            "truncated": len(text) > MAX_CHARS_PER_CHAPTER,
        })
    if not chapters:
        return None
    chars = sorted(item["chars"] for item in chapters)
    middle = chars[len(chars) // 2] if len(chars) % 2 else (chars[len(chars) // 2 - 1] + chars[len(chars) // 2]) // 2
    statuses: dict[str, int] = {}
    stats_file = directory / "chapters.json"
    if stats_file.exists():
        try:
            rows = json.loads(stats_file.read_text(encoding="utf-8"))
            for row in rows if isinstance(rows, list) else []:
                status = str(row.get("status") or "unknown")
                statuses[status] = statuses.get(status, 0) + 1
        except (OSError, json.JSONDecodeError):
            statuses = {}
    return {
        "key": spec["dir"],
        "dir": spec["dir"],
        "genre": spec["genre"],
        "model": spec["model"],
        "version": spec["version"],
        "config": spec["config"],
        "chapters": chapters,
        "stats": {
            "n": len(chapters),
            "median": middle,
            "mean": round(sum(chars) / len(chars)),
            "min": chars[0],
            "max": chars[-1],
            "below2000": sum(1 for value in chars if value < 2000),
            "statuses": statuses,
        },
    }


def build_html(runs: list[dict]) -> str:
    payload = json.dumps({"runs": runs}, ensure_ascii=False).replace("</", "<\\/")
    return """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ai-write 全部跑批 · 按题材 / 版本 / 模型对比</title>
<style>
  :root{--bg:#0f1115;--card:#171a21;--line:#252a34;--fg:#e6e9ef;--dim:#9aa4b2;--accent:#5b9dff;--good:#4ec98b;--warn:#f0b429;--bad:#e5534b}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);font-family:system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;line-height:1.65}
  .wrap{max-width:1180px;margin:0 auto;padding:32px 18px 80px}
  h1{font-size:22px;margin:0 0 4px}
  .sub{color:var(--dim);font-size:13px;margin-bottom:20px}
  .filters{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:18px;padding:12px 14px;background:var(--card);border:1px solid var(--line);border-radius:10px}
  label.f{font-size:12px;color:var(--dim);display:flex;gap:6px;align-items:center}
  select,button{background:#20242c;color:var(--fg);border:1px solid var(--line);border-radius:7px;padding:5px 8px;font-size:13px}
  button{cursor:pointer}
  button:hover{border-color:var(--accent)}
  .grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fill,minmax(330px,1fr))}
  .card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
  .card.sel{border-color:var(--accent)}
  .t{font-size:15px;font-weight:600;margin-bottom:2px}
  .meta{color:var(--dim);font-size:12px;margin-bottom:8px}
  .pill{display:inline-block;font-size:11px;padding:1px 7px;border-radius:999px;border:1px solid var(--line);color:var(--dim);margin-right:5px}
  .pill.g{color:var(--good);border-color:#245c43}
  .pill.w{color:var(--warn);border-color:#5c4a20}
  .pill.b{color:var(--bad);border-color:#5c2a26}
  .nums{display:grid;grid-template-columns:repeat(4,1fr);gap:6px;margin:10px 0;font-size:12px}
  .nums div{background:#1d2129;border-radius:7px;padding:6px 8px}
  .nums b{display:block;font-size:15px}
  .acts{display:flex;gap:8px;flex-wrap:wrap}
  .overlay{position:fixed;inset:0;background:rgba(6,8,12,.86);display:none;overflow:auto;z-index:50}
  .overlay.on{display:block}
  .panel{max-width:1180px;margin:24px auto;background:var(--bg);border:1px solid var(--line);border-radius:12px;padding:18px 20px}
  .panelhead{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:12px}
  .chap{display:grid;grid-template-columns:1fr 1fr;gap:14px}
  .chap .col{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
  .chap h4{margin:0 0 6px;font-size:13px;color:var(--dim);font-weight:600}
  .chap pre{white-space:pre-wrap;word-break:break-word;font-family:inherit;font-size:14px;margin:0}
  .toc{display:flex;flex-wrap:wrap;gap:6px;margin:10px 0}
  .toc button{font-size:12px;padding:3px 8px}
  table{width:100%;border-collapse:collapse;font-size:12.5px}
  th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line)}
  th{color:var(--dim);font-weight:600}
  footer{margin-top:26px;color:var(--dim);font-size:12px;border-top:1px solid var(--line);padding-top:12px}
  @media(max-width:820px){.chap{grid-template-columns:1fr}}
</style>
</head>
<body>
<div class="wrap">
  <h1>ai-write 全部跑批对照</h1>
  <div class="sub">按 <b>题材 / 版本 / 模型</b> 筛选;每张卡片可逐章读正文,选两个跑批可逐章左右并排。字数口径为去空白字符数,目标 2000 字/章。</div>

  <div class="filters">
    <label class="f">题材 <select id="fGenre"></select></label>
    <label class="f">版本 <select id="fVersion"></select></label>
    <label class="f">模型 <select id="fModel"></select></label>
    <button id="reset">重置</button>
    <span class="meta" id="count"></span>
  </div>

  <div class="grid" id="grid"></div>

  <footer>
    数据来自 <code>docs/evaluation/</code> 下各跑批目录;每章正文原样内联,未做改写。<br>
    所有"更好/更差"的判断来自模型评审与字数统计,<b>不是真人盲评</b>。
  </footer>
</div>

<div class="overlay" id="overlay"><div class="panel" id="panel"></div></div>

<script id="payload" type="application/json">__PAYLOAD__</script>
<script>
const DATA = JSON.parse(document.getElementById('payload').textContent);
const runs = DATA.runs;
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

function uniq(list) { return [...new Set(list)]; }

function fillSelect(el, values, allLabel) {
  el.innerHTML = `<option value="">${allLabel}</option>` +
    values.map((v) => `<option value="${esc(v)}">${esc(v)}</option>`).join('');
}

function initFilters() {
  fillSelect($('fGenre'), uniq(runs.map((r) => r.genre)), '全部题材');
  fillSelect($('fVersion'), uniq(runs.map((r) => r.version)), '全部版本');
  fillSelect($('fModel'), uniq(runs.map((r) => r.model)), '全部模型');
  [$('fGenre'), $('fVersion'), $('fModel')].forEach((el) => el.addEventListener('change', render));
  $('reset').addEventListener('click', () => {
    $('fGenre').value = ''; $('fVersion').value = ''; $('fModel').value = ''; render();
  });
}

function visible() {
  const g = $('fGenre').value, v = $('fVersion').value, m = $('fModel').value;
  return runs.filter((r) => (!g || r.genre === g) && (!v || r.version === v) && (!m || r.model === m));
}

function pill(cls, text) { return `<span class="pill ${cls}">${esc(text)}</span>`; }

function card(r, index) {
  const s = r.stats;
  const below = s.below2000;
  const belowCls = below === 0 ? 'g' : (below / s.n > 0.3 ? 'b' : 'w');
  const statuses = Object.entries(s.statuses).map(([k, n]) => `${k}:${n}`).join(' · ') || '—';
  return `<div class="card">
    <div class="t">${esc(r.dir)}</div>
    <div class="meta">${esc(r.genre)} · ${esc(r.model)} · ${esc(r.version)}</div>
    <div>${pill('', r.config)}</div>
    <div class="nums">
      <div><b>${s.n}</b>章数</div>
      <div><b>${s.median}</b>中位字数</div>
      <div><b>${s.min}</b>最短</div>
      <div><b>${s.max}</b>最长</div>
    </div>
    <div class="meta"><code>&lt;2000</code> ${pill(belowCls, below + '/' + s.n)} · 状态 ${esc(statuses)}</div>
    <div class="acts">
      <button data-read="${index}">逐章阅读</button>
      <button data-compare="${index}">并排对比…</button>
    </div>
  </div>`;
}

function render() {
  const rows = visible();
  $('count').textContent = `${rows.length} 个跑批`;
  $('grid').innerHTML = rows.map((r) => card(r, runs.indexOf(r))).join('');
  $('grid').querySelectorAll('[data-read]').forEach((b) => b.addEventListener('click', () => openRead(+b.dataset.read)));
  $('grid').querySelectorAll('[data-compare]').forEach((b) => b.addEventListener('click', () => openCompare(+b.dataset.compare)));
}

function openOverlay(html) {
  $('panel').innerHTML = html;
  $('overlay').classList.add('on');
}
function closeOverlay() { $('overlay').classList.remove('on'); }
$('overlay').addEventListener('click', (e) => { if (e.target === $('overlay') || e.target.id === 'close') closeOverlay(); });

function openRead(index) {
  const r = runs[index];
  const toc = r.chapters.map((c) => `<button data-ch="${c.idx}">${c.idx} · ${c.chars}字</button>`).join('');
  openOverlay(`<div class="panelhead">
      <div><div class="t">${esc(r.dir)}</div><div class="meta">${esc(r.genre)} · ${esc(r.model)} · ${esc(r.config)}</div></div>
      <button id="close">关闭</button>
    </div>
    <div class="toc" id="toc">${toc}</div>
    <div id="body"><div class="meta">选择上面的章节查看正文。</div></div>`);
  $('toc').querySelectorAll('button').forEach((b) => b.addEventListener('click', () => {
    const c = r.chapters.find((x) => String(x.idx) === b.dataset.ch);
    $('body').innerHTML = `<div class="chap"><div class="col"><h4>${esc(c.title)} · ${c.chars} 字${c.truncated ? '(已截断显示)' : ''}</h4><pre>${esc(c.text)}</pre></div></div>`;
  }));
}

function openCompare(index) {
  const base = runs[index];
  const peers = runs.filter((r) => r.genre === base.genre && r.dir !== base.dir);
  if (!peers.length) { openOverlay(`<div class="panelhead"><div class="t">同一题材下还没有其它跑批可对比</div><button id="close">关闭</button></div>`); return; }
  const options = peers.map((r) => `<option value="${runs.indexOf(r)}">${esc(r.dir)} · ${esc(r.model)} · ${esc(r.config)}</option>`).join('');
  openOverlay(`<div class="panelhead">
      <div><div class="t">并排对比</div><div class="meta">左:${esc(base.dir)}</div></div>
      <button id="close">关闭</button>
    </div>
    <div class="filters"><label class="f">右侧 <select id="peer">${options}</select></label>
      <label class="f">章节 <select id="chap"></select></label></div>
    <div id="body"></div>`);
  const leftLabel = `${base.dir} · ${base.model}`;
  const paint = () => {
    const peer = runs[+$('peer').value];
    const common = base.chapters.map((c) => c.idx).filter((i) => peer.chapters.some((p) => p.idx === i));
    $('chap').innerHTML = common.map((i) => {
      const l = base.chapters.find((c) => c.idx === i);
      const rr = peer.chapters.find((c) => c.idx === i);
      return `<option value="${i}">第 ${i} 章 · ${l.chars} vs ${rr.chars} 字</option>`;
    }).join('');
    $('chap').onchange = () => {
      const i = +$('chap').value;
      const l = base.chapters.find((c) => c.idx === i);
      const rr = peer.chapters.find((c) => c.idx === i);
      $('body').innerHTML = `<div class="chap">
        <div class="col"><h4>${esc(leftLabel)} · ${l.chars} 字${l.truncated ? '(截断)' : ''}</h4><pre>${esc(l.text)}</pre></div>
        <div class="col"><h4>${esc(peer.dir)} · ${esc(peer.model)} · ${rr.chars} 字${rr.truncated ? '(截断)' : ''}</h4><pre>${esc(rr.text)}</pre></div>
      </div>`;
    };
    $('chap').onchange();
  };
  $('peer').onchange = paint;
  paint();
}

initFilters();
render();
</script>
</body>
</html>
""".replace("__PAYLOAD__", payload)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(EVAL_ROOT), help="跑批产物根目录")
    parser.add_argument("--out", default=str(EVAL_ROOT / "_public/gallery.html"))
    args = parser.parse_args(argv)

    root = Path(args.root)
    runs = [item for spec in RUNS if (item := scan(root, spec))]
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_html(runs), encoding="utf-8")
    total = sum(len(r["chapters"]) for r in runs)
    print(json.dumps({"out": str(out), "runs": len(runs), "chapters": total,
                      "bytes": out.stat().st_size}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())