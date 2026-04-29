# Release Notes — v1.8.0

**HEAD**: 即将创建（dosage profile 渲染 + style_samples 截断 bug 修复）
**Branch**: `feature/v1.0-big-bang`
**Alembic head**: `a1001900`（不变）
**Tests**: 327 passed (post-fix 重跑前基线一致)
**Tag base**: v1.7.4 (`d6ab85e`) → 中间 v6 prompt 调整 (`b8d7ae6`) → v1.8.0

---

## TL;DR

v1.8.0 是 **dosage-driven anti-AI**（剂量画像驱动）的架构级胜利：从《龙族》全本 1,779,631 字提取 16 维风格剂量数字，注入 `style_profiles` 并在 ContextPack `_render_style_profile` 渲染到 system prompt。

首次实现朱雀 AI 检测 **Human 49.17% / AI 0%**（历史最佳 v178 仅 32.05% / 17.12%）。架构上从 v1.7.x 的「单向硬上限禁令式」转向「数据画像剂量式」——风格不靠规则禁止，靠参考书学习。

关键 bug 修复：`to_system_prompt()` line 401 `style_samples[:3]` 截断陷阱，导致 v1.7.x 阶段 9 行剂量数据被丢弃 8 行。

---

## v8-A — dosage-driven 风格画像架构

### 数据底座

- 新脚本 `/tmp/extract_dosage.py`：从龙族全文（5.99 MB）抽取 16 维剂量基线
  - paragraph / sentence count + length 分布
  - dialogue ratio + turn count + per-kchar
  - metaphor total + sentence-end metaphor + 5 specific patterns（像一/像被/像有人 等）
  - psychology canned phrases（13 类套语）+ neutral words（4 类中性词）
  - parallelism (XYX, ABAB)
  - colloquial particles + onomatopoeia
  - AI metawords（11 类，应为 0）
- 输出：`/tmp/longzu_dosage.json`
- 龙族 ground truth 关键数字：
  - 对话占比 0.3377，对话轮均长 27.43 字
  - 句长均 28.61 字，段长均 63.06 字
  - 比喻总量 4.4054/千字，句尾比喻 1.4964/千字
  - 心理戏套语 0.0315/千字（一章 7000 字仅 0.22 次）
  - 心理中性词 4.216/千字（正常心理描写，**非黑名单**）
  - 口语助词 0.7715/千字

### DB 注入

`style_profiles` 表 INSERT 1 行：

- `id`: `36fa0610-6df7-4e9a-aea9-6ea9ad1c9345`
- `name`: 「龙族剂量画像」
- `source_book`: `24498b6b-2698-4900-b44b-b42806964e1b`
- `config_json.dosage_profile`: 完整 16 维数字 + source 标识
- 还原 SQL：`/tmp/insert_longzu_profile.sql`

### 渲染

`backend/app/services/context_pack.py` `_render_style_profile()` +44 行：

- 当 `style_profile.config_json` 含 `dosage_profile` 时，按一章 7000 字换算渲染 9 行剂量段
- 渲染输出格式（写入 system prompt）：
  ```
  【剂量画像 — 仿写参考密度（按一章 7000 字换算）】
  · 对话占比 ≈ 34%，对话轮均长约 27 字…
  · 比喻总量 ≈ 4.4/千字（一章约 31 次），其中句尾比喻 ≈ 1.5/千字…
  · 心理戏套语 13 类总量 ≈ 0.032/千字，一章约 0.2 次。硬上限 ≤2 次。
  · 心理中性词 ≈ 4.2/千字（一章约 30 次）。这是「正常心理描写」，不是黑名单。
  · 句长均 29 字 / 段长均 63 字
  · 口语助词 ≈ 0.77/千字
  · prompt 自指语严禁出现
  ```

## 关键 bug — `style_samples[:3]` 截断陷阱

**症状**：渲染脚本验证 `_render_style_profile()` 已正确产出 9 行剂量数据并 extend 进 `pack.style_samples`，但实际 system prompt 只出现剂量画像标题，下面 8 行数字行全部丢失。

**根因**：`context_pack.py:401`：
```python
ss_text = "\n---\n".join(self.style_samples[:3])
```
4 层 token 分配（token_budget=8000）下 `style_samples` 槽位**硬截断为前 3 个 element**。`_render_style_profile()` 用 `parts.extend(dosage_lines)` 把 9 行各作为独立 element 加入，前 2 个槽位被「反 AI 块」「语气词汇块」占据，剩下 1 个槽位只够装剂量画像标题，**8 行数字行被无声丢弃**。

