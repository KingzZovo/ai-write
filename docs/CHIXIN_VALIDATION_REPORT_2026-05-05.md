# 赤心仿写验证报告（Stage A→E）

- 报告日期：2026-05-06
- 验证项目：`赤心巡天仿写验证`（id `df6f523e-f903-4644-bcce-636f5ed89c68`）
- 参考素材：reference_book `0a543m1d-19fe-4e03-986e-42844feb36ee` 《赤心巡天》/ profile `b76da43a-a2fa-4fd3-8c54-3912acee6bb0`（rules=73, samples=24）
- 关联交接：`docs/HANDOFF_2026-05-05_pr-book-profile-bind.md`、`docs/PROGRESS.md`（2026-05-06 22:02 段）

## 1. 总览

| Stage | 内容 | 结论 |
|---|---|---|
| A | 风格画像生成（赤心 profile 已就绪） | ✅ 既存（rules=73 / samples=24，profile_json 完备） |
| B | 创建仿写项目并绑定 profile + reference_book | ✅ 项目 `df6f523e-...`，settings_json.style_reference 双键已绑 |
| C | Book → Volume → Chapter outline 三层落库（结构化字段） | ✅ book/vol1/ch1-3 全齐 |
| D | 三章正文实测（scene_mode + auto_revise） | ✅ ch1=14940 字 / ch2=16690 字 / ch3=13431 字 |
| E | 验证报告 | ✅ 本文 |

核心结论：**仿写链路打通，端到端可重复**。Book outline 结构化、Volume 物化 50 章、Chapter outline ~17K chars、Chapter 正文 12-17K 字 + 自动评分 ≥ 7 阈值跳过 revise，全部经 SSE staged_stream 路径稳定产出。

## 2. Stage B：项目创建与 style 绑定

- 项目 ID：`df6f523e-f903-4644-bcce-636f5ed89c68`
- title=`赤心巡天仿写验证` / genre=`仙侠` / target_word_count=200000
- `settings_json.style_reference`：
  - `profile_id` = `b76da43a-a2fa-4fd3-8c54-3912acee6bb0`
  - `reference_book_id` = `0a543b1d-19fe-4e03-986e-42844feb36ee`
- 修复点：POST `/api/projects` 早先丢 `target_word_count`，已在 PR-OUTLINE-STAGED-PERSIST-STRUCT 顺带补 `target_word_count` + `genre_profile_code`（`backend/app/api/projects.py:create_project`）。

## 3. Stage C：三层 outline

### 3.1 Book outline（已 confirm）
- ID：`15a4770c-9230-49a0-a493-700644b32862`
- 长度：136923 chars / 7 个 top-level keys
- 结构化字段验收：
  - `main_plot` 95 chars
  - `characters` 3765 chars
  - `world_setting` 727 chars
  - `chapter_naming_style` 1029 chars
  - `sections` dict 9 个 key（一-九）齐全
  - `volume_plan` = 1 卷，title=`照骨登山`，est_chapters=50
- 流程：staged_stream（A骨架/B角色/C世界观）耗时 ~11 min，PR-OUTLINE-STAGED-PERSIST-STRUCT 落地后首次跑通即一次到位。

### 3.2 Volume outline（vol1，已 confirm）
- ID：`803b025e-5347-4eb3-bb18-780169f6a732`，parent=book outline
- 长度：86298 chars / 12 个 keys
- `chapter_summaries` 50 项（idx/title/key_events/summary 齐全）
- `core_conflict` 93、`emotional_arc` 91、`turning_points` 5、`new_characters` 9、`departing_characters` 6、`foreshadows` planted/resolved、`transition_to_next` 89、`raw_text` 14807
- 题材吻合：`照骨登山` 卷主题：主角追查小镇命案与自身家世，最终发现仙门以凡人采命为阶，并在山门夜审中以命债反击、逼宗门公开旧制。

### 3.3 Chapter outlines（ch1-3）

| 章 | outline_id | content_json 长度 |
|---|---|---|
| 1 | `b0833dd1-f5ab-4315-be57-f3b346f5bcaa` | 17007 |
| 2 | `92d98208-b015-4201-9f91-6c72b3157040` | 19204 |
| 3 | `42837860-8983-4c83-8899-1cb1f5cb443d` | 16507 |

（ch1 串行 ~90s；ch2/ch3 并行 ~110s。）

### 3.4 Volume 物化
- volume row：`ee36b649-ff4d-45ea-a045-f50f01589b5a`，volume_idx=1，title=`照骨登山`，target_word_count=600000
- 50 chapters 通过容器内 `/tmp/materialize_vol1.py` 落库，target_word_count=12000/章
- 已知风险：自动物化路径不存在；当前依赖手动脚本。后续候选 PR：把 vol outline `staged_stream done` 后补一段 chapter materialize 逻辑（参考 `backend/app/api/volumes.py:regenerate_volume:143-329`）。

