"""One-off regeneration for the Shenyi project outlines.

This script generates a fresh book outline and volume outlines in memory,
then replaces persisted outline rows only after generation succeeds. Existing
chapter body text is preserved; chapter titles/summaries/outline_json are
updated from the regenerated volume outlines.
"""

from __future__ import annotations

import asyncio
import argparse
import hashlib
import json
import logging
import math
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.session import async_session_factory, dispose_current_engine_async
from app.models.project import Chapter, Outline, Project, Volume
from app.services.model_router import get_model_router_async
from app.services.outline_generator import (
    BOOK_OUTLINE_CHARACTERS_SYSTEM,
    BOOK_OUTLINE_SKELETON_SYSTEM,
    BOOK_OUTLINE_WORLD_SYSTEM,
    BOOK_STAGE_MIN_CHARS,
    OUTLINE_BOOK_CONTRACT_PROMPT,
    OUTLINE_FAST_CALL_TIMEOUT_SECONDS,
    OUTLINE_LLM_CALL_TIMEOUT_SECONDS,
    OutlineGenerator,
    compute_scale,
)


PROJECT_ID = uuid.UUID("1bb6e0dd-e2d3-4eef-af70-f4a018e93f67")
OUTLINE_MAX_TOKENS = 8192
BOOK_SECTION_MAX_TOKENS = 8192
BOOK_FOCUS_SECTION_MAX_TOKENS = 2400
BOOK_STAGE_MAX_TOKENS = 700
BOOK_STAGE_CALL_TIMEOUT_SECONDS = 45
BOOK_LLM_FAILURE_THRESHOLD = 3
BOOK_LLM_FAILURES = 0
BOOK_LLM_DEGRADED = False
BOOK_LOCAL_FALLBACK_USED = False
BOOK_LOCAL_FALLBACK_REASONS: list[str] = []
ALLOW_DEGRADED_PERSIST = False
BOOK_MIN_CHARS = 30000
BOOK_SECTION_MIN_CHARS = {
    "一": 1800,
    "二": 5200,
    "三": 2600,
    "四": 2600,
    "五": 2600,
    "六": 6500,
    "七": 5600,
    "八": 2500,
    "九": 1200,
}
BOOK_SECTION_TITLES = {
    "一": "一、书名与核心概念",
    "二": "二、主要角色与小传",
    "三": "三、主角能力成长表",
    "四": "四、角色关系网",
    "五": "五、势力格局",
    "六": "六、世界观设定集",
    "七": "七、分卷规划",
    "八": "八、核心伏笔",
    "九": "九、基调与类型标签",
}
BANNED_PLACEHOLDER_TERMS = ["待补充", "TODO", "TBD", "待定", "若干", "一系列事件", "逐步展开", "此处略", "省略", "从略"]
BOOK_FULL_MAX_ATTEMPTS = 1
BOOK_SECTION_TARGET_MULTIPLIER = 1.45
BOOK_ENABLE_PREGEN_STAGES = False
BOOK_CHECKPOINT_PATH = Path(__file__).resolve().with_name(".shenyi_book_outline_checkpoint.json")
BOOK_CHECKPOINT_VERSION = 3
BOOK_USE_CHECKPOINT = True
BOOK_CHECKPOINT_STATE: dict[str, Any] | None = None
SHENYI_CANONICAL_FACTS_PROMPT = (
    "\n\n【《神裔》固定事实锁，严禁改写】"
    "主角只能叫林照，男，18岁；长期住在东港市‘旧环十二区’拆迁延迟区；"
    "初始生活来源是兼职快递与夜间维修学徒。"
    "故事起点是城市异常‘回声塌陷’，不要写‘第0次’；"
    "午夜整栋老楼发生空间折叠，居民被短暂替换为非人结构体。"
    "林照逃生濒死时第一次出现血脉激活征兆：右臂皮肤出现裂纹状‘银色神经纹路’。"
    "救出林照的女性高阶血脉者只能叫沈听澜；她是暗面学院外勤导师级成员，"
    "任务是回收异常觉醒个体并评估是否纳入学院体系。"
    "父亲只能叫林观澜，母亲只能叫叶清；官方清除机构称清除署；终局意识体称无名神核。"
    "记忆代价不得抹除主角寻找父母的核心动机；必须设置铁质维修工牌、机械录音带、快递单背面维修节点图这类不参与血脉共鸣的物理锚点；维修节点图必须伪装成老楼电路布线图和管网维修单，以阻值、断点与节点图保存低语义线索。"
    "旧神残骸不能被现代概念抹除协议彻底覆盖的原因必须是：协议覆盖语义记录，但低成本覆盖不了非语义物理残差；林照必须是可安全过滤并读取旧神残骸频率的异常滤波接口和活体节点；活体接口的战术具象化必须写成频率滤波、协议空拍、权限错位和三秒延迟，不能写成直接攻击力；暗面学院保他是为反向解析清除署协议漏洞并争取自治权；出生记录是林照人类身份根权限，一旦被清空会身份索引归零，成为无主接口并被无名神核强行接管；旧环十二区地下必须连接被污染的旧式城市管网，重型机械破坏地基会触发链式空间折叠；第二、三卷必须保留快递路线、维修工单、地下管网巡检和物理泄漏点的日常轨迹；第二卷末必须触发底层自检、证据衰变、反向追踪与物理抹除倒计时。"
    "分卷规划只能保留一套五卷主轴，不得输出多套候选分卷。"
    "不得把主角改成林烨、林澈、林烬、林序、林越、陆衍、林烁、林曜、沈曜；"
    "不得把救援者改成沈知夏、沈知微、林霁雪、沈清绫、沈清雾、沈观雪、沈清栀、沈澜、苏弥、苏岚、苏清澜、沈雾岚、林绯月、沈霁；"
    "不得把父母改成林岳、林承岳、林昭、林远山、苏晚晴、苏晚、苏璃；"
    "不得把城市改成云港市、临港市、海东市、夜港市、云城、澜城，"
    "不得把起点改成南槐里、槐安里、春和苑、锦安里、地下电梯坠落、青铜钥匙或熵烬残血。"
)
SHENYI_BANNED_DRIFT_TERMS = [
    "林烨",
    "林夜",
    "陆沉",
    "林澈",
    "林烬",
    "林序",
    "林越",
    "陆衍",
    "林烁",
    "林曜",
    "沈曜",
    "沈知夏",
    "沈知微",
    "林霁雪",
    "沈清绫",
    "沈清雾",
    "沈观雪",
    "沈清栀",
    "沈澜",
    "沈岚",
    "林夜澜",
    "苏湄",
    "苏弥",
    "苏岚",
    "沈观澜",
    "顾沉璃",
    "沈璃",
    "苏清澜",
    "沈雾岚",
    "林绯",
    "林绯月",
    "沈霁",
    "顾承锋",
    "姬玄烬",
    "林岳",
    "林承岳",
    "林昭",
    "林远山",
    "苏晚晴",
    "苏晚",
    "苏璃",
    "云港市",
    "临港市",
    "海东市",
    "夜港市",
    "云城",
    "澜城",
    "东城老小区",
    "海棠里",
    "南槐里",
    "槐安里",
    "春和苑",
    "锦安里",
    "地下电梯",
    "青铜钥匙",
    "熵烬残血",
    "第0次",
]
SHENYI_ENTITY_REGISTRY = {
    "protagonist": "林照",
    "mentor": "沈听澜",
    "father": "林观澜",
    "mother": "叶清",
    "city": "东港市",
    "origin_location": "旧环十二区",
    "origin_event": "回声塌陷",
    "academy": "暗面学院",
    "cleanup_authority": "清除署",
    "final_antagonist": "无名神核",
    "bloodline_mark": "银色神经纹路",
}
SHENYI_REQUIRED_BOOK_TERMS = tuple(SHENYI_ENTITY_REGISTRY.values())
SHENYI_ANCHOR_TERMS = ("铁质维修工牌", "机械录音带", "维修节点图", "电路布线图", "管网维修单", "阻值", "节点图", "异常滤波接口", "活体节点", "频率滤波", "协议空拍", "权限错位", "三秒延迟", "反向解析", "自治权", "出生记录", "身份索引归零", "无主接口", "强行接管", "旧式城市管网", "重型机械", "地基", "链式空间折叠", "快递路线", "维修工单", "地下管网巡检", "物理泄漏点", "证据衰变", "反向追踪", "底层自检", "物理抹除")
SHENYI_VOLUME_PLAN_TITLES = (
    "血脉苏醒：旧环十二区回声塌陷",
    "暗面学院：规则、训练与档案裂口",
    "旧神遗痕：残骸、锚点与父母轨迹",
    "血裔内战：学院分裂与清除署追捕",
    "神名归零：真相闭环与终局觉醒",
)
WRITING_GUIDE_PROMPT = (
    "写作质量准则：先校准硬事实，再输出内容；人物身份、关系、动机、伤势、筹码、时间线不能自相矛盾。"
    "不要用大量环境描写、华丽词藻、比喻修辞强行凑字数；扩写要靠具体行动、选择代价、人物关系变化、任务进度和伏笔流转。"
    "保持故事感和正在发生的动作感，语言直接自然，避免文艺腔、模板腔、机械总结。"
    "每段只围绕本段职责展开，不把其他段落内容强行塞进来；发现问题时只修正问题点，不推翻已合格内容。"
)


def section_target_chars(min_chars: int) -> int:
    return max(min_chars + 900, math.ceil(min_chars * BOOK_SECTION_TARGET_MULTIPLIER))


def section_quality_brief(section_num: str, scale: dict[str, Any] | None) -> str:
    n_volumes = int(scale.get("n_volumes") or 5) if scale else 5
    n_chapters = int(scale.get("n_chapters") or 750) if scale else 750
    chapters_per_volume = int(scale.get("chapters_per_volume") or 150) if scale else 150
    briefs = {
        "一": "必须写清一句话卖点、核心矛盾、主角初始处境、终局承诺、类型爽点、读者期待管理，不能只写概念。",
        "二": "至少列出主角、两名核心盟友、两名阶段敌人、终局敌人、关键导师/亲属/背叛者；每人必须有目标、秘密、能力、关系变化和首次登场作用。",
        "三": "必须按早期、中期、后期、终局拆出能力阶段；每阶段写触发事件、代价、限制、失败教训、代表战斗和下一阶段钩子。",
        "四": "必须写清主角与盟友、敌人、家族/宗门/王朝、隐藏势力之间的关系变化；每条关系要有冲突来源、反转节点和最终归宿。",
        "五": "至少写 8 个势力；每个势力必须有领袖、资源、目标、公开立场、隐藏筹码、与主角的利益交换、卷内变化。",
        "六": "必须覆盖修炼体系、血脉/神裔来源、地理层级、政治秩序、经济资源、禁忌规则、历史断层、日常生活和战争方式。",
        "七": f"必须严格规划 {n_volumes} 卷、总计 {n_chapters} 章、每卷约 {chapters_per_volume} 章；每卷写核心冲突、开局状态、中段转折、结尾爆点、主角成长和伏笔回收。",
        "八": "至少写 12 条核心伏笔；每条必须包含埋设位置、表层解释、真实含义、误导方式、回收卷数、回收后造成的剧情后果。",
        "九": "必须写叙事基调、情绪曲线、战斗风格、权谋密度、感情线尺度、幽默/压抑比例、禁用写法和类型标签。",
    }
    return briefs[section_num]


