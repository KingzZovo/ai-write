"""已确立事实清单:把"前文已经交代过、后续不得矛盾"的事项喂给写手与评审。

## 为什么需要(根因,2026-11-20)

用户实测到的逻辑冲突:正文先写"母亲住院时弟弟垫了三千元医药费,她答应年底还清",
同一段又写"如今还不到年底,她却只能先拿两千来抵一部分"——**期限没到为什么要还**、
"两千"也不是"抵一部分"(那是三分之二);后文更把这笔钱当租金来源,因果链整个悬空。

根因不在某一句话,而在三层:

1. **写手输入里没有"已确立事实"** —— 大纲给的是故事走向("她要还钱"),没有把
   "欠谁、多少、什么期限、为什么现在要还"当**约束**钉进生成输入,模型只能现编,
   于是编出自相矛盾的经济关系;
2. **评审 prompt 里没有自洽性判据** —— `chapter_evaluator` 的 5 个维度只覆盖情节、
   人物、文风、节奏、伏笔,没有"与已确立事实冲突"这一项,评审也就不会去核对;
3. **没有确定性兜底** —— 事实合同只校验人物称谓(`fact_contract` 自己声明"不做
   语境判断"),金额与期限不进入任何校验。

本模块负责第 1 层(把事实变成可注入的清单)与第 3 层的数据来源;判据在
`chapter_evaluator` 的 system prompt 里,确定性检查见 `check_chapter_against_facts`。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

# 与经济/约定/期限有关的句子:这类句子一旦写进正文,就成了后文的事实约束。
_FACT_KEYWORDS = (
    "欠", "借", "垫", "还清", "还款", "还钱", "抵", "医药费", "房租", "租金",
    "答应", "说好", "承诺", "约定", "期限", "年底", "月底", "到期",
)
_NUMBER_RE = re.compile(r"[一二三四五六七八九十百千万零两0-9]")
_SENTENCE_SPLIT_RE = re.compile(r"[。!?!?…\n]+")


@dataclass
class EstablishedFacts:
    lines: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.lines)

    def block(self, *, max_lines: int = 8) -> str:
        """写手/评审用的一段文本;没有事实时返回空串(调用方可直接拼接)。"""
        lines = self.lines[:max_lines]
        if not lines:
            return ""
        body = "\n".join(f"- {line}" for line in lines)
        return (
            "\n\n【已确立事实(后文不得与之矛盾)】\n"
            f"{body}\n"
            "若本章需要改变其中任何一条(例如提前还款、金额变化、期限调整),"
            "必须在正文里交代触发原因与钱的来源;交代不出来就不要改。"
        )


def extract_established_facts(texts: Iterable[str], *, limit: int = 8) -> list[str]:
    """从正文里抽出"已成事实"的句子(确定性、可复核)。

    只收**同时**满足三点的句子:含经济/约定关键词、含数字、长度适中。这样既能抓住
    "垫了三千元医药费""答应年底还清"这类约束,又不会把普通叙事句收成"事实"。
    """
    out: list[str] = []
    seen: set[str] = set()
    for text in texts:
        if not text:
            continue
        for raw in _SENTENCE_SPLIT_RE.split(text):
            sentence = raw.strip()
            if not (8 <= len(sentence) <= 80):
                continue
            if not _NUMBER_RE.search(sentence):
                continue
            if not any(keyword in sentence for keyword in _FACT_KEYWORDS):
                continue
            key = re.sub(r"\s", "", sentence)
            if key in seen:
                continue
            seen.add(key)
            out.append(sentence)
            if len(out) >= limit:
                return out
    return out


# 金额抽取分两层,因为口语里"钱"经常不带量词:
#   1) 数字紧挨"元/块" —— "垫出了三千元医药费";
#   2) 句子处于**钱语境**(还/欠/抵/租金/医药费…)时,句中的数字也算金额 ——
#      "她只能先拿两千来抵一部分"里的"两千"后面跟着"来",不带"元",但它就是金额。
#      时间量词(年/天/月/次…)后的数字不算钱。
_AMOUNT_RE = re.compile(r"([一二三四五六七八九十百千万零两0-9]{1,8})\s*(?:元|块钱|块)")
_NUMBER_TOKEN_RE = re.compile(r"([一二三四五六七八九十百千万零两0-9]{1,8})")
_MONEY_CONTEXT_RE = re.compile(
    r"(钱|元|块|医药费|房租|租金|还|欠|借|垫|付|交|抵|凑|工资|账)")
_TIME_UNIT_RE = re.compile(r"^(?:年|天|月|日|个|次|人|岁|条|只|件|周|分钟|小时|辈子)")


def _amounts_in(sentence: str) -> list[int]:
    """句子里的金额(元)。带时间量词的数字不计入。"""
    amounts = [_to_int(m.group(1)) for m in _AMOUNT_RE.finditer(sentence)]
    if _MONEY_CONTEXT_RE.search(sentence):
        for match in _NUMBER_TOKEN_RE.finditer(sentence):
            tail = sentence[match.end(): match.end() + 3]
            if _TIME_UNIT_RE.match(tail):
                continue
            value = _to_int(match.group(1))
            # 个位数大多是"一定/一阵/一起/一定还清"这类词的一部分,不是金额;
            # 只有紧跟元/块才当钱(那种情况已被 _AMOUNT_RE 收过)。
            if value >= 10:
                amounts.append(value)
    return [a for a in dict.fromkeys(amounts) if a > 0]
_DEADLINE_RE = re.compile(r"(年底|春节|年前|月底|下个?月|开春|秋后|年前|明年|来年)")
_ALTERNATIVE_RE = re.compile(r"(一半|一部分|部分|先|暂时|先拿|凑了|剩下|还差|不够)")
# 金额比较必须**指向同一笔钱**:真实跑批里 `verify-consistency-3` 第 2 章出现
# "初五交八百块铺租""月末补齐周航那一千块的账" —— 八百是铺租、一千是那笔欠款,
# 用途不同,把它们和已确立的 2000/3000 比就是纯噪音。只有出现指代词/专指名词
# (那笔/这笔/垫款/欠款/医药费/借的)时才认为在谈同一笔账。
_ANAPHORA_RE = re.compile(
    r"(那笔|这笔|垫款|欠款|欠的|借的|借来|医药费|垫的|那三千|这两千|那一千|账上)")
# 时间口径:只有"明确还没到"才可能构成"未到期处置";"到了/如约"是正常履约。
_NOT_YET_RE = re.compile(r"(还没|没到|没有到|未到|不到|未及|离[^。]{0,6}还)")
_PAST_DUE_RE = re.compile(r"(到了|已到|如约|按期|年前已|期限已)")


def check_chapter_against_facts(
    text: str,
    facts: Iterable[str],
    *,
    chapter_time_hint: str = "",
    check_amounts: bool = False,
) -> list[dict[str, Any]]:
    """确定性兜底:标出"期限未到就处置该笔钱"这类可机械判定的冲突(只告警不阻断)。

    **金额比较默认关闭**(``check_amounts=False``)。理由是一次真实实测:用 20 章
    现实生活题材(``docs/evaluation/verify-facts-20``)跑,启用金额比较后 20 章报出
    18 条,逐条核对几乎**全是误报** —— "上个月那笔七百块钱的货款""旧布包里那一千块
    周转钱""九百块的房租"都是**新出现**的金额,它们与已确立事实并不矛盾。
    要判"同一笔账的金额不符"必须先把金额绑定到具体对象(哪笔钱),那是金额台账
    ( ``money_plan`` )的职责,不是正则启发式能承担的。宁可不开,也不要制造噪音把
    作者淹掉;金额是否自洽交给评审的自洽性判据。

    期限类检查保留并默认开启:同一批实测 0 误报,且正好命中用户报告的核心问题
    ("还没到年底,却已经在处置这笔钱")。
    """
    findings: list[dict[str, Any]] = []
    if not text:
        return findings

    fact_amounts: list[int] = []
    fact_deadlines: list[str] = []
    for fact in facts:
        fact_amounts.extend(_amounts_in(fact))
        fact_deadlines.extend(_DEADLINE_RE.findall(fact))
    fact_amounts = [a for a in fact_amounts if a > 0]

    sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]
    for sentence in sentences:
        if not any(keyword in sentence for keyword in ("还", "还款", "抵", "租金", "房租")):
            continue
        amounts = _amounts_in(sentence)
        if not amounts:
            continue
        # 冲突 1:本章出现的金额与已确立数额不同。
        # 不再用"部分/一半/先"直接豁免(实测:"她只能先拿两千来抵一部分"正是被它
        # 豁免掉的,而这句本身逻辑就是错的 —— 两千不是三千的"一部分"那种量级表述);
        # 有部分说明时降为 info,仍然报出来让评审与作者看到。
        if check_amounts and fact_amounts and _ANAPHORA_RE.search(sentence) \
                and any(a not in fact_amounts for a in amounts):
            has_partial = bool(_ALTERNATIVE_RE.search(sentence))
            findings.append({
                "kind": "fact_amount_mismatch",
                "severity": "info" if has_partial else "warning",
                "sentence": sentence[:60],
                "established_amounts": sorted(set(fact_amounts)),
                "chapter_amounts": amounts,
                "partial_phrasing": has_partial,
            })
        # 冲突 2:已确立期限,而本章时间**明确还没到**该期限,却已经在处置这笔钱。
        # 注意:不能用"先/一部分"豁免 —— 那解释的是金额,解释不了"为什么期限没到就还";
        # 反过来"到了/如约"这类已到期表述必须豁免(按时履约不是矛盾)。
        if fact_deadlines and _NOT_YET_RE.search(chapter_time_hint) \
                and not _PAST_DUE_RE.search(chapter_time_hint):
            if any(marker in sentence for marker in ("还", "抵", "交租")):
                findings.append({
                    "kind": "obligation_before_deadline",
                    "severity": "warning",
                    "sentence": sentence[:60],
                    "established_deadline": sorted(set(fact_deadlines))[:3],
                    "chapter_time_hint": chapter_time_hint[:30],
                })
    return findings


_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9}


def _to_int(token: str) -> int:
    """中文数字 → 整数(够用即可:金额在 1~99999999 之间)。无法解析返回 -1。

    "万"是节单位:一万 = 10000。此前把它当普通单位再整体乘一次,得到 1e8。
    """
    if not token:
        return -1
    if token.isdigit():
        return int(token)
    total, section, number = 0, 0, 0
    units = {"十": 10, "百": 100, "千": 1000}
    for ch in token:
        if ch in _CN_DIGITS:
            number = _CN_DIGITS[ch]
        elif ch == "万":
            total = (total + section + number) * 10000
            section = 0
            number = 0
        elif ch in units:
            section += (number or 1) * units[ch]
            number = 0
    result = total + section + number
    return result if result else -1


async def build_established_facts(
    db: Any, project_id: Any, chapter_id: Any = None, *, lookback: int = 3
) -> EstablishedFacts:
    """取本章**之前**最近 ``lookback`` 章的正文,抽出已确立事实。

    只往前看:后文的计划不算"已确立",不能拿来判本章矛盾。
    """
    from sqlalchemy import select

    from app.models.project import Chapter, Volume

    if db is None or not project_id:
        return EstablishedFacts()

    target_idx = None
    if chapter_id:
        try:
            target = await db.get(Chapter, chapter_id)
            target_idx = getattr(target, "global_idx", None) if target else None
        except Exception:  # noqa: BLE001 - 取不到目标章就退化成"最近几章"
            target_idx = None

    stmt = (
        select(Chapter.content_text)
        .join(Volume, Chapter.volume_id == Volume.id)
        .where(Volume.project_id == str(project_id))
        .where(Chapter.content_text.isnot(None))
        .order_by(Chapter.global_idx.desc().nullslast(), Chapter.chapter_idx.desc())
        .limit(max(1, int(lookback)))
    )
    if target_idx is not None:
        stmt = stmt.where(Chapter.global_idx < target_idx)
    try:
        rows = (await db.execute(stmt)).scalars().all()
    except Exception:  # noqa: BLE001 - 事实清单取不到时绝不影响生成
        return EstablishedFacts()

    return EstablishedFacts(extract_established_facts(rows))


async def load_established_facts_block(
    db: Any, project_id: Any, chapter_id: Any = None
) -> str:
    """给调用方用的一站式取用:构建事实清单并渲染成可注入文本。

    取不到(没有更早章节、库异常)就返回空串 —— 事实清单缺失只是少一层保护,
    绝不能让生成或评审失败。调用方显式持有 ``db``,评审接口本身不碰数据库。
    """
    try:
        facts = await build_established_facts(db, project_id, chapter_id)
        return facts.block()
    except Exception:  # noqa: BLE001
        return ""


def established_facts_block_from_text(text: str) -> str:
    """SSE 路径用:没有 db 会话,但手上已有前文正文。"""
    return EstablishedFacts(extract_established_facts([text or ""])).block()