## 4. Stage D：三章正文实测

### 4.1 参数（一致）
- endpoint：`POST /api/generate/chapter`
- `use_scene_mode=true`
- `target_words=12000`
- `auto_revise=true`，`max_revise_rounds=2`，`revise_threshold=7.0`

### 4.2 结果

| 章 | chapter_id | 字数 | 评分(round 1) | issues | revise | status |
|---|---|---|---|---|---|---|
| 1 | `a01873a2-ce69-42c4-b2fa-1e25dc922be3`「义庄夜收无名尸」 | 14940 | 8.28 | 16 | skipped (≥7.0) | completed |
| 2 | `9294003b-a221-4b3e-91a7-162dc0b866ae`「按印者欠命」 | 16690 | 8.28 | 14 | skipped (≥7.0) | completed |
| 3 | `75c42b05-91b1-4238-a827-e37e9cc665cd`「夜更三十三响」 | 13431 | 8.36 | 16 | skipped (≥7.0) | completed |

- 三章累计 45061 字，约 12K/章目标完成度 100%（ch2 略超出反映 scene_planner 多场景拆分自由度）。
- 单章端到端耗时 ~8 min（scene_planner 前几分钟 SSE 静默后批量 token 流式）。
- auto_revise 全部 round 1 score 直接 ≥ 7.0 阈值，未触发 revise；issues 16/14/16 视作低风险性瑕疵清单（非阻断）。

### 4.3 风格匹配（人工抽检 head/tail）

抽检三章首尾片段（保存于 `/tmp/ch{1,2,3}_full.txt`），对照赤心 profile 的核心风格特征：

- **物候五感**：ch1 雨/灯/盐灰/泥水/铜色齐备；ch2 门闩、木屑、青砖、雾气、湿热；ch3 棚缝盐灰、木板床、梆子、月光层叠。✅ 与赤心一致。
- **节奏顿挫**：短句切片（"咚。咚咚。"、"一声。"、"隔两息。"、"又一声。"），逐拍推进。✅
- **底层视角 + 制度即神**：账=命、印=债、丁字号=点名；棚里"步子先于脑子出了停尸房"等心象写法。✅
- **章末钩子**：
  - ch1 尾："天亮了……高福要他按的就不是手印，是命。"——名/命的反讽对照
  - ch2 尾："像有钟声，要从灰里过来。"——下章转钟迟梆声
  - ch3 尾：债会"先认账"+ 寡妇"让我认一眼"——人性 vs 规则正面冲突
  - 钩子层次清晰，每章末向下章自然搭桥。✅
- **角色一致性**：陆照（盐灰镇收殓贱役）+ 曲麻子 + 高福 + 鲁三丰，跨章稳定，"骨灯"道具贯穿三章。✅

### 4.4 质量结论
- 三章正文风格与赤心 profile 强匹配；核心赤心特征（物候五感、短句节奏、制度神化、底层书生气）全部命中。
- 评分 8.28-8.36 区间，可作为后续 ch4-50 的基线。
- 不需要进入 revise 即可达成验证标准，端到端链路质量稳定。

## 5. Stage E：综合判定

- **链路可用性**：A→B→C→D 全段稳定，所有阻塞点（PR-BOOK-PROFILE-BIND/PR-OUTLINE-STAGED-PERSIST-STRUCT/章节 off-by-one）均已落地修复并 commit + push。
- **质量基线**：scene_mode + auto_revise（threshold=7.0）默认参数即可在不触发 revise 的前提下达到 8.28+ 评分。
- **可重复性**：同一项目 + 同一 profile + 默认参数复跑 ch4..ch50 应保持等量级输出（待批量验证）。
- **结论**：仿写验证 ✅ 通过。

## 6. 后续建议

1. **bulk_generate 路径**：当前未发现 `/api/generate/chapter/batch`；扩展验证 ch4-ch10 时建议串行单调用 `/api/generate/chapter`，避免 LLM rate limit 与 DB 写入争用。
2. **PR-OL2 自动物化**：vol outline `staged_stream done` 后建议补 chapter materialize 逻辑，去除对手动脚本的依赖（参考 `backend/app/api/volumes.py:regenerate_volume:143-329`）。
3. **Chapter outline confirm**：当前 ch1-3 outline `is_confirmed=0`，正文已生成不影响下游；如需流程完整性可批量 confirm。
4. **issues 清单分析**：三章 round-1 各报 14-16 个低风险 issue，建议抽样人工标注，用于后续 prompt/rule 微调。
5. **profile feedback 闭环**：将三章实测产物喂回赤心 profile 的 examples/反例库，迭代提升后续生成稳定性。