**修复**：把 9 行先 `"\n".join` 拼成 1 个字符串再 `parts.append`，整段剂量塞进 1 个 element。修复后 system prompt 7076 chars，9 行剂量数据完整出现。

**教训**：**多 element 输出**遇到**固定槽位截断**=静默丢失。任何向 `style_samples` extend 的渲染函数都必须先 join 成单个 element，或在 `to_system_prompt()` 改 `[:3]` 为更智能的 budget 分配。

## v8-A — e2e 验收（ch10）

端到端测试 `/tmp/e2e_ch10.py`，CHAPTER_ID=`4daca8b3-021a-4e9d-acf9-0914f27ffc5c`「黑市拍卖会」。

### 朱雀 AI 检测对照

| 版本 | Human | Suspected | **AI** |
|---|---|---|---|
| v4 (v175) | 27.30% | 56.07% | 16.63% |
| v5 (v176) | 5.42% | 46.75% | 47.83% |
| v6×5.2 (v177) | 10.15% | 72.73% | 17.12% |
| v6×5.4 (v178) | 32.05% | 50.83% | 17.12% |
| v7×5.4 (v179) | 15.22% | 33.06% | (?) |
| **v1.8.0 (ch10)** | **49.17%** | **50.83%** | **0%** |

**首次实现 AI 段清零**。Human 段从 v178 的 32.05% → 49.17%（+17.1pp，首次过半边线）。

### 8 维同口径剂量诊断（ch10 vs 龙族）

| 维度 | 龙族基线 | ch10 | 状态 |
|---|---|---|---|
| AI 元词 | 0 | 0 | ✅ |
| 心理戏套语（≤2/章） | 0.22 | 0 | ✅ |
| prompt 自指语 | 0 | 0 | ✅ |
| 对话占比 | 33.77% | 41.8% | ⚠️ 略超 |
| 对话轮均长 | 27.43 字 | 11.63 字 | ❌ 太碎 |
| 句长均 | 28.61 字 | 14.67 字 | ❌ 太短 |
| 段长均 | 63.06 字 | 25.53 字 | ❌ 太碎 |
| 比喻总量 | 4.41/千字 | 0.33/千字 | ❌ 13× 不足 |
| 句尾比喻 | 1.50/千字 | 0.22/千字 | ❌ 6.8× 不足 |
| 心理中性词 | 4.22/千字 | 1.32/千字 | ❌ 3× 不足 |

**模型把单向上限当成剂量目标**，把所有可压低维度全部压到接近 0。换来 AI 检测大胜，但损失了文学密度。带入 v1.8.1 阶段 B 用「双向区间」prompt 修复。

---

## 已知缺陷（带入 v1.8.1）

- `style_profiles.config_json.dosage_profile.source` 写成了 `longzu_full`（文件名）而非 `龙族`，渲染输出有「《longzu_full》原作采样基线」的不优雅文本。后续 UPDATE 修补。
- ch10 文学密度偏碎（句长/段长/比喻均显著低于龙族）：v1.8.1 阶段 B 用「双向区间」改写剂量段（`X-Y 次` 而非 `≤Y 次`）。
- 继承自 v1.7.4：ch3 `0aa149fa` 23 字 / ch8 `d54552cc` draft 0 字 / `knowledge_tasks.py:260` dead code。

---

## 文件清单

代码：
- `backend/app/services/context_pack.py` — `_render_style_profile()` +44 行（dosage_profile 渲染 + ss_text join 修复）

配置（DB-side，不在 git，需 SQL 还原）：
- `style_profiles` 行 `36fa0610-6df7-4e9a-aea9-6ea9ad1c9345`（龙族 16 维剂量画像）

脚本（runtime artifacts，不在 git）：
- `/tmp/extract_dosage.py` — 16 维剂量抽取器
- `/tmp/insert_longzu_profile.sql` — DB 还原 SQL
- `/tmp/longzu_dosage.json` — 龙族 ground truth
- `/tmp/diag_ch10.py` — 8 维生成质量诊断器（ch10 vs 龙族对照）
- `/tmp/verify_dosage.py` — pack 渲染独立验证器
- `/tmp/e2e_ch10.py` — 端到端 chapter generation 脚本（容器内）

输出：
- `/root/ai-write-shared/ch10_v8_output.txt`（11891 chars / 9063 中文字，朱雀 Human 49.17% / AI 0%）

---

## Commits in this release window

```
<待生成> feat(v1.8.0): dosage-driven anti-AI - longzu profile + style_samples truncation fix
b8d7ae6 fix(prompts): v6 generation prompt - kill metaphor + psy-formula, dialog ≥ 55%
d6ab85e feat(v1.7.4 P1-fix): prepend route.system_prompt when messages provided
```
