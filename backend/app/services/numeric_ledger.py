"""跨章账目核对:解析金额、区分单价与总额、比对"计划已定的数"。

**只做判定,不做改写。** 判定错就自动改稿,风险大于收益(实测已见:同一名目下的
单价与总额常在同一段里并存,机器分不干净)。

为什么不用正则直接在正文里找"某名目附近的数字"(实测 2026-11-16):
- 单价与总额同属一个名目,正则分不开。「每套校服二十八块」的"二十八"会被当成工钱总额,
  而真正的总额在另一句(「一共是八百四十块整」)。
- 名目会被抽成碎片:同一段文本抽出来的"名目"是 `呢`、`的铺租`、`周芸`。
所以改由模型抽 `{名目, 数额, 原文}`(见 ``scripts/quality_eval/amounts.py`` 的 ``SYSTEM``),
本模块负责把模型给出的"数额"解析成可比对的数,并按名目区分单价与总额。

实测限制(`deepseek-chat`,`max_tokens=1500`):11 次调用里 4 次整段返回空正文
(推理吃满预算)。因此**空正文必须当成失败**,不能当成"本章没有金额"。
"""
from __future__ import annotations

import json
import re

_ITEM_KEYS = ("item", "名称", "名目", "用途", "项目")
_VALUE_KEYS = ("value", "金额", "数额", "amount")
_QUOTE_KEYS = ("quote", "原文", "context", "原文片段", "片段")

_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000}

# 名目里出现这些词说明它是单价,不是总额。
_UNIT_PRICE_MARKERS = ("每套", "单价", "一套", "每斤", "每件", "每月", "一天", "一次")
_TOTAL_MARKERS = ("总", "共", "合计", "一共", "整")

# 冲突清单的上限:这是给作者看的一条提示,不是把全章账目抄一遍。
MAX_CONFLICTS = 5


def _parse_plain(s: str) -> int | None:
    """把纯数字/纯汉字数词解成整数;解不出返回 None。"""
    if not s:
        return None
    digits = re.search(r"\d+", s)
    if digits:
        return int(digits.group(0))
    numeral = "".join(ch for ch in s if ch in _CN_DIGITS or ch in _CN_UNITS or ch == "万")
    if not numeral:
        return None
    total = 0
    section = 0
    number = 0
    last_unit: int | None = None
    for ch in numeral:
        if ch in _CN_DIGITS:
            number = _CN_DIGITS[ch]
        elif ch in _CN_UNITS:
            unit = _CN_UNITS[ch]
            section += (number or 1) * unit
            number = 0
            last_unit = unit
        elif ch == "万":
            section = (section + number) * 10000
            total += section
            section = 0
            number = 0
            last_unit = 10000
        else:
            return None
    if number and last_unit and last_unit >= 100:
        # 口语简写:"一千二"=1200、"一万二"=12000。
        section += number * (last_unit // 10)
        number = 0
    elif number and last_unit == 10:
        section += number
        number = 0
    return total + section + number


def parse_amount(text: object) -> int | float | None:
    """把"八百二十"/"820 元"/"二十八块"/"四块五"/"二十块零五毛"解成数额。

    解不出返回 None。小数(角)返回 float —— 口语金额里"块/角"必须按位处理,
    否则"四块五"会被读成 5、"二十块零五毛"会被读成 25(实测踩过)。
    """
    if text is None:
        return None
    s = str(text).strip()
    if not s:
        return None
    s = s.replace("毛", "角")
    for sep in ("块", "元"):
        if sep in s:
            yuan_s, _, rest = s.partition(sep)
            yuan = _parse_plain(yuan_s)
            if yuan is None:
                return None
            rest = rest.lstrip("零和 ").strip()
            if not rest:
                return yuan
            if "角" in rest:
                jiao = _parse_plain(rest.split("角")[0]) or 0
            elif any(ch in _CN_DIGITS or ch.isdigit() for ch in rest):
                # "四块五"这类省略单位的写法:尾数按角算。
                jiao = _parse_plain(rest) or 0
            else:
                return yuan
            value = yuan + jiao / 10
            return int(value) if value == int(value) else value
    return _parse_plain(s)


def parse_amounts(raw: str) -> list[dict] | None:
    """解析模型的金额输出;兼容代码围栏与四种实测字段名。解不出返回 None。

    字段名在不同调用间真的会变,实测见过 ``{金额,单位,原文}``、
    ``{金额,原文,用途}``、``{amount,context}``、``{item,value,quote}``。
    """
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        parts = text.split("```")
        text = parts[1] if len(parts) > 1 else text
        text = text[4:] if text.startswith("json") else text
    text = text.strip()
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end < start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, list):
        return None
    rows = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        item = next((entry[k] for k in _ITEM_KEYS if entry.get(k)), "")
        value = next((entry[k] for k in _VALUE_KEYS if entry.get(k) is not None), "")
        quote = next((entry[k] for k in _QUOTE_KEYS if entry.get(k)), "")
        rows.append({"item": str(item), "value": str(value), "quote": str(quote),
                     "amount": parse_amount(value)})
    return rows