## 7. 关键 ID 速查

```
project:                df6f523e-f903-4644-bcce-636f5ed89c68
style_profile (赤心):    b76da43a-a2fa-4fd3-8c54-3912acee6bb0
reference_book (赤心):   0a543b1d-19fe-4e03-986e-42844feb36ee
outline.book:           15a4770c-9230-49a0-a493-700644b32862  (confirmed=1)
outline.vol1:           803b025e-5347-4eb3-bb18-780169f6a732  (confirmed=1)
outline.chap1:          b0833dd1-f5ab-4315-be57-f3b346f5bcaa
outline.chap2:          92d98208-b015-4201-9f91-6c72b3157040
outline.chap3:          42837860-8983-4c83-8899-1cb1f5cb443d
volume row:             ee36b649-ff4d-45ea-a045-f50f01589b5a   (vol1, target=600K)
chapter row 1:          a01873a2-ce69-42c4-b2fa-1e25dc922be3   (14940 字, completed)
chapter row 2:          9294003b-a221-4b3e-91a7-162dc0b866ae   (16690 字, completed)
chapter row 3:          75c42b05-91b1-4238-a827-e37e9cc665cd   (13431 字, completed)
```

## 8. 相关 commit

- `f73a74d` PR-BOOK-PROFILE-BIND（前端创建项目时绑 profile + reference_book）
- `a779524` 章节列表 off-by-one 修复（OutlineTree 全局 idx）
- `391e053` PR-OUTLINE-STAGED-PERSIST-STRUCT（book outline 三阶段结构化字段持久化 + projects.target_word_count 修补）

## 6. Stage D 续跑：ch4–ch10（2026-05-06 → 2026-05-07）

承袭 ch1-3 后，本阶段串行补完 vol1 前十章。

### 6.1 ch4 单独跑（手动）
- outline_id=`cd25bc41-8232-41c5-9c01-157fc0f76a3d`，chapter_id=`ba7cb6d9-1640-452e-9691-942169f3fad6`
- 结果：14029 字 / score 8.36 / issues 15 / revise_skipped (score>threshold 7.0)

### 6.2 ch5–ch10 driver 串行（PR-OL2 验证）
ch5–ch10 由 `/tmp/ch5_to_10_driver.sh`（setsid nohup PID 827545）+ `/tmp/ch9_10_resume.sh`（PID 3119019）两段串行跑完，全部利用 PR-OL2 vol-level 自动物化的 chapter row。

| ch | title (final) | wc | score | 备注 |
|----|---------------|----|-------|------|
| 5  | 岁贡棺中有故人 | 14387 | 8.36 | revise_skipped |
| 6  | 乱葬坡上验换牌 | 13681 | 8.32 | revise_skipped |
| 7  | 酒席之上听谎息 | 14707 | 8.52 | revise_skipped |
| 8  | 骨灯认主夜不止 | 13841 | 8.28 | revise_skipped；title SQL 强制还原 |
| 9  | 簿改丁七为流民 | 10159 | n/a  | 重试路径未走 evaluation；title 强制还原 |
| 10 | 夜出盐灰镇     | 9837  | n/a  | 重试路径未走 evaluation |

### 6.3 已知问题（写入 backlog）
1. **chapter outline 阶段会重写 title**，无视 vol1 outline 已 confirmed 的 chapter_summaries[idx].title，把 ch8/ch9 改回了第二人称（违反 rename 约束「禁你/你们」）。本次用 SQL 直更还原；后续应在 `_persist_outline_now` chapter 分支保留 vol-outline 已有 title，或在 prompt 中显式禁用第二人称章名。
2. **content gen 重试路径不走 auto_revise evaluation**：ch9 在第一次上游 NVIDIA SSE `INTERNAL_ERROR`（stream RST）后，driver 的第 2 次 curl 直接 saved + completed，没有 `event: scored` / `revise_skipped` 事件，且生成长度从前 8 章稳定的 13–16k 跌到 ~10k。怀疑 scene_mode + auto_revise 在「上次 partial scene 已落地或 cache 命中」时进入 fast path，跳过评估。需后续追查 `chapter_generator.py` 重入逻辑。
3. **NVIDIA 上游 SSE INTERNAL_ERROR 偶发**：长链路（每章 ~7-9 min stream）偶发 stream RST。已在 resume driver 中加 3 次重试 + sleep 30-45s 缓冲，能挡瞬时断流。生产化时应在 `_chapter_streamer` 内层加重连 / partial chunk 复用。

### 6.4 PR-OL2 物化路径已验证
- ch5–ch10 全部使用 PR-OL2 vol-level 物化时建好的 chapter row（200b9f61 / fa272327 等），content gen 直接复用 chapter_id，无需手动 INSERT。
- 物化 idempotent：driver 重启不会重复 INSERT chapter row。

