# v0.6.0 — 离线反编译 + 三库语义解耦

**目标：** 把参考小说从"原文片段"升级为"结构化指令 + 抽象情节骨架"，根治抄袭风险；把 Qdrant 三集合的语义边界明确成"设定 / 情节 / 风格"三路召回。

## 核心思路

1. 参考书入库时做一次**反向工程**：强推理模型按切片抽取
   - 风格特征（句节奏/词汇色彩/叙事视角/对话特征）→ 结构化 JSON
   - 情节骨架（核心矛盾/叙事节拍/情绪曲线）→ 去专名化 beat sheet
2. 新增**脱敏层**：当仍需召回原文片段作风格参考时，先把人名/地名/法宝替换为代词
3. **ContextPack v2**：L3 RAG 改为三路召回（设定/情节/风格），而非粗粒度灌入
4. **Feature flag 灰度**：`CONTEXT_PACK_V2_ENABLED` 默认 off，稳定后翻

## 数据层

### 新 Qdrant 集合

| 集合 | 维度 | payload | 来源 |
|---|---|---|---|
| `style_profiles` | 1536/4096 | `{book_id, slice_id, profile_json}` | style_abstractor |
| `beat_sheets` | 1536/4096 | `{book_id, slice_id, beat_json}` | beat_extractor |
| `style_samples_redacted` | 1536/4096 | `{book_id, slice_id, redacted_text, entities_map}` | entity_redactor |

旧集合 `plots` / `styles` 标记 deprecated，保留数据不再写入；`chapter_summaries` 维持不变。

### 新 Postgres 表

- `reference_book_slices` — 语义切片登记（id/book_id/slice_type/start_offset/end_offset/raw_text_hash/token_count/created_at）
- `style_profile_cards` — 风格结构化镜像（id/book_id/slice_id/profile_json/created_at）
- `beat_sheet_cards` — 情节骨架镜像（id/book_id/slice_id/beat_json/created_at）

Alembic: `a0600000_v06_decompile_and_decouple.py`，down_revision=`a0504000`。

## 服务层

| 模块 | 职责 |
|---|---|
| `semantic_chunker.py` | 规则切片：对话完整性 > 自然段 > 场景过渡词 > token 上限 |
| `style_abstractor.py` | `task_type="style_abstraction"` → Style Profile JSON |
| `beat_extractor.py` | `task_type="beat_extraction"` → Beat Sheet JSON（去专名） |
| `entity_redactor.py` | `task_type="redaction"` → 人名/地名替代为代词 |
| `reference_ingestor.py` | 编排离线反编译管线（Celery 任务） |
| `context_pack.py`（改造） | v2 三路召回 + feature flag 回退 |

## Prompt Registry 新条目

- `style_abstraction`（structured）
- `beat_extraction`（structured）
- `redaction`（text）
- `critic`（structured，为 v0.7 铺路）

## API

- `POST /api/reference-books/{id}/reprocess` — 触发反编译
- `GET /api/reference-books/{id}/style-profiles` — 列表
- `GET /api/reference-books/{id}/beat-sheets` — 列表
- `/api/vector/collections` 自动包含三个新集合

## 前端

- `/vector` 页面增加 `style_profiles` / `beat_sheets` / `style_samples_redacted` tabs
- 参考书详情页增加"反编译"按钮

## 验收标准

- [ ] 单参考书导入触发 reprocess → 三集合各有 points
- [ ] `/vector` 能浏览三集合 payload
- [ ] 启用 v2 后 llm_call_logs.rag_hits 能看到三路召回证据
- [ ] final prompt 中没有原参考书的人名/地名字面出现
- [ ] v2 off → 行为与 v0.5 完全一致
- [ ] tsc/next build/backend tests 全绿

## 环境变量

- `CONTEXT_PACK_V2_ENABLED` (bool, default=false)
- `SEMANTIC_CHUNKER_MAX_TOKENS` (int, default=800)
- `STYLE_REDACTION_ENABLED` (bool, default=true)