def looks_like_total(item: str) -> bool:
    """总额 vs 单价:名目带"总/共/合计/一共/整"算总额,带"每/单价"算单价。"""
    if any(marker in item for marker in _UNIT_PRICE_MARKERS):
        return False
    return any(marker in item for marker in _TOTAL_MARKERS)


def totals_for(rows: list[dict], keyword: str) -> list[dict]:
    """取某名目下的总额;没有明确的总额行时退回所有命中行。"""
    hits = [r for r in rows if keyword and keyword in str(r.get("item") or "")]
    return [r for r in hits if looks_like_total(str(r.get("item") or ""))] or hits


def planned_amounts_from_text(text: str) -> set[float]:
    """从大纲/金额钉子文本里取出所有已写明的数额,供比对。"""
    try:
        from app.services.numeric_pins import amount_spans
    except Exception:  # noqa: BLE001
        return set()
    values: set[float] = set()
    for match in amount_spans(text or ""):
        value = parse_amount(match.group(0))
        if value is not None:
            values.add(float(value))
    return values


def conflicts(rows: list[dict], planned: set[float], *, limit: int = MAX_CONFLICTS) -> list[dict]:
    """写出的"总额"里,计划没写过的那些。

    这是**启发式提示**,不是判错:计划本身可能不全,所以"计划里没有这个数"只说明
    作者需要自己看一眼。计划为空时一律不判(返回 ``[]``),避免凭空报警。
    只比总额:单价千变万化,计划不会逐个钉。
    """
    if not planned:
        return []
    out: list[dict] = []
    for row in rows or []:
        amount = row.get("amount")
        if amount is None or not looks_like_total(str(row.get("item") or "")):
            continue
        if float(amount) in planned:
            continue
        out.append({"item": str(row.get("item") or ""), "written": amount,
                    "quote": str(row.get("quote") or "")})
        if len(out) >= limit:
            break
    return out


AMOUNT_EXTRACTION_SYSTEM = (
    "你是中文小说的账目核对员。只输出 JSON 数组,不要解释,不要 markdown 代码围栏。\n"
    "从正文里找出**每一处明确给出的钱的数额**,每项一个对象:\n"
    '{"item": "这笔钱的名目(如 铺租 / 货款 / 校服工钱)",'
    ' "value": "数额,按原文写法",'
    ' "quote": "包含该数额的连续原文片段,不超过 40 字"}\n'
    "规则:\n"
    "- 只抽正文真的写出来的数额,不要推算、不要补全。\n"
    "- 单价与总额都要抽,并在 item 里写清是单价还是总额(如 校服工钱总额 / 每套单价)。\n"
    "- 同一个名目出现多次就分别列出。\n"
    "- 没有金额就输出 []。\n"
)


def drift_against(rows: list[dict], later_rows: list[dict]) -> list[dict]:
    """跨章漂移指标:本章写出的总额里,后一章没写过的那些。

    用来量"同一笔钱在相邻两章变来变去":金额常常是**后一章的大纲**才第一次写明,
    所以拿后一章的实际数额当参照,比拿本章大纲更接近真实漂移
    (实测 ch07 自造 880 / 840,而 ch08 是 820)。
    """
    planned = {float(row["amount"]) for row in (later_rows or [])
               if row.get("amount") is not None}
    return conflicts(rows, planned)