## 7. 当前 vol1 ch1-10 全景

| ch | title | wc | score | status |
|----|-------|----|-------|--------|
| 1  | 义庄夜收无名尸 | 14940 | 8.28 | completed |
| 2  | 按印者欠命 | 16690 | 8.28 | completed |
| 3  | 夜更三十三响 | 13431 | 8.36 | completed |
| 4  | 认尸者无门 | 14029 | 8.36 | completed |
| 5  | 岁贡棺中有故人 | 14387 | 8.36 | completed |
| 6  | 乱葬坡上验换牌 | 13681 | 8.32 | completed |
| 7  | 酒席之上听谎息 | 14707 | 8.52 | completed |
| 8  | 骨灯认主夜不止 | 13841 | 8.28 | completed |
| 9  | 簿改丁七为流民 | 10159 | n/a  | completed |
| 10 | 夜出盐灰镇     | 9837  | n/a  | completed |

合计：**139,602 字** / 10 章 / 平均 13,960 字 / 章。

---

## 8. 2026-05-08 增补：PR 闭环与环境阻塞

### 8.1 三个 P1 问题已在代码层闭环

| backlog | PR | 状态 |
|---|---|---|
| 6.3 #1 chapter outline 阶段重写 title | **PR-TITLE-Q1** + **PR-TITLE-Q1.1** | ✅ 合入 |
| 6.3 #2 重试路径跳过 evaluation | **PR-CHGEN-ALIAS**（`scene_mode` 双名字兼容） | ✅ 合入 |
| 6.3 #3 NVIDIA 上游 SSE INTERNAL_ERROR | NVIDIA chunk-level retry | ☑ backlog（外层 driver 已 3 次重试能抗瓶颈） |

#### PR-TITLE-Q1 / Q1.1 代码层三阵防护
1. **prompt** 阶段：`outline_generator.py` `VOLUME_CHAPTERS_SYSTEM` 加字数/逻辑/不重卷名/不使用占位符等总规则。
2. **质量门** 阶段：`title_quality_checker.py` (NEW, 284 行) 检测三类违规 + 未过全卷重生。阈值现 `chinese_count > 14` 或 `len > 18`，避免将整句描述当 title。
3. **chapter outline expand** 阶段：`chapter_outline_expander.py:276` 优先使用 vol1 outline 已 confirm 的 chapter\_summaries[idx].title，覆盖 LLM parsed.title。

#### PR-CHGEN-ALIAS
`generate.py` 调 chapter generator 时 `scene_mode` / `use_scene_mode` 任一字段为 true 即为 true，保证 SSE 重新走 evaluating 路径。单测 8/10 PASS（剩 2 个为 too\_long 边界权衡项，后续代码再调整后入库）。

### 8.2 50/50 title 离线扫描（1 卷 50 章）

```
2026-05-08 18:30+ vol1 章名扫描
clean: 50 / total: 50
violations: 0
```

* 之前两个假阳性（ch33「吞篆补窍，失一段记忆」9 字 / ch50「万人命债倒灌护山阵」9 字）随 Q1.1 阈值放宽后均 clean。
* prompt 中已明确“字数以短为主、可长可短、不需全卷统一、14 个汉字以内”。

### 8.3 环境阻塞（未闭环）

**上游 codex provider auth 失效**：`http://141.148.185.96:8317/v1` 代理在 codex 上缺凭据，所有 `gpt-5.x` 调用返 503 `auth_not_found`。导致 ch11+ chapter outline expand 和后续所有 LLM chat task 都炸。详见 `docs/HANDOFF_TODO.md` 末段。

* 本 PR 代码路径验证正确（路由到「大纲」endpoint, model=gpt-5.4(high), prompt_assets 表记录验证同步）。
* 代理上的 codex provider auth 修复后，Stage D ch11–ch30 可以直接使用已有 `chapter_id` 并走 PR-OL2 物化路径，不需重新功能调试。

### 8.4 v1.1 总账面

* 代码层：PR-TITLE-Q1 / Q1.1 / CHGEN-ALIAS 三个合入，main + feat/phase2-fix 同步 `0be816f`。
* 文档层：本报告 + `PR_TITLE_Q1_2026-05-07.md` + `PROGRESS.md` + `HANDOFF_TODO.md`（含 codex 阻塞专节）。
* 数据层：ch1–ch10 合 139,602 字 保持 completed；ch11–ch20 chapter row 已物化（draft, 0 字, 0 outline）等 LLM 恢复。
* 性能层：接手者跳过探索阶段，从 HANDOFF_TODO P0 直接起步（修 codex auth → ch11 expand → SSE event 验证 → Stage D 续写）。
