"""Deterministic consistency checks for generated outline payloads.

This gate is intentionally model-free. It catches structural drift before an
outline is persisted: multiple names for the same role, leftover alternative
plans, malformed volume plans, and known invalid/degraded payload markers.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
from typing import Any


# Unknown Chinese name boundaries cannot be inferred from an action verb:
# “导师陈川留在门外” does not establish a person named “陈川留”.
# Hard identity checks consume explicit declarations, not arbitrary prose.
ROLE_PATTERNS: dict[str, tuple[str, ...]] = {
    "protagonist": (
        r"(?:主角|主人公|男主|女主)(?:\s*[:：]\s*|\s*(?:名叫|名为|叫)\s*)([\u4e00-\u9fff]{2,4})(?=[，,。；;：:\s]|$)",
        r"(?:主角|主人公|男主|女主)([\u4e00-\u9fff]{2,4})(?=[，,]\s*[男女](?:[，,。；;\s]|$))",
    ),
    "mentor": (
        r"(?:^|[\n。；;，,])\s*([\u4e00-\u9fff]{2,4})(?=是[^\n。；;]{0,24}(?:外勤)?导师(?:级别)?成员)",
        r"(?:导师|救援者)(?:\s*[:：]\s*|\s*(?:名叫|名为|叫)\s*)([\u4e00-\u9fff]{2,4})(?=[，,。；;：:\s]|$)",
    ),
    # Free prose after kinship words is not a name declaration. Require
    # an explicit naming marker and a complete token; never consume prefixes
    # such as 母亲有亲近却 / 父亲常年在外 as canonical identity.
    "father": (
        r"(?:父亲|其父)(?:\s*[:：]\s*|\s*(?:名叫|名为|叫)\s*)([\u4e00-\u9fff]{2,4})(?=[，,。；;：:\s]|$)",
    ),
    "mother": (
        r"(?:母亲|其母)(?:\s*[:：]\s*|\s*(?:名叫|名为|叫)\s*)([\u4e00-\u9fff]{2,4})(?=[，,。；;：:\s]|$)",
    ),
}

GENERIC_PLACEHOLDER_NAMES = {"主角", "男主", "女主", "导师", "父亲", "母亲", "人物"}
COMMON_CHINESE_SURNAMES = set("赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜戚谢邹喻柏水窦章云苏潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳鲍史唐费廉岑薛雷贺倪汤滕殷罗毕郝邬安常乐于时傅皮卞齐康伍余元卜顾孟平黄和穆萧尹姚邵汪祁毛禹狄米贝明臧计伏成戴谈宋庞熊纪舒屈项祝董梁杜阮蓝闵席季麻强贾路娄危江童颜郭梅盛林刁钟徐邱骆高夏蔡田胡凌霍虞万支柯昝管卢莫经房裘缪干解应宗丁宣邓郁单杭洪包诸左石崔吉龚程邢滑裴陆荣翁荀羊於惠甄曲家封芮羿储靳汲邴糜松井段富巫乌焦巴弓牧隗山谷车侯宓蓬全郗班仰秋仲伊宫宁仇栾暴甘斜厉戎祖武符刘景詹束龙叶幸司韶郜黎蓟薄印宿白怀蒲邰从鄂索咸籍赖卓蔺屠蒙池乔阴胥能苍双闻莘党翟谭贡劳逄姬申扶堵冉宰郦雍桑桂濮牛寿通边扈燕冀郏浦尚农温别庄晏柴瞿阎充慕连茹习宦艾鱼容向古易慎戈廖庾终暨居衡步都耿满弘匡国文寇广禄阙东欧殳沃利蔚越夔隆师巩厍聂晁勾敖融冷訾辛阚那简饶空曾毋沙乜养鞠须丰巢关蒯相查后荆红游竺权逯盖益桓公万俟司马上官欧阳夏侯诸葛闻人东方赫连皇甫尉迟公羊澹台公冶宗政濮阳淳于单于太叔申屠公孙仲孙轩辕令狐钟离宇文长孙慕容鲜于闾丘司徒司空亓官司寇仉督子车颛孙端木巫马公西漆雕乐正壤驷公良拓跋夹谷宰父谷梁段干百里东郭南门呼延归海羊舌微生岳帅缑亢况后有琴梁丘左丘东门西门商牟佘佴伯赏南宫墨哈谯笪年爱阳佟")
NON_NAME_SUBSTRINGS = ("动机", "担保", "只使用", "失踪前", "留下", "工牌", "录音", "面容", "遗物", "证据", "被追", "自己")
ROLE_ENTITY_REGISTRY_KEYS = {
    "protagonist": ("protagonist", "main_character", "主角"),
    "mentor": ("mentor", "rescuer", "导师", "救援者"),
    "father": ("father", "父亲"),
    "mother": ("mother", "母亲"),
}

ALTERNATIVE_PLAN_PATTERNS = (
    r"(?:候选|备选|另[一二三四五六七八九]套|第二套|第三套).{0,12}(?:方案|分卷|大纲|版本)",
    r"方案[一二三四五六七八九A-Z]",
    r"版本[一二三四五六七八九A-Z]",
)

SHENYI_REQUIRED_LOGIC_ANCHORS: dict[str, tuple[str, tuple[str, ...]]] = {
    "semantic_anchor_encoding": (
        "纸质追查账必须被转译为低语义维修记录，不能保留直白文字推演",
        ("电路布线图", "管网维修单", "阻值", "节点图"),
    ),
    "academy_bargaining_motive": (
        "暗面学院保林照必须有足够锋利的政治/技术利益支点",
        ("异常滤波接口", "活体节点", "反向解析", "自治权"),
    ),
    "old_district_physical_basis": (
        "旧环十二区必须有强拆不可行的物理空间逻辑",
        ("旧式城市管网", "重型机械", "地基", "链式空间折叠"),
    ),
    "evidence_decay_catalyst": (
        "第二卷到第三卷必须有证据衰变与反向追踪压力作为行动催化剂",
        ("证据衰变", "反向追踪", "底层自检", "物理抹除"),
    ),
    "interface_tactical_actions": (
        "林照的异常滤波接口必须转化为可执行战术动作，而不是抽象开挂能力",
        ("频率滤波", "协议空拍", "权限错位", "三秒延迟"),
    ),
    "birth_record_fatality": (
        "出生记录必须有绝对致命后果，支撑叶清牺牲重量",
        ("出生记录", "身份索引归零", "无主接口", "强行接管"),
    ),
    "daily_reality_routes": (
        "第二、三卷必须保留快递与维修日常轨迹来锚定东港市现实质感",
        ("快递路线", "维修工单", "地下管网巡检", "物理泄漏点"),
    ),
}


@dataclass(slots=True)
class OutlineConsistencyIssue:
    code: str
    message: str
    severity: str = "error"
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "severity": self.severity,
            "details": self.details,
        }


@dataclass(slots=True)
class OutlineConsistencyReport:
    ok: bool
    issues: list[OutlineConsistencyIssue] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "issues": [issue.to_dict() for issue in self.issues],
            "facts": self.facts,
        }


class OutlineConsistencyError(RuntimeError):
    def __init__(self, report: OutlineConsistencyReport):
        self.report = report
        messages = "; ".join(issue.message for issue in report.issues) or "outline consistency failed"
        super().__init__(messages)


def _payload_text(payload: Any) -> str:
    if isinstance(payload, str):
        return payload
    if not isinstance(payload, dict):
        return ""
    parts: list[str] = []
    for key in ("raw_text", "full_outline", "summary", "title", "core_conflict"):
        value = payload.get(key)
        if isinstance(value, str):
            parts.append(value)
    for key in ("structured", "sections", "volume_plan", "chapter_summaries", "entity_registry"):
        value = payload.get(key)
        if value:
            parts.append(json.dumps(value, ensure_ascii=False, sort_keys=True))
    return "\n".join(parts)


def _clean_name(name: str) -> str:
    return re.sub(r"[，,。；;：:\s].*$", "", name or "").strip()


def _looks_like_person_name(name: str) -> bool:
    if not (2 <= len(name) <= 4):
        return False
    if name in GENERIC_PLACEHOLDER_NAMES:
        return False
    if any(fragment in name for fragment in NON_NAME_SUBSTRINGS):
        return False
    return name[0] in COMMON_CHINESE_SURNAMES


def _registry_role_names(payload: Any) -> dict[str, set[str]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("entity_registry"), dict):
        return {}
    registry = payload["entity_registry"]
    names: dict[str, set[str]] = {}
    for role, keys in ROLE_ENTITY_REGISTRY_KEYS.items():
        for key in keys:
            value = registry.get(key)
            if isinstance(value, str) and value.strip():
                names.setdefault(role, set()).add(value.strip())
    return names


def extract_role_names(text: str, payload: Any | None = None) -> dict[str, list[str]]:
    """Extract explicit identity declarations/registry entries, not all mentions."""
    names: dict[str, set[str]] = {}
    for role, patterns in ROLE_PATTERNS.items():
        for pattern in patterns:
            for match in re.finditer(pattern, text):
                name = _clean_name(match.group(1))
                if _looks_like_person_name(name):
                    names.setdefault(role, set()).add(name)
    for role, registry_names in _registry_role_names(payload).items():
        names.setdefault(role, set()).update(registry_names)
    return {role: sorted(values) for role, values in names.items()}


def _volume_plan_from_payload(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    raw = payload.get("volume_plan")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]

_CN_VOLUME_DIGITS: dict[str, int] = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}


def _volume_number(token: str) -> int | None:
    """把「第X卷」中的 X 解析为卷号;解析不出来就返回 None(不猜)。"""
    raw = (token or "").strip()
    if not raw:
        return None
    if raw.isdigit():
        return int(raw)
    if raw in _CN_VOLUME_DIGITS:
        return _CN_VOLUME_DIGITS[raw]
    if raw.startswith("十"):
        tail = raw[1:]
        if not tail:
            return 10
        if tail in _CN_VOLUME_DIGITS and _CN_VOLUME_DIGITS[tail] < 10:
            return 10 + _CN_VOLUME_DIGITS[tail]
        return None
    if "十" in raw:
        head, _, tail = raw.partition("十")
        if head in _CN_VOLUME_DIGITS and _CN_VOLUME_DIGITS[head] < 10:
            base = _CN_VOLUME_DIGITS[head] * 10
            if not tail:
                return base
            if tail in _CN_VOLUME_DIGITS and _CN_VOLUME_DIGITS[tail] < 10:
                return base + _CN_VOLUME_DIGITS[tail]
    return None


def _is_shenyi_payload(payload: Any, text: str) -> bool:
    if isinstance(payload, dict):
        title = str(payload.get("title") or "")
        if "神裔" in title:
            return True
        registry = payload.get("entity_registry")
        if isinstance(registry, dict) and {"林照", "沈听澜", "旧环十二区"} <= {
            str(value).strip() for value in registry.values()
        }:
            return True
    return "《神裔》" in text


def _check_shenyi_logic_anchors(text: str, issues: list[OutlineConsistencyIssue]) -> None:
    for code, (message, required_terms) in SHENYI_REQUIRED_LOGIC_ANCHORS.items():
        missing = [term for term in required_terms if term not in text]
        if missing:
            issues.append(
                OutlineConsistencyIssue(
                    code,
                    message,
                    details={"missing_terms": missing, "required_terms": list(required_terms)},
                )
            )

def _check_volume_plan(plan: list[dict[str, Any]], issues: list[OutlineConsistencyIssue]) -> None:
    if not plan:
        return
    idx_values: list[int] = []
    titles: list[str] = []
    for item in plan:
        try:
            idx = int(item.get("idx") or 0)
        except (TypeError, ValueError):
            idx = 0
        idx_values.append(idx)
        title = str(item.get("title") or "").strip()
        if title:
            titles.append(title)
        try:
            chapters = int(item.get("est_chapters") or 0)
        except (TypeError, ValueError):
            chapters = 0
        if idx <= 0:
            issues.append(OutlineConsistencyIssue("volume_idx_invalid", "分卷规划存在无效 idx", details={"item": item}))
        if chapters <= 0:
            issues.append(OutlineConsistencyIssue("volume_chapters_invalid", "分卷规划存在无效章节数", details={"item": item}))
    if len(idx_values) != len(set(idx_values)):
        issues.append(OutlineConsistencyIssue("volume_idx_duplicate", "分卷规划 idx 重复", details={"idx_values": idx_values}))
    if titles and len(titles) != len(set(titles)):
        issues.append(OutlineConsistencyIssue("volume_title_duplicate", "分卷规划卷名重复", details={"titles": titles}))


def check_outline_consistency(payload: Any, *, level: str = "book") -> OutlineConsistencyReport:
    text = _payload_text(payload)
    issues: list[OutlineConsistencyIssue] = []
    facts: dict[str, Any] = {}

    if isinstance(payload, dict) and str(payload.get("_quality_status") or "") in {
        "degraded_structural_draft",
        "invalidated_degraded_draft",
    }:
        issues.append(OutlineConsistencyIssue("degraded_payload", "大纲是降级结构草稿，不能作为正式生成依据"))

    role_names = extract_role_names(text, payload)
    facts["role_names"] = role_names
    for role, names in role_names.items():
        if len(names) > 1:
            issues.append(
                OutlineConsistencyIssue(
                    "role_name_conflict",
                    f"同一角色槽位出现多个姓名：{role}={','.join(names)}",
                    details={"role": role, "names": names},
                )
            )

    plan = _volume_plan_from_payload(payload)
    facts["volume_plan_count"] = len(plan)
    _check_volume_plan(plan, issues)

    if level == "book" and isinstance(payload, dict):
        raw = str(payload.get("raw_text") or payload.get("full_outline") or "")
        open_count = raw.count("<volume-plan>")
        close_count = raw.count("</volume-plan>")
        if open_count or close_count:
            issues.append(
                OutlineConsistencyIssue(
                    "volume_plan_tags_visible",
                    "用户可见大纲正文残留 <volume-plan> 控制标签",
                    details={"open_count": open_count, "close_count": close_count},
                )
            )
        if plan and "七、分卷规划" in raw:
            # 2026-11-21 修复:原判定是"全文里『第X卷』提及次数 >= len(plan) * 16",
            # 卷数少时阈值低得离谱(实测模型只给 2 卷时阈值仅 32),正文正常描述多卷
            # 就会被判成"残留多套分卷方案"→ 书纲永远保存不了(连锁导致第 2 卷起无法生成)。
            # 改为检测**真正的多套特征**:同一卷号在"分卷规划"一节里重复出现。
            section = raw.split("七、分卷规划", 1)[1]
            section = re.split(r"\n\s*[八九十]、", section, 1)[0]
            # 只认**行首**的卷号条目(列表项/标题),句中引用("承接第一卷的钩子")、
            # "第一卷到第二卷"这类跨度表述都不计 —— 否则正常回指会被算成第二套方案。
            tokens = re.findall(r"(?m)^\s*第([一二三四五六七八九十\d]+)卷", section)
            numbers = [n for n in (_volume_number(t) for t in tokens) if n is not None]
            facts["volume_mentions_in_section"] = len(numbers)
            # 2026-11-21 二次修复:同一卷号在标题/概述/要点里被正常重复提及(1,1,1)不是多套方案。
            # 真正的多套方案特征 = 卷号序列**回绕** —— 出现过更大的卷号之后又回到小卷号
            # (例如 1,2,3,1,2,3)。原判定用「重复出现」,实测模型只规划 1 卷时把
            # 「第一卷」在标题+概述里写两遍就被误杀。
            # 2026-11-21 四次修复。真正的多套方案特征是**以 1 开头的连续递增段
            # (1,2,3…)出现了 ≥2 次**。逐个排除过:
            #   - "同一卷号重复提及"(1,1,1)      → 正常
            #   - 回指(1,2,1 / 1,3,2)            → 正常("第二卷承接第一卷")
            #   - 单个 1 后面没有 2 的段           → 不算一段
            # 只有完整重现整个序列(1,2,3,1,2,3)才是残留了第二套方案。
            runs = 0
            run_len = 0
            expect = 1
            for number in numbers:
                if number == expect:
                    run_len += 1
                    expect += 1
                elif number == 1:
                    if run_len >= 2:
                        runs += 1
                    run_len = 1
                    expect = 2
            if run_len >= 2:
                runs += 1
            if runs >= 2:
                facts["volume_plan_restarts"] = runs
                issues.append(
                    OutlineConsistencyIssue(
                        "possible_multiple_volume_plans",
                        "分卷规划一节里以第一卷开头的连续卷号段出现多次,疑似残留多套分卷方案",
                        details={"restarts": runs,
                                 "volume_numbers": numbers[:40],
                                 "volume_plan_count": len(plan)},
                    )
                )


    for pattern in ALTERNATIVE_PLAN_PATTERNS:
        match = re.search(pattern, text)
        if match:
            issues.append(
                OutlineConsistencyIssue(
                    "alternative_plan_residue",
                    "大纲中残留候选/备选方案表述，不能作为唯一支撑库",
                    details={"match": match.group(0)},
                )
            )
            break

    if _is_shenyi_payload(payload, text):
        _check_shenyi_logic_anchors(text, issues)

    return OutlineConsistencyReport(ok=not any(i.severity == "error" for i in issues), issues=issues, facts=facts)


def validate_outline_consistency(payload: Any, *, level: str = "book") -> OutlineConsistencyReport:
    report = check_outline_consistency(payload, level=level)
    if not report.ok:
        raise OutlineConsistencyError(report)
    return report


def annotate_outline_consistency(payload: dict[str, Any], *, level: str = "book") -> dict[str, Any]:
    report = check_outline_consistency(payload, level=level)
    annotated = dict(payload)
    annotated["_consistency_report"] = report.to_dict()
    if not report.ok:
        annotated["_quality_status"] = "invalidated_consistency_failed"
    return annotated