# ---------------------------------------------------------------------------
# 按名目的金额台账:把"实际写出过的金额"记下来,写后核对才有可比对象
# ---------------------------------------------------------------------------
# 实测(2026-11-17,真实章节链):旧口径("这个总额不在计划里")3 条命中里只有 1 条是真的,
# 两条误报都是"这个数正文里早就写过,只是大纲/摘要没记"(ch08 的 820、ch18 的 3000)。
# 根因是没有地方记录"实际写出过的金额",于是"新引入的金额"和"与前文冲突的金额"
# 分不开。台账就是这份记录:第一次写出即登记,之后只按**同名目**比对。
#
# 登记口径(每章一次,**只登记总额**):
#     {"item": 名目标签, "amount": 数额}   —— 读回时附上 chapter_idx
# 判定口径(见 ``ledger_conflicts``):
#     - 同名目已登记过**同一个数** → 不报(与前文一致,哪怕计划里没有这个数);
#     - 同名目登记过**别的数** → 报;
#     - 台账里没有这个名目 → 退回旧的计划口径(数额不在计划里才报)。
#
# 名目匹配必须保守:模型给的名目是自由文本(实测抽出来过"呢""的铺租""周芸"),
# 归一后要求共享 ≥2 个**连续汉字**才算同名目;纯拉丁名目只有完全一致才算同名。

# 同名目要求的最短公共连续汉字数。1 个字会把"工钱/饭钱"这类不同名目并到一起。
MIN_ITEM_RUN = 2
# 归一时名目的最大长度(防止超长文本把匹配拖慢);超出部分截断。
MAX_ITEM_LEN = 80
# 单章登记的台账条目上限(只给比对用,不必把全章账目抄一遍)。
MAX_LEDGER_ITEMS = 20
# 一次比对最多接受多少条台账条目(读回上限,避免历史越积越多、越算越慢)。
MAX_LEDGER_ENTRIES = 200

# 归一:去掉分类词、数字、标点、空白,只留字(汉字/字母)。
# - 分类词("总额/单价"这类)在名目里是模板词,`looks_like_total` 已经判过;留着会让
#   "铺租总额"和"工钱总额"只因共享"总额"就被当成同名目(去词前会这样误配);
# - 数字在名目里通常是数量或编号("三十套校服总工钱" vs "四十套校服总工钱"),
#   不该影响"是不是同一个名目"。
_ITEM_NOISE = ("总额", "总数", "合计", "一共", "总共", "整", "每套", "单价", "一套",
               "每斤", "每件", "每月", "每次", "一天", "一次")
_ITEM_DROP_RE = re.compile(r"[\d\s\W_]+")
# 只把汉字当作"连续汉字"证据;扩展 A 与兼容区一并算上(异体字人名常见)。
_CJK_CHAR_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")


def normalize_item(item: object) -> str:
    """名目归一:去分类词、数字、标点、空白,只留字。空名目返回空串。"""
    text = str(item or "").strip()
    if not text:
        return ""
    for phrase in _ITEM_NOISE:
        text = text.replace(phrase, "")
    return _ITEM_DROP_RE.sub("", text)[:MAX_ITEM_LEN]


def shared_item_run_len(a: str, b: str, *, limit: int = 12) -> int:
    """两个**已归一**名目的最长公共连续汉字串长度(无汉字公共串时为 0)。"""
    if not a or not b:
        return 0
    best = 0
    for i, ch in enumerate(a):
        if not _CJK_CHAR_RE.match(ch):
            continue
        for j, other in enumerate(b):
            if ch != other:
                continue
            run = 0
            while (run < limit and i + run < len(a) and j + run < len(b)
                   and a[i + run] == b[j + run] and _CJK_CHAR_RE.match(a[i + run])):
                run += 1
            if run > best:
                best = run
    return best


def same_item(a: object, b: object, *, min_run: int = MIN_ITEM_RUN) -> bool:
    """两个名目是不是同名目:归一后完全一致,或共享 ≥ ``min_run`` 个连续汉字。"""
    left, right = normalize_item(a), normalize_item(b)
    if not left or not right:
        return False
    if left == right:
        return True
    return shared_item_run_len(left, right) >= min_run