def normalize_section_heading(text: str, section_num: str) -> str:
    stripped = text.strip()
    title = BOOK_SECTION_TITLES[section_num]
    if stripped.startswith(title):
        lines = stripped.splitlines()
    elif stripped.startswith(section_num + "、"):
        lines = stripped.splitlines()
        if lines:
            lines[0] = title
    else:
        lines = [title, *stripped.splitlines()] if stripped else []
    if not lines:
        return ""
    cleaned = [lines[0]]
    # Avoid confusing the top-level section parser with inner subheads like "一、".
    for line in lines[1:]:
        inner = line.lstrip()
        prefix = line[: len(line) - len(inner)]
        for num in BOOK_SECTION_TITLES:
            if inner.startswith(num + "、"):
                line = prefix + "（" + num + "）" + inner[2:]
                break
        cleaned.append(line)
    return "\n".join(cleaned).strip()


def shenyi_drift_hits(text: str) -> list[str]:
    return [word for word in SHENYI_BANNED_DRIFT_TERMS if word in text]


def shenyi_text_gate_errors(
    text: str,
    *,
    require_book_terms: bool = False,
    require_anchor_terms: bool = False,
    require_volume_plan_tags: bool = False,
) -> list[str]:
    errors: list[str] = []
    drift_hits = shenyi_drift_hits(text)
    if drift_hits:
        errors.append("含《神裔》设定漂移词：" + ",".join(drift_hits))
    if require_book_terms:
        missing = [term for term in SHENYI_REQUIRED_BOOK_TERMS if term not in text]
        if missing:
            errors.append("缺少全局实体锚点：" + ",".join(missing))
    if require_anchor_terms:
        missing_anchors = [term for term in SHENYI_ANCHOR_TERMS if term not in text]
        if missing_anchors:
            errors.append("缺少记忆代价物理锚点：" + ",".join(missing_anchors))
    if require_volume_plan_tags:
        open_count = text.count("<volume-plan>")
        close_count = text.count("</volume-plan>")
        if open_count != 1 or close_count != 1:
            errors.append(f"volume_plan_tag_count:{open_count}/{close_count}，必须唯一")
    return errors


def assert_shenyi_text_gate(
    text: str,
    context: str,
    *,
    require_book_terms: bool = False,
    require_anchor_terms: bool = False,
    require_volume_plan_tags: bool = False,
) -> None:
    errors = shenyi_text_gate_errors(
        text,
        require_book_terms=require_book_terms,
        require_anchor_terms=require_anchor_terms,
        require_volume_plan_tags=require_volume_plan_tags,
    )
    if errors:
        raise RuntimeError(f"{context} consistency gate failed: " + "; ".join(errors))


def section_validation_errors(text: str, section_num: str, min_chars: int) -> list[str]:
    errors: list[str] = []
    title = BOOK_SECTION_TITLES[section_num]
    if not text.lstrip().startswith(title):
        errors.append("标题不是精确标题")
    if len(text) < min_chars:
        errors.append(f"长度 {len(text)} 低于最低 {min_chars}")
    placeholder_hits = [word for word in BANNED_PLACEHOLDER_TERMS if word in text]
    if placeholder_hits:
        errors.append("含占位词：" + ",".join(placeholder_hits))
    errors.extend(shenyi_text_gate_errors(text, require_volume_plan_tags=(section_num == "七")))
    return errors


def deterministic_section_supplement(section_num: str, min_chars: int, scale: dict[str, Any] | None) -> str:
    """Diagnostic-only local patch used when the endpoint under-delivers."""
    mark_local_fallback(f"section_{section_num}_local_supplement")
    title = BOOK_SECTION_TITLES[section_num]
    scale_note = ""
    if scale:
        scale_note = (
            f"本书按 {scale.get('n_volumes') or 5} 卷、{scale.get('n_chapters') or 750} 章执行，"
            f"每卷约 {scale.get('chapters_per_volume') or 150} 章。"
        )
    text = title + "\n" + "\n".join(
        [
            "【本地诊断占位】模型端点没有返回可用内容，本脚本不会在本地编造角色、势力、地名、卷名或情节。",
            f"项目规模约束：{scale_note or '未读取到规模信息。'}",
            f"本段生成职责：{section_quality_brief(section_num, scale)}",
            "处理建议：修复端点空响应、限流或提示词适配问题后，使用真实模型输出重新生成本段。",
        ]
    )
    generic = [
        "诊断原则：本地兜底只能标记失败原因，不能替模型创作《神裔》的具体内容。",
        "质量原则：大纲必须来自项目设定和模型生成结果，不能混入脚本硬编码设定。",
    ]
    i = 0
    while len(text) < min_chars:
        text += "\n" + generic[i % len(generic)]
        i += 1
    return text


def reinforce_section_with_local_patch(
    text: str,
    section_num: str,
    min_chars: int,
    scale: dict[str, Any] | None,
) -> str:
    """Append deterministic, section-specific content when the endpoint under-delivers."""
    text = normalize_section_heading(text, section_num)
    errors = section_validation_errors(text, section_num, min_chars)
    if not errors:
        return text
    supplement = deterministic_section_supplement(section_num, max(700, min_chars - len(text)), scale)
    supplement_lines = supplement.splitlines()
    supplement_body = "\n".join(supplement_lines[1:]).strip() if len(supplement_lines) > 1 else supplement
    if text.strip():
        reinforced = text.rstrip() + "\n" + supplement_body
    else:
        reinforced = supplement
    return normalize_section_heading(reinforced, section_num)

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")


def log(message: str) -> None:
    now = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{now}] {message}", flush=True)


def record_book_llm_failure(reason: str) -> None:
    global BOOK_LLM_DEGRADED, BOOK_LLM_FAILURES
    BOOK_LLM_FAILURES += 1
    if not BOOK_LLM_DEGRADED and BOOK_LLM_FAILURES >= BOOK_LLM_FAILURE_THRESHOLD:
        BOOK_LLM_DEGRADED = True
        log(f"全书大纲模型熔断：连续失败 {BOOK_LLM_FAILURES} 次，原因：{reason}；本轮改用本地结构化补强")
    else:
        log(f"全书大纲模型失败计数 {BOOK_LLM_FAILURES}/{BOOK_LLM_FAILURE_THRESHOLD}：{reason}")


def mark_local_fallback(reason: str) -> None:
    global BOOK_LOCAL_FALLBACK_USED
    BOOK_LOCAL_FALLBACK_USED = True
    if reason not in BOOK_LOCAL_FALLBACK_REASONS:
        BOOK_LOCAL_FALLBACK_REASONS.append(reason)


def stop_if_degraded(stage: str) -> None:
    if ALLOW_DEGRADED_PERSIST:
        return
    if BOOK_LLM_DEGRADED or BOOK_LOCAL_FALLBACK_USED:
        reasons = ",".join(BOOK_LOCAL_FALLBACK_REASONS) or "model_degraded"
        raise RuntimeError(
            f"{stage} stopped: 本轮生成已经依赖本地兜底或触发模型熔断，"
            f"原因={reasons}；默认停止写库，避免把结构草稿误当高质量大纲。"
        )


def as_text(value: Any, limit: int | None = None) -> str:
    text = "" if value is None else str(value).strip()
    if limit and len(text) > limit:
        return text[:limit].rstrip()
    return text


