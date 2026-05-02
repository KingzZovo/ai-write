# Release Notes — v1.7.4

**HEAD**: `d6ab85e` (P1-fix) on `feature/v1.0-big-bang`
**Alembic head**: `a1001900`
**Tests**: 327 passed (post P1-fix re-run)
**Tag base**: v1.7.3

---

## TL;DR

v1.7.4 是 **anti-AI（去 AI 味）** 一揽子整改：补齐 P0 的 ContextPack / 章节摘要 / 大纲 ETL，修风格链与项目-参考书绑定，把 generation 与 polishing 的 system prompt 升到 v3 三支柱，最后一脚把 ChapterGenerator → run_text_prompt 链路上 v3 prompt 被 silent bypass 的关键路径修掉。修复后 ch8 重生成实测三大指标全部达标。

---

## P0 — ContextPack 数据底座（已 tag 在 v1.7.3 之前的中间提交，归档于 v1.7.4 release window）

### P0-1 — 注入 book/volume outline 到 ContextPack（5f7b692）
- 修复 ContextPack 在生成时缺失全书 / 卷 outline 上下文的问题
- 让 ChapterGenerator 拿到层级化大纲，而不是只有相邻章节摘要

### P0-2 — chapter post-summarizer + dirty-data cleanup（689eaa0）
- 章节生成完成后自动跑 post-summarizer，落到 chapter summary
- 清理历史脏摘要（旧版残留）

### P0-3 — outline → facts ETL（587e289）
- 把 outline_json 中的固定事实抽到结构化 facts 字段
- 给 ContextPack/Pack downstream 提供稳定的 fact 锚点

## P1-α — 风格链修复（6648386）

- `_load_style_samples` 既有 bug 修复
- 加 cards fallback：当 style_samples 缺失时回退到 reference card slices
- 注入路径：`context_pack.py` line 219-220、389-400

## P1-β — 项目绑定龙族 reference_book

- 项目 `f14712d6-...`「验收测试-玄幻全本200万」绑定 reference_book `24498b6b-2698-4900-b44b-b42806964e1b`（《龙族》）
- 让 anti-AI 风格 reference 链路有真实素材可用

## P1-1 / P1-2 — generation + polishing prompt 升级到 v3（30f7b42）

两个 prompt_assets 由 v1 升级到 v3，三支柱设计：

1. **对话密度** — 鼓励对话占比 ≥35%
2. **世界观黑话** — 强制使用世界词、具体名词
3. **句长极端方差** — 短句 / 长句交替，句长 std 拉大

附加约束：
- 黑名单 12 词（典型 AI 味词汇）
- 护城词（必须出现的世界词类）
- 五感落地、数字落地（具体距离 / 数量）
- 短段强制（≤6 字独立段比例）

生成版：`backend/app/prompts/v174_generation_v3.txt`（4732 B）
润色版：`backend/app/prompts/v174_polishing_v3.txt`（3760 B）

DB upsert：
- generation 行 `18721e12-95f8-40c8-a402-70b66564017f`（v2 active, sys_len 1862）
- polishing 行 `a526be4b-a5f3-4332-bd34-5dd955ad3df8`（v2 active, sys_len 1506）
- 各保留 v1 备份 `is_active=0`

## P1-fix — ContextPack 路径 prompt silent-bypass 修复（d6ab85e）

**根因**：`run_text_prompt` / `stream_text_prompt` 在调用方传入 `messages=` 时，原代码直接使用调用方提供的 messages，**完全跳过** `route.system_prompt`。所有 ContextPack 路径（ChapterGenerator）DB 层 prompt_assets 改动均无运行时效果。

**修复**：在两处函数加 `else` 分支，把 `route.system_prompt`（与 extra_system 拼接后）作为前置 system 消息 prepend 到 messages 头部。位置：
- `prompt_registry.py:730-740`（run_text_prompt）
- `prompt_registry.py:919-927`（stream_text_prompt）

**验证**：ch8 e2e 重生成，messages 结构由 `[system(pack), user]` 变为 `[system(v3, 1862), system(pack, 7557), user(48)]`，input_tokens 6833 → 8279。

## P1-D — e2e 验收

直接调用 `ChapterGenerator.generate(ch8)` 端到端验证。

### Round 1（HEAD=30f7b42，P1-fix 应用前）
- ContextPack 单独已能让输出体面
- v3 prompt 被 silent bypass（messages 中无 v3 system）

### Round 2（HEAD=d6ab85e，P1-fix 应用后）

| 指标 | Round 1 | Round 2 | 目标 | 状态 |
|---|---|---|---|---|
| 对话占比 | 30% | 40% | ≥35% | ✅ |
| 黑名单出现次数 | 2 | 0 | ≤3 | ✅ |
| 比喻总数 | 20 | 12 | ≤8 | ⚠️ 仍偏多 |
| 世界词总频次 | 211 | 256 | 多 | ✅ |
| ≤6 字独立段比例 | 9% | 20% | 多 | ✅ |
| 句长 std | n/a | 11.5 | 极端 | ✅ |

尾段已自带 v3 招牌打法（短句 + 钩住式收束，如「短句。」「钩住了。」）。

**朱雀 AI 检测**：由用户人工执行，结果回传后再决定是否需要 D 步收紧。

---

## 已知缺陷（带入 v1.8）

- ch3 `0aa149fa` 完成态但 content_text 仅 23 字（异常生成结果未及时识别）
- ch8 `d54552cc` draft 0 字 — ChapterGenerator.generate 输出未持久化，后端落盘点 4 处需统一修复
- `knowledge_tasks.py:260` 用了 `ChapterGenerator.generate_stream` 过时签名（dead code，不影响主链路）

---

## 文件清单

代码：
- `backend/app/services/prompt_registry.py`（patched, 34206 B）
- `backend/app/services/chapter_generator.py`（ContextPack → messages → run_text_prompt 主链路）
- `backend/app/services/context_pack.py`（P1-α 风格注入）
- `backend/app/prompts/v174_generation_v3.txt`
- `backend/app/prompts/v174_polishing_v3.txt`

输出：
- `/root/ai-write-shared/ch8_v174_round1.txt`（无 v3，ContextPack only）
- `/root/ai-write-shared/ch8_v174_round2.txt`（含 v3，最新基线）

---

## Commits in this release window

```
d6ab85e feat(v1.7.4 P1-fix): prepend route.system_prompt when messages provided
30f7b42 feat(v1.7.4 P1-1/P1-2): rewrite generation+polishing prompts (anti-AI 3-pillar)
6648386 feat(v1.7.4 P1-alpha): repair style chain - fix bugs in _load_style_samples + add cards fallback
587e289 feat(v1.7.4 P0-3): outline -> facts ETL
689eaa0 fix(v1.7.4 P0-2): chapter post-summarizer + dirty-data cleanup
5f7b692 fix(v1.7.4 P0-1): inject book/volume outline into ContextPack
```
