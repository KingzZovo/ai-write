"""逻辑与剧情核查角色（章内语义级自洽审查）。

读完整章正文 + 本章大纲 + 紧邻前章末尾（隔离 context，不喂全书记忆），
专查现有 checker 漏掉的章内缺陷：空间方向矛盾、画面重述、跨度突变、
动作因果断裂、道具状态连续性。产出结构化 issue 清单供定向改写。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 五个检测维度（与 spec 一一对应）。
LOGIC_DIMENSIONS: tuple[str, ...] = (
    "spatial_direction",     # 空间方向一致性
    "scene_redescription",   # 画面重述/草稿叠写残留
    "span_jump",             # 空间/时间跨度突变
    "action_causality",      # 动作因果链断裂
    "prop_state",            # 道具/状态连续性
)


@dataclass(frozen=True)
class LogicIssue:
    dimension: str
    severity: str  # high|medium|low
    quote: str
    problem: str
    fix_hint: str
    locatable: bool = True


@dataclass
class LogicCriticReport:
    available: bool          # False = 核查不可用（LLM/解析失败）→ 降级
    clean: bool              # True = 无 issue
    issues: list[LogicIssue] = field(default_factory=list)

    @property
    def high_issues(self) -> list[LogicIssue]:
        return [i for i in self.issues if i.severity == "high"]

    @property
    def locatable_issues(self) -> list[LogicIssue]:
        return [i for i in self.issues if i.locatable]

    @property
    def issue_count(self) -> int:
        return len(self.issues)