def book_checkpoint_key(project: Project, scale: dict[str, Any] | None) -> str:
    payload = {
        "version": BOOK_CHECKPOINT_VERSION,
        "project_id": str(project.id),
        "title": project.title,
        "genre": project.genre,
        "premise": project.premise,
        "target_word_count": project.target_word_count,
        "scale": scale or {},
        "section_min_chars": BOOK_SECTION_MIN_CHARS,
        "pregen_stages": BOOK_ENABLE_PREGEN_STAGES,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def load_book_checkpoint(project: Project, scale: dict[str, Any] | None) -> dict[str, Any]:
    if not BOOK_USE_CHECKPOINT or not BOOK_CHECKPOINT_PATH.exists():
        return {}
    try:
        data = json.loads(BOOK_CHECKPOINT_PATH.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        log(f"读取全书大纲断点失败，将忽略旧断点：{type(exc).__name__}: {exc}")
        return {}
    if not isinstance(data, dict):
        return {}
    if data.get("checkpoint_key") != book_checkpoint_key(project, scale):
        log("全书大纲断点与当前项目/规模/门禁不匹配，已忽略")
        return {}
    return data


def save_book_checkpoint(data: dict[str, Any]) -> None:
    if not BOOK_USE_CHECKPOINT:
        return
    BOOK_CHECKPOINT_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = BOOK_CHECKPOINT_PATH.with_suffix(BOOK_CHECKPOINT_PATH.suffix + ".tmp")
    temp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(BOOK_CHECKPOINT_PATH)


def get_book_batch_checkpoint(section_num: str, label: str, min_chars: int) -> str:
    if not BOOK_USE_CHECKPOINT or not isinstance(BOOK_CHECKPOINT_STATE, dict):
        return ""
    batches = BOOK_CHECKPOINT_STATE.get("section_batches")
    if not isinstance(batches, dict):
        return ""
    section_batches = batches.get(section_num)
    if not isinstance(section_batches, dict):
        return ""
    body = as_text(section_batches.get(label))
    if body and shenyi_text_gate_errors(body):
        log(f"全书大纲段落 {section_num} 批次 {label} 断点含设定漂移，已忽略")
        return ""
    return body if len(body) >= min_chars else ""


def save_book_batch_checkpoint(section_num: str, label: str, body: str) -> None:
    if not BOOK_USE_CHECKPOINT or not isinstance(BOOK_CHECKPOINT_STATE, dict):
        return
    assert_shenyi_text_gate(body, f"book_outline_section_{section_num}_{label}_checkpoint")
    batches = BOOK_CHECKPOINT_STATE.setdefault("section_batches", {})
    if not isinstance(batches, dict):
        batches = {}
        BOOK_CHECKPOINT_STATE["section_batches"] = batches
    section_batches = batches.setdefault(section_num, {})
    if not isinstance(section_batches, dict):
        section_batches = {}
        batches[section_num] = section_batches
    section_batches[label] = body
    BOOK_CHECKPOINT_STATE["updated_at"] = datetime.now(timezone.utc).isoformat()
    save_book_checkpoint(BOOK_CHECKPOINT_STATE)


def clear_book_checkpoint() -> None:
    if BOOK_CHECKPOINT_PATH.exists():
        BOOK_CHECKPOINT_PATH.unlink()


async def generate_text_with_retries(
    generator: OutlineGenerator,
    *,
    stage_name: str,
    min_chars: int,
    messages: list[dict[str, str]],
    attempts: int = 2,
    max_tokens: int = BOOK_STAGE_MAX_TOKENS,
    timeout_seconds: int = BOOK_STAGE_CALL_TIMEOUT_SECONDS,
) -> str:
    last_text = ""
    best_text = ""
    if BOOK_LLM_DEGRADED:
        log(f"全书大纲{stage_name}跳过模型调用：本轮模型已熔断")
        return ""
    for stage_attempt in range(1, attempts + 1):
        try:
            result = await asyncio.wait_for(
                generator.router.generate(
                    task_type="outline_book",
                    messages=messages,
                    max_tokens=max_tokens,
                    stream=False,
                    request_timeout=timeout_seconds,
                    retry_attempts=1,
                ),
                timeout=timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            log(f"全书大纲{stage_name}第 {stage_attempt} 次异常：{type(exc).__name__}: {exc!r}")
            record_book_llm_failure(f"{stage_name} {type(exc).__name__}")
            if BOOK_LLM_DEGRADED:
                break
            continue
        text = as_text(getattr(result, "text", ""))
        if not text.strip():
            record_book_llm_failure(f"{stage_name} empty_response")
        last_text = text
        if len(text) > len(best_text):
            best_text = text
        log(f"全书大纲{stage_name}第 {stage_attempt} 次返回，长度 {len(text)} 字")
        if len(text) >= min_chars:
            return text
        if BOOK_LLM_DEGRADED:
            break
        messages = [dict(item) for item in messages]
        messages.append(
            {
                "role": "user",
                "content": (
                    f"上一轮{stage_name}只有 {len(text)} 字，低于最低 {min_chars} 字。"
                    "请只重写这一阶段，不要解释，不要道歉；必须输出完整、具体、可执行内容。"
                ),
            }
        )
    return best_text or last_text


def normalize_plan_item(item: dict[str, Any], idx: int, chapters_per_volume: int) -> dict[str, Any]:
    canonical_title = SHENYI_VOLUME_PLAN_TITLES[idx - 1] if 1 <= idx <= len(SHENYI_VOLUME_PLAN_TITLES) else f"第{idx}卷"
    return {
        "idx": idx,
        "title": as_text(item.get("title")) or canonical_title,
        "theme": as_text(item.get("theme")),
        "core_conflict": as_text(item.get("core_conflict")),
        "est_chapters": int(item.get("est_chapters") or chapters_per_volume or 10),
    }


def fallback_volume_plan(scale: dict[str, Any] | None) -> list[dict[str, Any]]:
    expected = int(scale.get("n_volumes") or 5) if scale else 5
    chapters_per_volume = int(scale.get("chapters_per_volume") or 150) if scale else 150
    total_chapters = int(scale.get("n_chapters") or expected * chapters_per_volume) if scale else expected * chapters_per_volume
    plan: list[dict[str, Any]] = []
    for idx in range(1, expected + 1):
        remaining = total_chapters - chapters_per_volume * (expected - 1)
        est_chapters = remaining if idx == expected else chapters_per_volume
        title = SHENYI_VOLUME_PLAN_TITLES[idx - 1] if 1 <= idx <= len(SHENYI_VOLUME_PLAN_TITLES) else f"第{idx}卷"
        plan.append(
            {
                "idx": idx,
                "title": title,
                "theme": title.split("：", 1)[-1],
                "core_conflict": f"围绕{title}推进林照、沈听澜、暗面学院、清除署与无名神核的主线冲突",
                "est_chapters": est_chapters,
            }
        )
    return plan


def extract_sections(generator: OutlineGenerator, text: str) -> dict[str, str]:
    return {num: body.strip() for num, body in generator._iter_sections(text or "")}


def format_book_quality_errors(
    generator: OutlineGenerator,
    book_outline: dict[str, Any],
    scale: dict[str, Any] | None,
    *,
    min_chars: int = BOOK_MIN_CHARS,
) -> list[str]:
    text = as_text(book_outline.get("raw_text") or book_outline.get("full_outline"))
    sections = extract_sections(generator, text)
    errors: list[str] = []
    if len(text) < min_chars:
        errors.append(f"too_short:{len(text)}<{min_chars}")
    missing = [num for num in BOOK_SECTION_TITLES if num not in sections]
    if missing:
        errors.append("missing_sections:" + ",".join(missing))
    for num, minimum in BOOK_SECTION_MIN_CHARS.items():
        length = len(sections.get(num, ""))
        if length < minimum:
            errors.append(f"section_{num}_too_short:{length}<{minimum}")
    volume_plan = book_outline.get("volume_plan")
    expected_volumes = int(scale.get("n_volumes") or 0) if scale else 0
    expected_chapters = int(scale.get("n_chapters") or 0) if scale else 0
    if expected_volumes:
        if not isinstance(volume_plan, list) or len(volume_plan) != expected_volumes:
            count = len(volume_plan) if isinstance(volume_plan, list) else 0
            errors.append(f"volume_plan_count:{count}<{expected_volumes}")
        elif expected_chapters:
            total = sum(int(item.get("est_chapters") or 0) for item in volume_plan if isinstance(item, dict))
            if total != expected_chapters:
                errors.append(f"volume_plan_chapters:{total}!={expected_chapters}")
    hit = [word for word in BANNED_PLACEHOLDER_TERMS if word in text]
    if hit:
        errors.append("placeholder_terms:" + ",".join(hit))
    errors.extend(shenyi_text_gate_errors(text, require_book_terms=True, require_anchor_terms=True))
    plan = book_outline.get("volume_plan")
    if isinstance(plan, list):
        titles = [as_text(item.get("title")) for item in plan if isinstance(item, dict)]
        missing_titles = [title for title in SHENYI_VOLUME_PLAN_TITLES if title not in titles]
        if missing_titles:
            errors.append("volume_plan_title_drift:" + ",".join(missing_titles))
    return errors


def validate_shenyi_book_outline_payload(
    generator: OutlineGenerator,
    book_outline: dict[str, Any],
    scale: dict[str, Any] | None,
    context: str,
) -> dict[str, Any]:
    errors = format_book_quality_errors(generator, book_outline, scale)
    if errors:
        raise RuntimeError(f"{context} quality gate failed: " + "; ".join(errors))
    enriched = dict(book_outline)
    enriched["entity_registry"] = dict(SHENYI_ENTITY_REGISTRY)
    return enriched


def validate_shenyi_volume_outline(outline: dict[str, Any], context: str) -> None:
    payload_text = json.dumps(outline, ensure_ascii=False, sort_keys=True)
    assert_shenyi_text_gate(payload_text, context)


async def expand_book_section(
    generator: OutlineGenerator,
    *,
    user_input: str,
    base_outline: str,
    section_num: str,
    min_chars: int,
    scale: dict[str, Any] | None,
    focus: str | None = None,
    attempts: int = 2,
) -> str:
    title = BOOK_SECTION_TITLES[section_num]
    request_min_chars = min_chars
    target_chars = section_target_chars(request_min_chars)
    if focus:
        target_chars = max(request_min_chars + 300, math.ceil(request_min_chars * 1.25))
    quality_brief = section_quality_brief(section_num, scale)
    scale_note = ""
    if scale:
        scale_note = (
            f"本书目标 {scale.get('target_word_count')} 字，"
            f"必须规划 {scale.get('n_volumes')} 卷、{scale.get('n_chapters')} 章，"
            f"每卷约 {scale.get('chapters_per_volume')} 章。"
        )
    focus_note = f"\n本次聚焦要求：{focus}" if focus else ""
    system = (
        "你是中文长篇小说总纲主编。只输出指定段落正文，不要 Markdown 代码块，不要解释。"
        "必须保留段落标题原编号，内容要具体、可执行、可落库。"
        "禁止使用略、待补充、若干、一系列事件、逐步展开等占位词。"
        "生成前先在心里检查：标题精确、字数达标、每条设定有事件或人物承载、没有空泛总结；最终只输出正文。"
        + WRITING_GUIDE_PROMPT
    )
    user = (
        f"项目信息：\n{user_input}\n\n"
        f"当前基础全书大纲：\n{base_outline[:16000]}\n\n"
        f"{scale_note}\n"
        f"请为《{title}》这一段生成正式可落库内容，最低 {request_min_chars} 个中文字符，目标 {target_chars} 个中文字符。"
        f"\n本段硬性内容清单：{quality_brief}"
        f"{focus_note}"
        "\n只输出这一段，第一行必须精确等于该段标题。"
        "\n不要用总括性空话凑字；每个判断都要落到具体人物、地点、势力、物件、事件或代价。"
    )
    if section_num == "七":
        expected_volumes = int(scale.get("n_volumes") or 5) if scale else 5
        expected_chapters = int(scale.get("n_chapters") or 750) if scale else 750
        user += (
            "\n分卷规划段必须包含每卷核心冲突、关键转折、主角状态变化、阶段性失败/胜利、伏笔流转，"
            f"并在段末输出严格 <volume-plan> JSON 块；JSON 必须正好 {expected_volumes} 卷，总章数 {expected_chapters}。"
        )
    base_user = user
    last_text = ""
    best_text = ""
    best_score = -10_000
    if BOOK_LLM_DEGRADED:
        reinforced = reinforce_section_with_local_patch("", section_num, min_chars, scale)
        log(f"全书大纲段落 {section_num} 跳过模型调用：本轮模型已熔断，使用本地结构化补强，长度 {len(reinforced)} 字")
        return reinforced
    for attempt in range(1, attempts + 1):
        focus_label = f"，聚焦：{focus[:24]}" if focus else ""
        log(f"开始生成全书大纲段落 {section_num} 第 {attempt} 次，目标 {target_chars} 字{focus_label}")
        try:
            result = await asyncio.wait_for(
                generator.router.generate(
                    task_type="outline_book",
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    max_tokens=BOOK_FOCUS_SECTION_MAX_TOKENS if focus else BOOK_SECTION_MAX_TOKENS,
                    temperature=0.35,
                    stream=False,
                    request_timeout=OUTLINE_FAST_CALL_TIMEOUT_SECONDS,
                    retry_attempts=1,
                ),
                timeout=OUTLINE_FAST_CALL_TIMEOUT_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001
            log(f"全书大纲段落 {section_num} 第 {attempt} 次异常：{type(exc).__name__}: {exc!r}")
            record_book_llm_failure(f"段落 {section_num} {type(exc).__name__}")
            if BOOK_LLM_DEGRADED:
                break
            continue
        text = normalize_section_heading(as_text(getattr(result, "text", "")), section_num)
        if not text.strip():
            record_book_llm_failure(f"段落 {section_num} empty_response")
        last_text = text
        log(f"全书大纲段落 {section_num} 第 {attempt} 次返回，长度 {len(text)} 字")
        errors = section_validation_errors(text, section_num, request_min_chars)
        score = len(text) - 2000 * len(errors)
        if score > best_score:
            best_score = score
            best_text = text
        if not errors:
            return text
        if BOOK_LLM_DEGRADED:
            break
        retry_note = (
            "\n上一轮模型返回空内容。请降低修辞密度，直接列出具体人物、关系、目标、代价和事件；不要解释原因。"
            if not text
            else f"\n上一轮未通过：{'；'.join(errors)}。"
        )
        user = (
            base_user
            + retry_note
            + f"请重新输出《{title}》，第一行必须精确等于“{title}”，目标 {target_chars} 字。"
            + f"硬性内容清单仍然是：{quality_brief}"
            + "不要出现略、待补充、若干、一系列事件、逐步展开等占位词；所有地方都改成具体事件、人物、地点、物件或行动。"
        )
    if best_text:
        log(f"全书大纲段落 {section_num} 未完全达标，保留最佳候选，长度 {len(best_text)} 字")
        if len(best_text) >= math.floor(min_chars * 0.85):
            extended = await extend_short_section_with_model(
                generator,
                user_input=user_input,
                base_outline=base_outline,
                section_num=section_num,
                text=best_text,
                min_chars=min_chars,
                scale=scale,
                focus=focus,
            )
            if not section_validation_errors(extended, section_num, min_chars):
                log(f"全书大纲段落 {section_num} 通过模型追加补足，长度 {len(extended)} 字")
                return extended
        return best_text
    return last_text


async def extend_short_section_with_model(
    generator: OutlineGenerator,
    *,
    user_input: str,
    base_outline: str,
    section_num: str,
    text: str,
    min_chars: int,
    scale: dict[str, Any] | None,
    focus: str | None = None,
) -> str:
    missing = max(80, min_chars - len(text) + 160)
    title = BOOK_SECTION_TITLES[section_num]
    focus_note = f"\n本次聚焦要求：{focus}" if focus else ""
    system = (
        "你是中文长篇小说总纲编辑。只输出需要追加到原段落末尾的正文，不要重复段落标题，"
        "不要解释，不要道歉，不要 Markdown。追加内容必须具体、可落库，不得使用占位词。"
        + WRITING_GUIDE_PROMPT
    )
    user = (
        f"项目信息：\n{user_input}\n\n"
        f"当前基础全书大纲：\n{base_outline[:10000]}\n\n"
        f"原段落标题：{title}\n"
        f"原段落当前长度 {len(text)} 字，最低要求 {min_chars} 字。请只追加 {missing} 到 {missing + 240} 个中文字符，"
        "用于补足原段落的信息密度；不要重写已有内容，不要另起新的顶级编号。"
        f"\n本段硬性内容清单：{section_quality_brief(section_num, scale)}"
        f"{focus_note}\n"
        "追加内容必须落到具体人物、目标、代价、关系变化、任务推进或伏笔流转。"
    )
    try:
        result = await asyncio.wait_for(
            generator.router.generate(
                task_type="outline_book",
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                max_tokens=900,
                temperature=0.32,
                stream=False,
                request_timeout=OUTLINE_FAST_CALL_TIMEOUT_SECONDS,
                retry_attempts=1,
            ),
            timeout=OUTLINE_FAST_CALL_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001
        log(f"全书大纲段落 {section_num} 模型追加补足异常：{type(exc).__name__}: {exc!r}")
        return text
    extra = normalize_section_heading(as_text(getattr(result, "text", "")), section_num)
    lines = extra.splitlines()
    if lines and lines[0].strip() == title:
        extra = "\n".join(lines[1:]).strip()
    if not extra:
        log(f"全书大纲段落 {section_num} 模型追加补足返回空内容")
        return text
    return normalize_section_heading(text.rstrip() + "\n" + extra.strip(), section_num)


async def generate_volume_plan_json(
    generator: OutlineGenerator,
    *,
    user_input: str,
    skeleton_text: str,
    scale: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    expected = int(scale.get("n_volumes") or 5) if scale else 5
    chapters_per_volume = int(scale.get("chapters_per_volume") or 0) if scale else 0
    total_chapters = int(scale.get("n_chapters") or 0) if scale else 0
    if BOOK_LLM_DEGRADED:
        plan = fallback_volume_plan(scale)
        log(f"卷计划 JSON 跳过模型调用：本轮模型已熔断，使用确定性卷计划 {len(plan)} 卷")
        return plan
    plan_system = (
        "你是小说总纲规划师。只输出 JSON 数组，不要 Markdown，不要解释。"
        "数组每一项必须包含 idx、title、theme、core_conflict、est_chapters。"
        "title/theme/core_conflict 必须具体，不能写占位词；禁止使用“第1卷”“第1阶段主线推进”这类结构占位。"
    )
    plan_user = (
        f"项目信息：\n{user_input}\n\n已生成骨架：\n{skeleton_text}\n\n"
        f"请生成严格 {expected} 卷的结构化卷计划。总章数 {total_chapters}，"
        f"每卷约 {chapters_per_volume} 章；各卷 est_chapters 相加必须等于 {total_chapters}。"
        "必须锚定《神裔》的真实前提：18岁少年、老小区异常、女性高阶血脉救援者、暗面学院、父母失踪、旧神血裔、被抹除旧神。"
        "不要照抄骨架里可能存在的临时卷计划占位。"
    )
    last_error: str | None = None
    for attempt in range(1, 4):
        try:
            result = await asyncio.wait_for(
                generator.router.generate(
                    task_type="outline_book",
                    messages=[
                        {"role": "system", "content": plan_system},
                        {"role": "user", "content": plan_user},
                    ],
                    max_tokens=4096,
                    temperature=0.3,
                    stream=False,
                    request_timeout=OUTLINE_FAST_CALL_TIMEOUT_SECONDS,
                    retry_attempts=1,
                ),
                timeout=OUTLINE_FAST_CALL_TIMEOUT_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001
            last_error = f"{type(exc).__name__}: {exc!r}"
            log(f"卷计划 JSON 第 {attempt} 次异常：{last_error}")
            record_book_llm_failure(f"卷计划 {type(exc).__name__}")
            if BOOK_LLM_DEGRADED:
                plan = fallback_volume_plan(scale)
                log(f"卷计划 JSON 熔断后使用确定性卷计划 {len(plan)} 卷")
                return plan
            continue
        raw = as_text(getattr(result, "text", ""))
        if not raw.strip():
            record_book_llm_failure("卷计划 empty_response")
        raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        try:
            parsed = json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            last_error = f"JSON parse failed: {exc}"
            log(f"卷计划 JSON 第 {attempt} 次失败：{last_error}")
            continue
        if not isinstance(parsed, list):
            last_error = "not a list"
            log(f"卷计划 JSON 第 {attempt} 次失败：{last_error}")
            continue
        plan = [normalize_plan_item(item, idx, chapters_per_volume) for idx, item in enumerate(parsed, start=1) if isinstance(item, dict)]
        if len(plan) != expected:
            last_error = f"count mismatch {len(plan)}/{expected}"
            log(f"卷计划 JSON 第 {attempt} 次失败：{last_error}")
            continue
        generic_items = [
            item for item in plan
            if as_text(item.get("title")) in {f"第{item.get('idx')}卷", f"第{item.get('idx')}卷大纲未生成"}
            or "阶段主线推进" in as_text(item.get("theme"))
            or "阶段核心冲突" in as_text(item.get("core_conflict"))
        ]
        if generic_items:
            last_error = "generic placeholder volume plan"
            log(f"卷计划 JSON 第 {attempt} 次失败：{last_error}")
            continue
        if total_chapters:
            current_total = sum(int(item.get("est_chapters") or 0) for item in plan)
            if current_total != total_chapters:
                for idx, item in enumerate(plan, start=1):
                    remaining = total_chapters - chapters_per_volume * (expected - 1)
                    item["est_chapters"] = remaining if idx == expected else chapters_per_volume
        log(f"卷计划 JSON 完成：{len(plan)} 卷")
        return plan
    raise RuntimeError(f"Volume plan JSON generation failed: {last_error}")


async def generate_character_stage_sections(
    generator: OutlineGenerator,
    *,
    user_input: str,
    skeleton_text: str,
    scale: dict[str, Any] | None,
) -> str:
    base_outline = f"创意：\n{user_input}\n\n已生成的骨架：\n{skeleton_text}\n"
    parts: list[str] = []
    for section_num in ("二", "四", "五"):
        if section_num == "二":
            parts.append(
                await expand_character_biographies_section(
                    generator,
                    user_input=user_input,
                    base_outline=base_outline,
                    scale=scale,
                )
            )
        elif section_num == "四":
            parts.append(
                await expand_relationship_network_section(
                    generator,
                    user_input=user_input,
                    base_outline=base_outline,
                    scale=scale,
                )
            )
        else:
            parts.append(
                await expand_book_section(
                    generator,
                    user_input=user_input,
                    base_outline=base_outline,
                    section_num=section_num,
                    min_chars=BOOK_SECTION_MIN_CHARS[section_num],
                    scale=scale,
                )
            )
    return "\n\n".join(part.strip() for part in parts if part.strip())


def fallback_character_batch(label: str, focus: str, min_chars: int) -> str:
    """Return diagnostic-only text when character generation fails."""
    mark_local_fallback(f"character_batch_local_fallback:{label}")
    text = "\n".join(
        [
            f"{label}本地诊断占位：模型端点没有返回可用角色内容。",
            f"聚焦职责：{focus}",
            "本地兜底不得编造角色姓名、身份、关系、势力、地点或情节，避免把非项目设定写入《神裔》。",
            "处理建议：修复端点空响应、限流或提示词适配问题后，重新生成本组角色小传。",
        ]
    )
    filler = "诊断占位：此处缺少模型生成的有效内容，不能作为正式全书大纲使用。"
    while len(text) < min_chars:
        text += "\n" + filler
    return text

async def expand_character_biographies_section(
    generator: OutlineGenerator,
    *,
    user_input: str,
    base_outline: str,
    scale: dict[str, Any] | None,
) -> str:
    title = BOOK_SECTION_TITLES["二"]
    batches = [
        ("主角", "只写主角；必须包含初始处境、核心欲望、神裔血脉秘密、能力限制、伤痕或代价、五卷成长节点。", 900),
        ("核心盟友与导师", "只写两名核心盟友和一名导师；每人必须包含目标、秘密、能力、与主角关系变化、首次登场作用。", 900),
        ("亲属与旧识", "只写两名亲属或旧识；每人必须包含情感债、利益筹码、与主角关系变化、关键相遇事件和结局归宿。", 700),
        ("立场转向者与竞争者", "只写一名立场转向者和一名竞争者；每人必须包含公开立场、隐藏筹码、转向或竞争的触发事件、代价和最终关系状态。", 700),
        ("阶段敌人", "只写两名阶段敌人；每人必须包含公开目标、隐藏筹码、与主角冲突来源、代表事件和失败方式。", 800),
        ("终局对手", "只写一名终局对手；必须包含公开目标、真实目的、与主角最终冲突的资源筹码、失败代价和结局状态。", 650),
        ("隐藏推动者", "只写一名隐藏推动者；必须包含表面身份、操控局势的资源、制造误判的事件、暴露节点和被制衡方式。", 650),
        ("血脉源头知情者", "只写一名掌握神裔源头信息的人物；必须包含掌握的信息、为何隐瞒、首次透露线索的位置、与主角交易的代价。", 650),
        ("关键配角与伏笔人物", "只写推动世界观、势力交易、伏笔回收的关键配角；每人必须有剧情功能、牵连势力、信息差和出场位置。", 800),
    ]
    parts: list[str] = []
    if BOOK_LLM_DEGRADED:
        log("全书大纲段落 二 跳过模型分批调用：本轮模型已熔断，使用本地角色草案")
        for idx, (label, focus, batch_min_chars) in enumerate(batches, start=1):
            parts.append(f"（{idx}）{label}\n{fallback_character_batch(label, focus, batch_min_chars)}")
        stop_if_degraded("book_outline_section_二")
        return title + "\n" + "\n\n".join(parts)
    for idx, (label, focus, batch_min_chars) in enumerate(batches, start=1):
        cached_body = get_book_batch_checkpoint("二", label, batch_min_chars)
        if cached_body:
            parts.append(f"（{idx}）{label}\n{cached_body}")
            log(f"全书大纲段落 二 批次 {label} 复用断点，长度 {len(cached_body)} 字")
            continue
        batch_attempts = 2
        text = await expand_book_section(
            generator,
            user_input=user_input,
            base_outline=base_outline,
            section_num="二",
            min_chars=batch_min_chars,
            scale=scale,
            focus=focus,
            attempts=batch_attempts,
        )
        body_lines = text.splitlines()[1:] if text.splitlines() and text.splitlines()[0].strip() == title else text.splitlines()
        body = "\n".join(body_lines).strip()
        if not body:
            log(f"全书大纲段落 二 批次 {label} 连续空响应，使用本地结构化保底草案")
            body = fallback_character_batch(label, focus, batch_min_chars)
            stop_if_degraded(f"book_outline_section_二_{label}")
        elif len(body) < batch_min_chars:
            raise RuntimeError(
                f"book_outline_section_二_{label} stopped: 模型返回长度 {len(body)} 低于最低 {batch_min_chars}，"
                "未追加本地补写，避免把占位内容混入正式全书大纲。"
            )
        save_book_batch_checkpoint("二", label, body)
        log(f"全书大纲段落 二 批次 {label} 已保存断点，长度 {len(body)} 字")
        parts.append(f"（{idx}）{label}\n{body}")
    combined = title + "\n" + "\n\n".join(part for part in parts if part.strip())
    errors = section_validation_errors(combined, "二", BOOK_SECTION_MIN_CHARS["二"])
    if errors:
        log(f"全书大纲段落 二 分批组装后仍未完全达标：{'；'.join(errors)}")
    return combined


async def expand_relationship_network_section(
    generator: OutlineGenerator,
    *,
    user_input: str,
    base_outline: str,
    scale: dict[str, Any] | None,
) -> str:
    title = BOOK_SECTION_TITLES["四"]
    batches = [
        (
            "救援者、导师与暗面学院",
            "只写主角与女性高阶血脉救援者、关键导师、暗面学院之间的关系变化；必须包含信任建立、规则束缚、交换代价、第一次冲突和阶段性归属变化。",
            700,
        ),
        (
            "盟友、竞争者与立场转向者",
            "只写主角与核心盟友、同龄竞争者、立场转向者之间的关系；必须包含合作目标、利益冲突、误判节点、关系破裂或修复、共同承担的代价。",
            700,
        ),
        (
            "父母失踪线与旧识亲属",
            "只写主角与父母失踪线、老小区旧识、亲属或知情者之间的关系；必须包含情感债、隐瞒信息、线索交换、真相递进和最终归宿。",
            650,
        ),
        (
            "阶段敌人与旧神势力",
            "只写主角与阶段敌人、终局对手、旧神血裔和抹除旧神真相相关势力之间的关系；必须包含冲突来源、误导、临时交易、反转节点和终局清算。",
            750,
        ),
    ]
    parts: list[str] = []
    for idx, (label, focus, batch_min_chars) in enumerate(batches, start=1):
        cached_body = get_book_batch_checkpoint("四", label, batch_min_chars)
        if cached_body:
            parts.append(f"（{idx}）{label}\n{cached_body}")
            log(f"全书大纲段落 四 批次 {label} 复用断点，长度 {len(cached_body)} 字")
            continue
        text = await expand_book_section(
            generator,
            user_input=user_input,
            base_outline=base_outline,
            section_num="四",
            min_chars=batch_min_chars,
            scale=scale,
            focus=focus,
            attempts=2,
        )
        body_lines = text.splitlines()[1:] if text.splitlines() and text.splitlines()[0].strip() == title else text.splitlines()
        body = "\n".join(body_lines).strip()
        if len(body) < batch_min_chars:
            raise RuntimeError(
                f"book_outline_section_四_{label} stopped: 模型返回长度 {len(body)} 低于最低 {batch_min_chars}，"
                "未追加本地补写，避免把占位内容混入正式关系网。"
            )
        save_book_batch_checkpoint("四", label, body)
        log(f"全书大纲段落 四 批次 {label} 已保存断点，长度 {len(body)} 字")
        parts.append(f"（{idx}）{label}\n{body}")
    combined = title + "\n" + "\n\n".join(part for part in parts if part.strip())
    errors = section_validation_errors(combined, "四", BOOK_SECTION_MIN_CHARS["四"])
    if errors:
        raise RuntimeError("Book outline section 四 split generation failed: " + "; ".join(errors))
    return combined


async def expand_worldbuilding_section(
    generator: OutlineGenerator,
    *,
    user_input: str,
    base_outline: str,
    scale: dict[str, Any] | None,
) -> str:
    title = BOOK_SECTION_TITLES["六"]
    batches = [
        ("血脉规则与神裔来源", "只写神裔血脉的来源、等级、觉醒条件、能力边界和误用代价；必须锚定旧神血裔与被抹除旧神真相。", 850),
        ("暗面学院与信息验证", "只写暗面学院的招生、课程、任务、档案验证、保密制度和惩罚方式；必须说明主角如何用学院资源追查父母失踪。", 850),
        ("老小区、城市暗面与地理层级", "只写老小区异常、城市暗面入口、血脉世界层级、危险区域和通行代价；必须让地点服务任务推进。", 800),
        ("旧神历史断层", "只写旧神被抹除的历史断层、幸存证据、伪造叙事、知情者风险和主角逐卷接近真相的方式。", 850),
        ("势力秩序与资源经济", "只写学院、家族、旧神血裔、监管者和地下组织如何争夺资源；必须写清资源交换、债务、身份权限和背叛成本。", 850),
        ("禁忌规则与代价边界", "只写血脉使用禁忌、记忆污染、身份暴露、救人代价、契约反噬和旧神信息验证失败的后果。", 850),
        ("日常生活与任务机制", "只写主角在现实城市、学院训练、暗面任务和人际关系中的日常机制；必须体现目标、代价、关系变化和任务推进。", 850),
        ("战斗方式与灾变升级", "只写战斗体系、侦查方式、血脉克制、团队配合、失败代价和五卷灾变升级路径。", 850),
    ]
    parts: list[str] = []
    for idx, (label, focus, batch_min_chars) in enumerate(batches, start=1):
        cached_body = get_book_batch_checkpoint("六", label, batch_min_chars)
        if cached_body:
            parts.append(f"（{idx}）{label}\n{cached_body}")
            log(f"全书大纲段落 六 批次 {label} 复用断点，长度 {len(cached_body)} 字")
            continue
        text = await expand_book_section(
            generator,
            user_input=user_input,
            base_outline=base_outline,
            section_num="六",
            min_chars=batch_min_chars,
            scale=scale,
            focus=focus,
            attempts=2,
        )
        body_lines = text.splitlines()[1:] if text.splitlines() and text.splitlines()[0].strip() == title else text.splitlines()
        body = "\n".join(body_lines).strip()
        if len(body) < batch_min_chars:
            raise RuntimeError(
                f"book_outline_section_六_{label} stopped: 模型返回长度 {len(body)} 低于最低 {batch_min_chars}，"
                "未追加本地补写，避免把占位内容混入正式世界观。"
            )
        save_book_batch_checkpoint("六", label, body)
        log(f"全书大纲段落 六 批次 {label} 已保存断点，长度 {len(body)} 字")
        parts.append(f"（{idx}）{label}\n{body}")
    combined = title + "\n" + "\n\n".join(part for part in parts if part.strip())
    errors = section_validation_errors(combined, "六", BOOK_SECTION_MIN_CHARS["六"])
    if errors:
        raise RuntimeError("Book outline section 六 split generation failed: " + "; ".join(errors))
    return combined


async def expand_volume_planning_section(
    generator: OutlineGenerator,
    *,
    user_input: str,
    base_outline: str,
    scale: dict[str, Any] | None,
) -> str:
    title = BOOK_SECTION_TITLES["七"]
    plan = await generate_volume_plan_json(
        generator,
        user_input=user_input,
        skeleton_text=base_outline,
        scale=scale,
    )
    expected = int(scale.get("n_volumes") or 5) if scale else 5
    total_chapters = int(scale.get("n_chapters") or 0) if scale else 0
    if len(plan) != expected:
        raise RuntimeError(f"Book outline section 七 plan count mismatch: {len(plan)}/{expected}")
    if total_chapters:
        total = sum(int(item.get("est_chapters") or 0) for item in plan)
        if total != total_chapters:
            raise RuntimeError(f"Book outline section 七 chapter total mismatch: {total}/{total_chapters}")

    parts: list[str] = []
    overview_min = 700
    overview_body = get_book_batch_checkpoint("七", "全书分卷总推进", overview_min)
    if overview_body:
        log(f"全书大纲段落 七 批次 全书分卷总推进 复用断点，长度 {len(overview_body)} 字")
    else:
        overview = await expand_book_section(
            generator,
            user_input=user_input,
            base_outline=base_outline,
            section_num="七",
            min_chars=overview_min,
            scale=scale,
            focus="只写五卷整体推进逻辑；必须说明主角从老小区异常进入暗面学院、追查父母失踪、接近旧神血裔和被抹除旧神真相的总路径。",
            attempts=2,
        )
        overview_body = "\n".join(overview.splitlines()[1:]).strip() if overview.splitlines() and overview.splitlines()[0].strip() == title else overview.strip()
    if len(overview_body) < overview_min:
        raise RuntimeError(f"Book outline section 七 overview too short: {len(overview_body)}<{overview_min}")
    save_book_batch_checkpoint("七", "全书分卷总推进", overview_body)
    parts.append("（0）全书分卷总推进\n" + overview_body)

    per_volume_min = 850
    for item in plan:
        idx = int(item.get("idx") or len(parts))
        volume_title = as_text(item.get("title")) or f"第{idx}卷"
        batch_label = f"第{idx}卷:{volume_title}"
        cached_body = get_book_batch_checkpoint("七", batch_label, per_volume_min)
        if cached_body:
            parts.append(f"（{idx}）{volume_title}\n{cached_body}")
            log(f"全书大纲段落 七 批次 {batch_label} 复用断点，长度 {len(cached_body)} 字")
            continue
        focus = (
            f"只写第 {idx} 卷《{volume_title}》；本卷约 {item.get('est_chapters')} 章。"
            f"主题：{item.get('theme') or ''}。核心冲突：{item.get('core_conflict') or ''}。"
            "必须包含开局状态、中段转折、结尾爆点、主角成长、阶段性失败/胜利、父母失踪线索推进和伏笔流转。"
        )
        text = await expand_book_section(
            generator,
            user_input=user_input,
            base_outline=base_outline,
            section_num="七",
            min_chars=per_volume_min,
            scale=scale,
            focus=focus,
            attempts=2,
        )
        body = "\n".join(text.splitlines()[1:]).strip() if text.splitlines() and text.splitlines()[0].strip() == title else text.strip()
        if len(body) < per_volume_min:
            raise RuntimeError(f"Book outline section 七 volume {idx} too short: {len(body)}<{per_volume_min}")
        save_book_batch_checkpoint("七", batch_label, body)
        log(f"全书大纲段落 七 批次 {batch_label} 已保存断点，长度 {len(body)} 字")
        parts.append(f"（{idx}）{volume_title}\n{body}")

    plan_json = json.dumps(plan, ensure_ascii=False, indent=2)
    combined = title + "\n" + "\n\n".join(parts) + f"\n\n<volume-plan>\n{plan_json}\n</volume-plan>"
    errors = section_validation_errors(combined, "七", BOOK_SECTION_MIN_CHARS["七"])
    if errors:
        raise RuntimeError("Book outline section 七 split generation failed: " + "; ".join(errors))
    return combined


def get_volume_title(outline: dict[str, Any], idx: int, plan_item: dict[str, Any] | None) -> str:
    for key in ("title", "volume_title", "name"):
        value = as_text(outline.get(key))
        if value:
            return value
    if plan_item:
        value = as_text(plan_item.get("title"))
        if value:
            return value
    return f"第{idx}卷"


def get_volume_summary(outline: dict[str, Any], plan_item: dict[str, Any] | None) -> str:
    parts: list[str] = []
    for key in ("summary", "core_conflict", "emotional_arc", "transition_to_next"):
        value = as_text(outline.get(key))
        if value:
            parts.append(value)
    if not parts and plan_item:
        for key in ("theme", "core_conflict"):
            value = as_text(plan_item.get(key))
            if value:
                parts.append(value)
    return "\n\n".join(parts)


def get_chapter_title(item: dict[str, Any], idx: int) -> str:
    for key in ("title", "chapter_title", "name"):
        value = as_text(item.get(key), 480)
        if value:
            return value
    return f"第{idx}章"


def get_chapter_summary(item: dict[str, Any]) -> str:
    for key in ("summary", "main_progress", "key_scene"):
        value = as_text(item.get(key))
        if value:
            return value
    return json.dumps(item, ensure_ascii=False)


def fallback_volume_outline(idx: int, plan_item: dict[str, Any], target_chapters: int) -> dict[str, Any]:
    """Return a diagnostic-only volume outline when generation degrades."""
    mark_local_fallback(f"volume_{idx}_local_fallback")
    title = as_text(plan_item.get("title")) or f"第{idx}卷大纲未生成"
    result = {
        "volume_idx": idx,
        "title": title,
        "summary": "模型端点没有返回可用分卷大纲；本地兜底不会编造卷剧情或章节梗概。",
        "core_conflict": as_text(plan_item.get("core_conflict")),
        "chapter_count": 0,
        "chapter_summaries": [],
        "_quality_status": "degraded_structural_draft",
        "_quality_notes": (
            "模型端点连续空响应或熔断后生成的诊断占位；只能用于排查，"
            "不能视为《神裔》的正式分卷大纲，也不能用于生成正文。"
        ),
    }
    result["raw_text"] = json.dumps(result, ensure_ascii=False, indent=2)
    return result

async def load_project_snapshot() -> tuple[Project, int, int]:
    async with async_session_factory() as db:
        project = await db.get(Project, PROJECT_ID)
        if not project:
            raise RuntimeError(f"Project not found: {PROJECT_ID}")

        volume_count = await db.scalar(
            select(func.count(Volume.id)).where(Volume.project_id == PROJECT_ID)
        )
        body_count = await db.scalar(
            select(func.count(Chapter.id))
            .join(Volume, Chapter.volume_id == Volume.id)
            .where(Volume.project_id == PROJECT_ID, Chapter.content_text != "")
        )
        return project, int(volume_count or 0), int(body_count or 0)


async def generate_book_once(project: Project, scale: dict[str, Any] | None, attempt: int) -> dict[str, Any]:
    scale_note = ""
    if scale:
        scale_note = (
            f"目标字数：{scale.get('target_word_count')}；"
            f"卷数：{scale.get('n_volumes')}；"
            f"章节数：{scale.get('n_chapters')}；"
            f"每卷约：{scale.get('chapters_per_volume')}章"
        )
    user_input = (
        f"书名：{project.title}\n"
        f"类型：{project.genre or '未指定'}\n"
        f"创意/前提：{project.premise or ''}\n"
        f"规模：{scale_note}"
        f"{SHENYI_CANONICAL_FACTS_PROMPT}"
    )
    generator = OutlineGenerator(project_id=str(project.id))
    generator.router = await get_model_router_async()
    checkpoint_key = book_checkpoint_key(project, scale)
    checkpoint = load_book_checkpoint(project, scale) or {
        "checkpoint_key": checkpoint_key,
        "project_id": str(project.id),
        "title": project.title,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "skeleton_text": "",
        "characters_text": "",
        "world_text": "",
        "sections": {},
    }
    checkpoint.setdefault("checkpoint_key", checkpoint_key)
    checkpoint.setdefault("sections", {})
    global BOOK_CHECKPOINT_STATE
    BOOK_CHECKPOINT_STATE = checkpoint

    log(f"开始生成全书大纲（三阶段：骨架、角色、世界观），第 {attempt} 次")
    skeleton_system = (
        "你是中文小说大纲助手。只输出正文，不要解释。"
    )
    skeleton_text = as_text(checkpoint.get("skeleton_text"))
    if skeleton_text:
        skeleton_errors = shenyi_text_gate_errors(skeleton_text)
        if skeleton_errors:
            log("全书大纲阶段 A 断点含设定漂移，已忽略：" + "；".join(skeleton_errors))
            skeleton_text = ""
    if len(skeleton_text) >= 120:
        log(f"全书大纲阶段 A 复用断点内容，长度 {len(skeleton_text)} 字")
    else:
        skeleton_text = await generate_text_with_retries(
            generator,
            stage_name="阶段 A",
            min_chars=120,
            messages=[
                {"role": "system", "content": skeleton_system},
                {
                    "role": "user",
                    "content": (
                        f"项目信息：\n{user_input}\n\n"
                        "请生成3段故事种子，每段60-100字，必须锚定上述创意/前提，"
                        "写主角目标、代价、关系变化、父母失踪线索、旧神血裔真相推进。"
                    ),
                },
            ],
            attempts=2,
        )
    if len(skeleton_text) < 120:
        log(f"全书大纲阶段 A 连续短响应/空响应，使用本地结构化骨架保底，原长度 {len(skeleton_text)} 字")
        mark_local_fallback("book_skeleton_local_fallback")
        skeleton_text = generator._fallback_book_skeleton(user_input, scale=scale)
    stop_if_degraded("book_outline_stage_a")
    assert_shenyi_text_gate(skeleton_text, "book_outline_stage_a")
    checkpoint["skeleton_text"] = skeleton_text
    checkpoint["updated_at"] = datetime.now(timezone.utc).isoformat()
    save_book_checkpoint(checkpoint)
    log(f"全书大纲阶段 A 完成，长度 {len(skeleton_text)} 字")

    shared_context = f"创意：\n{user_input}\n\n已生成的骨架：\n{skeleton_text}\n"
    volume_plan = generator._extract_volume_plan(skeleton_text) or []
    if scale and len(volume_plan) != int(scale.get("n_volumes") or 0):
        volume_plan = fallback_volume_plan(scale)
        log(f"骨架未提供可用卷计划，先使用确定性兜底卷计划 {len(volume_plan)} 卷，后续第七段扩写可覆盖")
        skeleton_text += "\n\n<volume-plan>\n" + json.dumps(volume_plan, ensure_ascii=False, indent=2) + "\n</volume-plan>"
        assert_shenyi_text_gate(skeleton_text, "book_outline_stage_a_volume_plan", require_volume_plan_tags=True)
        checkpoint["skeleton_text"] = skeleton_text
        checkpoint["updated_at"] = datetime.now(timezone.utc).isoformat()
        save_book_checkpoint(checkpoint)

    if BOOK_ENABLE_PREGEN_STAGES:
        characters_text = await generate_character_stage_sections(
            generator,
            user_input=user_input,
            skeleton_text=skeleton_text,
            scale=scale,
        )
        world_text = await generate_text_with_retries(
            generator,
            stage_name="阶段 C",
            min_chars=120,
            messages=[
                {"role": "system", "content": "你是中文小说世界观助手。只输出正文，不要解释。"},
                {
                    "role": "user",
                    "content": (
                        f"项目信息：\n{user_input}\n\n"
                        "请生成300字以内世界观种子，只写血脉规则、暗面学院的信息验证方式、"
                        "旧神血裔来源、被抹除旧神的禁忌边界，以及主角追查父母失踪需要付出的代价。"
                    ),
                },
            ],
            max_tokens=BOOK_STAGE_MAX_TOKENS,
            timeout_seconds=BOOK_STAGE_CALL_TIMEOUT_SECONDS,
        )
        if len(world_text) < 120:
            log(f"全书大纲阶段 C 连续短响应/空响应，使用本地结构化世界观保底，原长度 {len(world_text)} 字")
            world_text = deterministic_section_supplement("六", BOOK_STAGE_MIN_CHARS["world"], scale)
        stop_if_degraded("book_outline_seed_stages")
        log(f"全书大纲阶段 B 完成，长度 {len(characters_text)} 字")
        log(f"全书大纲阶段 C 完成，长度 {len(world_text)} 字")
    else:
        characters_text = ""
        world_text = ""
        log("全书大纲阶段 B/C 已跳过：当前端点吞吐较低，直接进入正式九段生成，减少重复调用")

    base_combined = generator._reassemble_sections(skeleton_text, characters_text, world_text)
    reusable_stage_sections = extract_sections(generator, characters_text)
    log(f"开始逐段扩写全书大纲，目标最低 {BOOK_MIN_CHARS} 字")
    expanded_sections: dict[str, str] = {}
    checkpoint_sections = checkpoint.get("sections") if isinstance(checkpoint.get("sections"), dict) else {}
    for num, minimum in BOOK_SECTION_MIN_CHARS.items():
        cached = as_text(checkpoint_sections.get(num))
        if cached and not section_validation_errors(cached, num, minimum):
            expanded_sections[num] = cached
            log(f"全书大纲段落 {num} 复用断点合格内容，长度 {len(cached)} 字")
            continue
        reusable = reusable_stage_sections.get(num, "") if num in {"二", "四", "五"} else ""
        if reusable and not section_validation_errors(reusable, num, minimum):
            expanded_sections[num] = reusable
            log(f"全书大纲段落 {num} 复用预生成合格内容，长度 {len(reusable)} 字")
        elif num == "二":
            expanded_sections[num] = await expand_character_biographies_section(
                generator,
                user_input=user_input,
                base_outline=base_combined,
                scale=scale,
            )
        elif num == "四":
            expanded_sections[num] = await expand_relationship_network_section(
                generator,
                user_input=user_input,
                base_outline=base_combined,
                scale=scale,
            )
        elif num == "六":
            expanded_sections[num] = await expand_worldbuilding_section(
                generator,
                user_input=user_input,
                base_outline=base_combined,
                scale=scale,
            )
        elif num == "七":
            expanded_sections[num] = await expand_volume_planning_section(
                generator,
                user_input=user_input,
                base_outline=base_combined,
                scale=scale,
            )
        else:
            expanded_sections[num] = await expand_book_section(
                generator,
                user_input=user_input,
                base_outline=base_combined,
                section_num=num,
                min_chars=minimum,
                scale=scale,
            )
        errors = section_validation_errors(expanded_sections[num], num, minimum)
        if errors:
            raise RuntimeError(
                f"Book outline section {num} quality gate failed before assembly: " + "; ".join(errors)
            )
        stop_if_degraded(f"book_outline_section_{num}")
        checkpoint_sections[num] = expanded_sections[num]
        checkpoint["sections"] = checkpoint_sections
        checkpoint["updated_at"] = datetime.now(timezone.utc).isoformat()
        save_book_checkpoint(checkpoint)
        log(f"全书大纲段落 {num} 已保存断点，长度 {len(expanded_sections[num])} 字")

    combined = "\n\n".join(expanded_sections[num].strip() for num in BOOK_SECTION_TITLES)
    assert_shenyi_text_gate(
        combined,
        "book_outline_assembled_with_volume_plan",
        require_book_terms=True,
        require_anchor_terms=True,
        require_volume_plan_tags=True,
    )
    buckets = dict(expanded_sections)
    extracted_volume_plan = generator._extract_volume_plan(combined) or []
    if extracted_volume_plan:
        volume_plan = extracted_volume_plan
    else:
        raise RuntimeError(
            "Book outline quality gate failed: 第七段没有生成可抽取的 <volume-plan> JSON，"
            "未使用本地卷计划占位，避免分卷规划被占位结构替代。"
        )
    combined = generator._strip_volume_plan_tags(combined)
    structured = generator._build_book_structured(buckets)
    done = {
        "raw_text": combined,
        "volume_plan": volume_plan,
        "structured": structured,
        "_staged": True,
        "_stage_lengths": {
            "skeleton": len(skeleton_text),
            "characters": len(characters_text),
            "world": len(world_text),
        },
        "_stages": {
            "skeleton": len(skeleton_text) >= 120,
            "characters": True if not BOOK_ENABLE_PREGEN_STAGES else len(characters_text) >= BOOK_STAGE_MIN_CHARS["characters"],
            "world": True if not BOOK_ENABLE_PREGEN_STAGES else len(world_text) >= 120,
        },
    }

    if not isinstance(done, dict) or not as_text(done.get("raw_text") or done.get("full_outline")):
        raise RuntimeError("Book outline generation returned empty content")

    log("全书大纲返回字段：" + ",".join(sorted(str(k) for k in done.keys())))
    stages = done.get("_stages") if isinstance(done.get("_stages"), dict) else {}
    stage_lengths = done.get("_stage_lengths") if isinstance(done.get("_stage_lengths"), dict) else {}
    log("全书大纲阶段长度：" + json.dumps(stage_lengths, ensure_ascii=False))
    missing = [name for name in ("skeleton", "characters", "world") if not stages.get(name)]
    if missing:
        raise RuntimeError(f"Book outline generation missing stages: {missing}")

    full_outline = as_text(done.get("raw_text") or done.get("full_outline"))
    structured = done.get("structured") if isinstance(done.get("structured"), dict) else {}
    volume_plan = done.get("volume_plan") if isinstance(done.get("volume_plan"), list) else []
    expected_volumes = int(scale.get("n_volumes") or 0) if scale else 0
    if expected_volumes and len(volume_plan) != expected_volumes:
        raise RuntimeError(f"Book outline volume_plan count mismatch: {len(volume_plan)}/{expected_volumes}")
    validation_payload = {
        "raw_text": full_outline,
        "volume_plan": volume_plan,
    }
    validation_payload = validate_shenyi_book_outline_payload(
        generator,
        validation_payload,
        scale,
        "Book outline",
    )
    log("全书大纲质量门禁通过：" + json.dumps({
        "length": len(full_outline),
        "sections": {num: len(extract_sections(generator, full_outline).get(num, "")) for num in BOOK_SECTION_TITLES},
        "volume_plan_count": len(volume_plan),
    }, ensure_ascii=False))
    log(f"全书大纲完成，正文长度 {len(full_outline)} 字，卷计划 {len(volume_plan)} 卷")
    return {
        "raw_text": full_outline,
        "volume_plan": volume_plan,
        "entity_registry": validation_payload["entity_registry"],
        "structured": structured,
        "_staged": True,
        "_stages": done.get("_stages") or {},
        **structured,
    }


async def generate_book(project: Project, scale: dict[str, Any] | None) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(1, BOOK_FULL_MAX_ATTEMPTS + 1):
        try:
            return await generate_book_once(project, scale, attempt)
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            log(f"全书大纲第 {attempt} 次失败：{exc}")
    raise RuntimeError(f"Book outline generation stopped after root-cause capture: {last_error}")


def normalize_plan(book_outline: dict[str, Any], scale: dict[str, Any] | None) -> list[dict[str, Any]]:
    raw_plan = book_outline.get("volume_plan")
    plan = [p for p in raw_plan if isinstance(p, dict)] if isinstance(raw_plan, list) else []
    expected = int(scale.get("n_volumes")) if scale else len(plan)
    target_chapters = int(scale.get("n_chapters")) if scale else 0
    chapters_per_volume = int(scale.get("chapters_per_volume")) if scale else 0

    if expected <= 0:
        expected = len(plan) or 1
    if len(plan) < expected:
        mark_local_fallback("normalized_volume_plan_missing_items")
        for idx in range(len(plan) + 1, expected + 1):
            title = SHENYI_VOLUME_PLAN_TITLES[idx - 1] if 1 <= idx <= len(SHENYI_VOLUME_PLAN_TITLES) else f"第{idx}卷"
            plan.append({"idx": idx, "title": title, "theme": title.split("：", 1)[-1], "core_conflict": ""})
    if len(plan) > expected:
        mark_local_fallback("normalized_volume_plan_truncated")
        plan = plan[:expected]

    for pos, item in enumerate(plan, start=1):
        item["idx"] = pos
        if target_chapters:
            remaining = target_chapters - chapters_per_volume * (expected - 1)
            item["est_chapters"] = remaining if pos == expected else chapters_per_volume
        elif not item.get("est_chapters"):
            item["est_chapters"] = chapters_per_volume or 10
    return plan


async def generate_volumes(book_outline: dict[str, Any], plan: list[dict[str, Any]]) -> list[dict[str, Any]]:
    generator = OutlineGenerator(project_id=str(PROJECT_ID))
    generator.router = await get_model_router_async()
    generated: list[dict[str, Any]] = []
    for item in plan:
        idx = int(item.get("idx") or len(generated) + 1)
        target_chapters = int(item.get("est_chapters") or 0)
        notes = (
            f"{SHENYI_CANONICAL_FACTS_PROMPT}\n"
            f"实体注册表：{json.dumps(SHENYI_ENTITY_REGISTRY, ensure_ascii=False)}\n"
            f"请严格按照全书卷计划生成第 {idx} 卷。"
            f"本卷标题：{item.get('title') or f'第{idx}卷'}。"
            f"本卷主题：{item.get('theme') or ''}。"
            f"本卷核心冲突：{item.get('core_conflict') or ''}。"
            f"本卷必须输出 chapter_count={target_chapters}，chapter_summaries 必须正好 {target_chapters} 条。"
        )
        outline: dict[str, Any] | None = None
        chapter_count = 0
        last_error: str | None = None
        if BOOK_LLM_DEGRADED:
            outline = fallback_volume_outline(idx, item, target_chapters)
            validate_shenyi_volume_outline(outline, f"volume_{idx}_fallback")
            chapter_count = len(outline.get("chapter_summaries") or [])
            log(f"第 {idx} 卷跳过模型调用：本轮模型已熔断，使用本地结构化分卷提纲 {chapter_count} 条")
            generated.append({"idx": idx, "plan": item, "outline": outline})
            continue
        for attempt in range(1, 3):
            log(f"开始生成第 {idx}/{len(plan)} 卷：{item.get('title') or ''}，目标 {target_chapters} 章，第 {attempt} 次")
            candidate = await generator.generate_volume_outline(
                book_outline=book_outline,
                volume_idx=idx,
                user_notes=notes,
                stream=False,
                staged=True,
            )
            if not isinstance(candidate, dict) or candidate.get("_parse_error"):
                last_error = f"parse error: {candidate}"
                log(f"第 {idx} 卷第 {attempt} 次失败：{last_error}")
                continue
            chapters = candidate.get("chapter_summaries")
            chapter_count = len(chapters) if isinstance(chapters, list) else 0
            if target_chapters and chapter_count < target_chapters:
                last_error = f"章节数不足 {chapter_count}/{target_chapters}"
                log(f"第 {idx} 卷第 {attempt} 次失败：{last_error}")
                continue
            try:
                validate_shenyi_volume_outline(candidate, f"volume_{idx}_attempt_{attempt}")
            except RuntimeError as exc:
                last_error = str(exc)
                log(f"第 {idx} 卷第 {attempt} 次失败：{last_error}")
                continue
            outline = candidate
            break
        if outline is None:
            raise RuntimeError(f"Volume {idx} generation failed after retries: {last_error}")
        log(f"第 {idx} 卷完成：章节提纲 {chapter_count} 条，标题：{get_volume_title(outline, idx, item)}")
        generated.append({"idx": idx, "plan": item, "outline": outline})
    return generated


async def persist(project: Project, book_outline: dict[str, Any], volumes: list[dict[str, Any]]) -> dict[str, int]:
    generator = OutlineGenerator(project_id=str(PROJECT_ID))
    book_outline = validate_shenyi_book_outline_payload(
        generator,
        book_outline,
        compute_scale(project.target_word_count),
        "Book outline persist",
    )
    for vol in volumes:
        outline = vol.get("outline") if isinstance(vol, dict) else None
        if isinstance(outline, dict):
            validate_shenyi_volume_outline(outline, f"volume_{vol.get('idx')}_persist")
    async with async_session_factory() as db:
        await db.execute(delete(Outline).where(Outline.project_id == PROJECT_ID, Outline.level.in_(["book", "volume"])))

        book_row = Outline(
            id=uuid.uuid4(),
            project_id=PROJECT_ID,
            level="book",
            content_json=book_outline,
            version=1,
            is_confirmed=1,
        )
        db.add(book_row)
        await db.flush()

        existing_volumes = {
            v.volume_idx: v
            for v in (
                await db.execute(
                    select(Volume).where(Volume.project_id == PROJECT_ID).order_by(Volume.volume_idx)
                )
            ).scalars().all()
        }

        updated_chapters = 0
        created_chapters = 0
        volume_rows = 0
        target_volume_words = max(1, math.floor((project.target_word_count or 0) / max(1, len(volumes))))

        for vol in volumes:
            idx = int(vol["idx"])
            plan_item = vol.get("plan") if isinstance(vol.get("plan"), dict) else {}
            outline = vol["outline"]
            title = get_volume_title(outline, idx, plan_item)
            summary = get_volume_summary(outline, plan_item)
            volume = existing_volumes.get(idx)
            if volume is None:
                volume = Volume(
                    id=uuid.uuid4(),
                    project_id=PROJECT_ID,
                    title=title,
                    volume_idx=idx,
                    summary=summary,
                    target_word_count=target_volume_words,
                )
                db.add(volume)
                await db.flush()
            else:
                volume.title = title
                volume.summary = summary
                volume.target_word_count = target_volume_words
            volume_rows += 1

            db.add(
                Outline(
                    id=uuid.uuid4(),
                    project_id=PROJECT_ID,
                    level="volume",
                    parent_id=book_row.id,
                    content_json=outline,
                    version=1,
                    is_confirmed=1,
                )
            )

            existing_chapters = {
                c.chapter_idx: c
                for c in (
                    await db.execute(
                        select(Chapter).where(Chapter.volume_id == volume.id).order_by(Chapter.chapter_idx)
                    )
                ).scalars().all()
            }
            summaries = outline.get("chapter_summaries")
            if not isinstance(summaries, list):
                summaries = []
            for pos, item in enumerate(summaries, start=1):
                if not isinstance(item, dict):
                    continue
                chapter_idx = int(item.get("chapter_idx") or pos)
                chapter = existing_chapters.get(chapter_idx)
                if chapter is None:
                    chapter = Chapter(
                        id=uuid.uuid4(),
                        volume_id=volume.id,
                        chapter_idx=chapter_idx,
                        title=get_chapter_title(item, chapter_idx),
                        outline_json=item,
                        summary=get_chapter_summary(item),
                        content_text="",
                        word_count=0,
                        status="draft",
                    )
                    db.add(chapter)
                    created_chapters += 1
                else:
                    chapter.title = get_chapter_title(item, chapter_idx)
                    chapter.outline_json = item
                    chapter.summary = get_chapter_summary(item)
                    updated_chapters += 1

        await db.commit()
        return {
            "book_outlines": 1,
            "volume_outlines": len(volumes),
            "volume_rows_touched": volume_rows,
            "chapters_updated": updated_chapters,
            "chapters_created": created_chapters,
        }


async def persist_book_only(book_outline: dict[str, Any]) -> dict[str, int]:
    generator = OutlineGenerator(project_id=str(PROJECT_ID))
    async with async_session_factory() as db:
        project = await db.get(Project, PROJECT_ID)
        if not project:
            raise RuntimeError(f"Project not found: {PROJECT_ID}")
        book_outline = validate_shenyi_book_outline_payload(
            generator,
            book_outline,
            compute_scale(project.target_word_count),
            "Book outline persist_book_only",
        )
        await db.execute(delete(Outline).where(Outline.project_id == PROJECT_ID, Outline.level == "book"))
        db.add(
            Outline(
                id=uuid.uuid4(),
                project_id=PROJECT_ID,
                level="book",
                content_json=book_outline,
                version=1,
                is_confirmed=1,
            )
        )
        await db.commit()
        return {"book_outlines": 1, "volume_outlines": 0, "chapters_updated": 0, "chapters_created": 0}


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--book-only", action="store_true", help="Only regenerate and persist the book outline")
    parser.add_argument(
        "--allow-degraded-persist",
        action="store_true",
        help="Persist local diagnostic scaffolds when the model endpoint degrades; off by default.",
    )
    parser.add_argument(
        "--reset-book-checkpoint",
        action="store_true",
        help="Clear the temporary book-outline checkpoint before generation.",
    )
    parser.add_argument(
        "--no-book-checkpoint",
        action="store_true",
        help="Disable temporary book-outline checkpoint reuse for this run.",
    )
    args = parser.parse_args()
    global ALLOW_DEGRADED_PERSIST, BOOK_USE_CHECKPOINT
    ALLOW_DEGRADED_PERSIST = bool(args.allow_degraded_persist)
    BOOK_USE_CHECKPOINT = not bool(args.no_book_checkpoint)
    if args.reset_book_checkpoint:
        clear_book_checkpoint()
        log("已清理全书大纲临时断点")
    try:
        project, existing_volumes, existing_body_chapters = await load_project_snapshot()
        scale = compute_scale(project.target_word_count)
        log(
            "项目："
            f"{project.title}，目标字数 {project.target_word_count}，"
            f"现有 {existing_volumes} 卷，已有正文章节 {existing_body_chapters} 个"
        )
        log(f"后端规模规则：{json.dumps(scale, ensure_ascii=False)}")

        book_outline = await generate_book(project, scale)
        if BOOK_LLM_DEGRADED or BOOK_LOCAL_FALLBACK_USED:
            book_outline = dict(book_outline)
            book_outline["_quality_status"] = "degraded_structural_draft"
            book_outline["_quality_notes"] = (
                "模型端点不稳定或本地兜底参与后写入的结构草稿；结构字段和规模可用于定位流程问题，"
                "但不能作为已通过的高质量全书大纲。"
            )
            book_outline["_quality_reasons"] = list(BOOK_LOCAL_FALLBACK_REASONS)
            if not args.allow_degraded_persist:
                raise RuntimeError(
                    "本轮生成包含本地兜底或模型熔断，只得到降级结构草稿，已停止写库；"
                    "请先修复端点空响应/限流问题，或仅在排查时显式使用 --allow-degraded-persist。"
                )
        if args.book_only:
            stats = await persist_book_only(book_outline)
            log("已写回数据库：" + json.dumps(stats, ensure_ascii=False))
            return
        plan = normalize_plan(book_outline, scale)
        log("将按卷计划生成：" + "；".join(f"{p['idx']}.{p.get('title')}({p.get('est_chapters')}章)" for p in plan))
        volume_outlines = await generate_volumes(book_outline, plan)
        stats = await persist(project, book_outline, volume_outlines)
        log("已写回数据库：" + json.dumps(stats, ensure_ascii=False))
    except RuntimeError as exc:
        log("生成停止：" + str(exc))
        raise SystemExit(2) from None
    finally:
        await dispose_current_engine_async()


if __name__ == "__main__":
    asyncio.run(main())
