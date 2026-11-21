# ai-write 代码审计基线 v1.7.2

> **记录时间**：2026-04-28
> **对应提交**：`31c0362` (v1.7.2) → 审计 commit `dc04c71` (包含本文与两份架构文档)
> **范围**：后端 Python (`backend/app/`) + 依赖扫描 + 前端 npm 依赖 + 单元测试覆盖。未覆盖负载测试与安全渗透。
> **工具**：ruff 0.15.12 / mypy 1.20.2 / pytest-cov 6.x / pip-audit 2.10.0 / npm audit
>
> **一句话结论**：产品逻辑能跑能交付，252 个单测全绿，但测试覆盖率只有 **34%**，静态分析门禄未出过现场，**在生产 hot-path `model_router.stream_by_route` 里发现一个真 bug（F821 NameError）是本次审计最重要的产出**。纲领建议：v1.7.3 先热修该 NameError；v1.8 带上 ruff 门禄 + P0 服务补单测。

---

## 1. 工具与运行环境

| 工具 | 版本 | 跳路径 |
|---|---|---|
| Python | 3.11.15 | `ai-write-backend-1` |
| ruff  | 0.15.12 | 容器内 pip 现装 |
| mypy  | 1.20.2 (compiled) | 容器内 pip 现装 |
| pytest-cov | 6.x | 容器内 pip 现装 |
| pip-audit | 2.10.0 | 容器内 pip 现装 |
| npm | 随镜像 | 宿主 `frontend/` |

原始输出起始位置：`/root/ai-write/.audit-v172/`（未入仓，仅供下次复现参考）。

## 2. ruff 静态检查

### 2.1 总览

```
Found 72 errors. (55 可自动修复)
```

| 规则 | 数量 | 含义 | 严重度 |
|---|---:|---|---|
| F401 | 50 | unused-import | 低 |
| F841 | 10 | unused-variable | 低（但有几处 dead-logic 徵兆）|
| E741 | 4 | 嵌名 `l` | 低 |
| F541 | 4 | 空 f-string | 低 |
| E401 | 1 | 多 import 在同一行 | 低 |
| E402 | 1 | 模块 import 不在顶部 | 低 |
| F811 | 1 | 没用的重复定义 | 低 |
| **F821** | **1** | **未定义名** | **🔥 高** |

### 2.2 🔥 P0 真 bug：`stream_by_route` 造成 NameError

**位置**：`app/services/model_router.py:765`。

```python
async def stream_by_route(self, route, messages: list[dict],
                          temperature: float | None = None,
                          max_tokens: int | None = None,
                          _log_meta: dict | None = None,
                          **kw) -> AsyncIterator[str]:
    """Stream using an explicit RouteSpec (v0.5 path)."""
    ep_key = str(route.endpoint_id)
    provider = self._get_provider(ep_key)
    model = route.model or self._endpoint_defaults.get(ep_key, "")
    eff_temp = temperature if temperature is not None else route.temperature
    eff_max = max_tokens if max_tokens is not None else route.max_tokens
    if _log_meta is None:
        async for chunk in provider.generate_stream(
            messages=messages, model=model,
            temperature=eff_temp, max_tokens=eff_max,
            task_type=task_type, **kw):   # ←← F821：本作用域未定义 task_type
            yield chunk
        return
    # ... 下面才出现 task_type = meta.pop("task_type", "by_route_stream")
```

**成因**：v1.7.1 Z1 推广 task_type 传递时，`generate_by_route` 那个分支走到了 `if _log_meta is None` 这一块仅被 `kw` 包裹过去、并未明示引用 `task_type`；但 `stream_by_route` 这个类似分支被丝到“要举 `task_type`”，刷了 `task_type=task_type`、但漏了在这个分支上方提前给 task_type 赋值。

**触发条件**：`stream_by_route` + `_log_meta is None`（即 caller 未传该字段）会抹捭 NameError。

**当前发生概率**：需调用点审计；SSE 生成路径大多会传 `_log_meta`，所以业务层可能未全面曝露，但一旦被呼到即炸。**不能拖**。

**修复预案**：在 `if _log_meta is None:` 之前加 `task_type = kw.pop("task_type", "by_route_stream")`，两个分支均用本地变量。同时补一条单测：如果不传 `_log_meta`，调用 stream 返回应提供 chunks，并且 `llm_call_total{task_type="by_route_stream"}` 上升 1。

**调度**：v1.7.3 hotfix，作为本轮审计后紧接着的第一项动作，不咨询多走一轮。

### 2.3 其他在意项

- `app/services/checkers/pacing_checker.py` 出现 5 处 F841/E741，是该检查器实际业务逻辑中装载、但未被使用的变量（`avg_len` `last_quarter_avg` `first_quarter_avg`）——这些是业务逻辑出错的徵兆，需 review checker 实现是否跳过了本应参与计算的变量。
- `app/services/checkers/consistency_checker.py:70` `rule_lower` 同类型啳弄。
- `app/services/checkers/reader_pull_checker.py:80` `middle` 同。
- `app/api/generate.py` 中 4 个未使用变量（`world_rules` `book_summary` `current_text` `previous_text`）：在主路径上，贴近 prompt 拼装环节。**需人工 review** 这些东西是不是本应进入 prompt 但被遗忘了。
- F541 空 f-string 是代码遗代，无质量影响。
- E741 全部可一次性 sed 重命名。