def parse_ledger_entries(payload: object) -> list[dict]:
    """解析持久化的台账 JSON;坏结构、坏数额一律跳过,绝不抛。

    台账是提示用的旁路:读不出来只说明"没有可比对象",不能让核对因此报错或报警。
    字段名固定为 ``item`` / ``amount``(可选 ``chapter_idx``),不做别名猜测 ——
    写进去的和读出来的都是本模块自己的格式。
    """
    if not isinstance(payload, list):
        return []
    out: list[dict] = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        item = entry.get("item")
        if not isinstance(item, str) or not item.strip():
            continue
        amount = entry.get("amount")
        if isinstance(amount, bool) or not isinstance(amount, (int, float, str)):
            continue
        try:
            value = float(amount)
        except (TypeError, ValueError):
            continue
        record = {"item": item, "amount": value}
        chapter_idx = entry.get("chapter_idx")
        if isinstance(chapter_idx, int) and not isinstance(chapter_idx, bool):
            record["chapter_idx"] = chapter_idx
        out.append(record)
        if len(out) >= MAX_LEDGER_ENTRIES:
            break
    return out


def ledger_entries_from_rows(rows: list[dict], *,
                             limit: int = MAX_LEDGER_ITEMS) -> list[dict]:
    """从抽取结果里取"本章要登记进台账"的条目:只登记总额,解析不出数额的跳过。"""
    out: list[dict] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        item = str(row.get("item") or "")
        amount = row.get("amount")
        if amount is None or not item.strip() or not looks_like_total(item):
            continue
        out.append({"item": item, "amount": amount})
        if len(out) >= limit:
            break
    return out


def _ledger_evidence(registered: list[dict]) -> dict:
    return {
        "ledger_items": sorted({str(entry.get("item")) for entry in registered}),
        "ledger_amounts": sorted({entry["amount"] for entry in registered}),
        "ledger_chapters": sorted({entry["chapter_idx"] for entry in registered
                                   if isinstance(entry.get("chapter_idx"), int)}),
    }


def ledger_conflicts(rows: list[dict], ledger: object,
                     planned: object = None,
                     *, limit: int = MAX_CONFLICTS) -> list[dict]:
    """写后核对的判定:只有"同名目此前已登记过、且本次数额不同"才算冲突。

    逐条规则(纯判断,任何输入都不抛):
    - 台账里同名目已登记**同一个数** → 不报。这正是两类误报的来源:ch08 的 820 写在
      本章大纲里、ch18 的 3000 早在 ch10 正文里,两者此前都写出过;
    - 台账里同名目登记过**别的数** → 报(附台账证据);
    - 台账里没有这个名目 → 退回旧的计划口径:``planned`` 给了(哪怕空集合)就比数额,
      数额不在计划里才报;``planned=None`` 表示不做计划口径(只按台账判)。
      首次出现的名目若与计划一致仍不报,与计划不符则报 —— 保持既有的"钉子对不上"能力。
    只比总额:单价不在台账口径里(计划不会逐个钉单价,`looks_like_total` 先过滤)。
    """
    if not rows:
        return []
    entries = parse_ledger_entries(ledger)
    planned_values = None
    if planned is not None:
        try:
            planned_values = {float(v) for v in planned}
        except (TypeError, ValueError):
            planned_values = None
    out: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        item = str(row.get("item") or "")
        amount = row.get("amount")
        if amount is None or not looks_like_total(item):
            continue
        try:
            written = float(amount)
        except (TypeError, ValueError):
            continue
        registered = [entry for entry in entries if same_item(item, entry.get("item"))]
        if registered:
            if any(entry["amount"] == written for entry in registered):
                continue
            out.append({"item": item, "written": amount,
                        "quote": str(row.get("quote") or ""),
                        **_ledger_evidence(registered)})
        else:
            if planned_values is None or written in planned_values:
                continue
            out.append({"item": item, "written": amount,
                        "quote": str(row.get("quote") or ""),
                        "ledger_items": [], "ledger_amounts": [], "ledger_chapters": []})
        if len(out) >= limit:
            break
    return out
