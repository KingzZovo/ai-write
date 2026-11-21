"""Explicit complete arc plans for tests; never a production fallback."""


def arc_meta(target=4, title="修好以后"):
    return {
        "title": title, "chapter_count": target,
        "core_conflict": "宋岚修好借来的旧书，再和林禾商量归还的日子。",
        "emotional_arc": "从担心被责怪，到能够说明修补经过。",
        "volume_start_state": {"旧书": "在宋岚手中，封面尚未修补"},
        "volume_end_state": {"旧书": "本弧计划修好并交给林禾"},
        "transition_to_next": "本弧自然收束，后续按作者方向继续。",
        "foreshadow_progression": "保留全书已经约定的线索，本弧不新增强制悬念。",
    }


def arc_chapter(index):
    return {
        "chapter_idx": index, "title": f"约定第{index}步",
        "summary": f"宋岚与林禾处理第{index}次约定，说明当时可行的安排。",
        "key_events": [f"二人谈清第{index}次约定的具体安排。"],
        "start_state": {"进展": f"承接第{index-1}章的实际结果"},
        "end_state": {"进展": f"第{index}次安排得到明确回应"},
        "handoff_to_next": "按约定继续；若为末章则在本弧结果处收束。",
        "character_choices": "林禾听完修补经过后说明自己的顾虑，宋岚据此调整安排。",
        "narrative_focus": "详写双方如何回应具体顾虑，不重复介绍旧书来历。",
    }


def arc_outline(target=4, title="修好以后"):
    return {**arc_meta(target, title), "arc_plan_version": 1, "available": True,
            "chapter_summaries": [arc_chapter(i) for i in range(1, target + 1)]}