### 2.4 推荐接入门禄者

```toml
# pyproject.toml 占位（当前仓库未出现）
[tool.ruff]
line-length = 110
target-version = "py311"
extend-select = ["F", "E", "W"]
```

v1.8 全扫修复 + CI 门禄 “不能新增 F8** 类错误”。

## 3. mypy 静态类型检查

### 3.1 总览

```
Found 550 errors in 56 files (checked 157 source files)
```

### 3.2 Top 错误类别

| 类别 | 数量 | 含义 |
|---|---:|---|
| `[assignment]` | 234 | 赋值类型不匹配 |
| `[str]` | 230 | str 潜在 None / 不可调用 |
| `[arg-type]` | 197 | 函数参数类型不你 |
| `[int]` | 95 | int 推断不你 |
| `[float]` | 33 | 同 |
| `[attr-defined]` | 33 | 访问不存在属性 |
| `[datetime]` | 28 | datetime 推断 |
| `[var-annotated]` | 26 | 未标注 |
| `[union-attr]` | 26 | 联合类型访问属性 |
| `[call-arg]` | 11 | 调用参数个数/名不对 |

### 3.3 Top 出错文件

| 文件 | 错误 | 点评 |
|---|---:|---|
| `app/tasks/knowledge_tasks.py` | 90 | 未套 ORM 返回型/未套 dict 类型。为主赔备选。 |
| `app/services/model_router.py` | 28 | 包含 F821 那一条，其余是 provider 返回类型。修 F821 后可一起收 |
| `app/api/knowledge.py` | 28 | router pydantic 输入 |
| `app/api/generate.py` | 23 | 同上 |
| `app/tasks/evaluation_tasks.py` | 19 | 评估任务中 JSON dict 未标注 |
| `app/api/model_config.py` | 18 | router |
| `app/services/pipeline_service.py` | 17 | |
| `app/services/generation_runner.py` | 17 | |
| `app/tasks/cascade.py` | 16 | |
| `app/services/context_pack.py` | 16 | |
| `app/api/styles.py` | 16 | |
| `app/services/tool_registry.py` | 14 | |

### 3.4 评估

- 550 是 “项目仅启用 `--ignore-missing-imports` + 默认 strictness” 下的计数。中一大部分源于 SQLAlchemy 2.x async ORM 返回值未套 + Pydantic v1→v2 迁移里 30+ 个 JSON dict 未标注。取后三名能减到 ≤100。
- 多为类型推断质量，极低概率是运行时 bug。但 `[attr-defined]` 33 条是“访问不存在属性”——不判断为产生现场全面需 case-by-case，在下一轮审计里进一步取全量、分轻重。
- 当前**不接入 mypy 门禄**，先记账。v1.9 才轮到该项目。

## 4. pytest 覆盖率

### 4.1 总览

```
252 passed, 8 warnings in 10.53s
TOTAL  15222 statements / 9997 missed / 34% coverage
```

未使用 branch coverage（`--cov-branch`）；次轮可补。`tests/integration` 已隐去，只在外部看 API 纯包装随手跳起。

### 4.2 占运行顺序上贤的 “够高” 服务 （≥ 80%）

| 服务 | 覆盖 | 备注 |
|---|---:|---|
| `budget_allocator` | 97% | |
| `auto_revise` | 93% | C2 徽站 |
| `cascade_planner` | 93% | C4 徽站 |
| `scene_orchestrator` | 90% | C1 徽站 |
| `prompt_recommendations` | 92% | |
| `prompt_cache` | 84% | C3 徽站 |
| `entity_dispatch` | 84% | |
| `chapter_evaluator` | 75% | |
| `quality_scorer` | 72% | |

### 4.3 🔴 P0 补补补（0% 覆盖且在主路径上）

| 服务 | 覆盖 | 语句数 | 业务位置 |
|---|---:|---:|---|
| `outline_generator` | 13% | 221 | **全文/分卷/章节大纲生成主体** |
| `chapter_generator` | 42% | 31 | **章节生成主体（瘦）** |
| `style_abstractor` | 0% | 17 | **风格萄馕主体** |
| `feature_extractor` | 41% | 116 | **风格特征抽取主体** |
| `beat_extractor` | 0% | 24 | **beat sheet 抽取** |
| `settings_extractor` | 22% | 67 | 设定抽取 |
| `style_clustering` | 0% | 75 | |
| `style_runtime` | 0% | 30 | resolve_style_prompt / anti-AI |
| `text_pipeline` | 0% | 180 | |
| `text_rewriter` | 0% | 30 | |
| `memory` | 0% | 218 | 记忆上下文 |
| `strand_tracker` | 0% | 142 | |
| `book_source_engine` | 0% | 389 | 参考书爬取 |
| `reference_ingestor` | 0% | 241 | 参考书入库 |
| `agents/style_agent` | 0% | 78 | multi-agent |
| `agents/plot_agent` | 0% | 32 | multi-agent |
| `pacing_checker` | 0% | 152 | |
| `reader_pull_checker` | 0% | 158 | |
| `continuity_checker` | 0% | 121 | |
| `tasks/knowledge_tasks` | 6% | 564 | **任务总输入** |

### 4.4 上调路径

- v1.7.3：补上本轮审计发现的 F821 的达主测试。
- v1.8：P0 五大服务 (`outline_generator / style_abstractor / feature_extractor / beat_extractor / chapter_generator`) 每个 3–5 个单测。目标总覆盖 ≥ 60%。
- v1.9：补 `text_pipeline / strand_tracker / memory / agents/`。
- 使用与 Z3 同样的 “fake provider” 模式避免上上游。

## 5. 依赖安全

### 5.1 Python (`pip-audit --strict`)

```
Found 4 known vulnerabilities in 2 packages
Name  Version  ID              Fix Versions
----- -------- --------------- ------------
pip   24.0     CVE-2025-8869   25.3
pip   24.0     CVE-2026-1703   26.0
pip   24.0     CVE-2026-3219   (未公布修复版)
wheel 0.45.1   CVE-2026-24049  0.46.2
```

**评估**：pip / wheel 是构建工具，**不是运行时依赖**。在主服务运行面上，4 个漏洞都不在受攻击面上（除非有人在产环境里跳进容器 `pip install`，而这不是我们的正常运维姿势）。在下次造镜像时顺手开主 pip。优先级 P3。

### 5.2 Frontend (`npm audit`)

```
4 moderate severity vulnerabilities
  postcss vulnerable in next 下依赖链
```

**评估**：moderate 级别，`npm audit fix` 应能无破坏性修复。面向公开 SaaS 的话该推上。P2。

## 6. 高优先级发现汇总

### 🔥 P0 仅一项

1. **`app/services/model_router.py:765` F821 NameError**。详见 §2.2。**看完本报告紧接着处理**（v1.7.3 hotfix）。

### 🟠 P1

2. **五大核心服务 0–40% 覆盖**：`outline_generator(13%) / chapter_generator(42%) / style_abstractor(0%) / feature_extractor(41%) / beat_extractor(0%)`。由其是收费 / 决策边缘 case 最多的场景，必须在 v1.8 全部补。
3. **`app/api/generate.py` 多个 `prompt` 拼装中间变量被丢弃**（`world_rules` `book_summary` `current_text` `previous_text`）。贴近业务价值，需人工补检是否是 “本应拼进去但遗忘了”。
4. **`scene_orchestrator` 体量边缘 case**（ch3=23 字 / ch4=14337 字）：当前该服务覆盖率 90%，但接到的用例并不含该两个边缘场景。补两个回归用例。

### 🟡 P2

5. **前端 npm audit 4 moderate**：公开 SaaS 前需 `npm audit fix` 。
6. **mypy [attr-defined] 33 条**：需人工过一遍筛出真 bug。
7. **`pacing_checker / consistency_checker / reader_pull_checker` F841 dead-variable**：可能是检查器业务逻辑遗漏，需阅代码。

### 🟢 P3

8. ruff F541 空 f-string × 4 / E741 嵌名 `l` × 4 / E401·E402·F811 各 1。一次性 sed/--unsafe-fixes 可装。
9. pip / wheel CVE：下次造镜像时顶个版本。

## 7. 门禄接入路线图

| 阶段 | 门禄项 | 实现点 |
|---|---|---|
| v1.7.3 | F821 修复 + 补单测 | `model_router.stream_by_route` 套补 |
| v1.8 | ruff `F` `E` 门禄（不准新增）；P0 五大服务覆盖 ≥ 60%。 | `pyproject.toml` + Make/CI step + 5 × (3–5) 单测 |
| v1.9 | 补齐 `text_pipeline / strand_tracker / memory / agents/`；branch coverage。 | |
| v2.0 | mypy 调起 “strict-optional” 门禄。 | |

## 8. 未走过的项目 / 剩下空白

- 压测 / 负载 / k6 / artillery。
- 分析型静态安全扫描。如 bandit / semgrep 。下次。
- LLM prompt injection / jailbreak 专项。仅 anti_ai_scanner 在 23% 覆盖。
- Grafana 面板与告警规则。未梳理。

---

## 附录：照贴错误原始（选要者）

### F821 上下文

```text
app/services/model_router.py:765:69: F821 Undefined name `task_type`
```

### mypy main.py 微趣实例（1 条）

```text
app/main.py:143: error: Incompatible types in assignment (expression has type "dict[str, int]", variable has type "int")  [assignment]
app/main.py:144: error: "int" has no attribute "values"  [attr-defined]
```

### 覆盖现场运行

```text
252 passed, 8 warnings in 10.53s
TOTAL  15222   9997   34%
```
