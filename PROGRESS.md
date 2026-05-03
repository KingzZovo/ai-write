
## 2026-05-03 18:25 — Phase II 第二轮收尾（user msg 16）

### 真根因表（5 症状全击穿）
| 症状 | 真根因 | 修复 PR | 实测 |
|---|---|---|---|
| 设定集人物 profile 空 | next-server 旧 build + ETL 没覆盖新 outline | next-server 重启 v16.2.3 + ETL 重跑 | profile 行 14→28 ✅ |
| 世界规则空 | 同上 | 同上 | world_rules 111→194 ✅ |
| 章节大纲按钮无效 | 前端旧 build (lightweight outline_json 修复未上线) | next-server 重启 | 已 deploy ✅ |
| 第一卷空 | api/generate.py 端点不像 volumes.py 那样从解析结果创建 chapters 行 | PR-VOL15-SYNC SQL 直接拆 outline.content_json.chapter_summaries 落库 | vol1=150 / vol5=150 ✅ |
| TOKEN 用量 0 | (1) llm_call_logger import `add_usage` 不存在 (2) 又 import `resolve_user_id_from_context` / `_current_user_id_ctx` 也不存在 (3) outline_generator 不传 _log_meta 整个 outline 链路根本不进 logger | PR-USAGE-FIX-FN + PR-USAGE-FIX-IMPORT + PR-USAGE-LOGMETA | usage_quotas king/2026-05 prompt 0→36915 / completion 0→3424；verify_p2 注入 (123,45) AFTER-BEFORE 精确匹配 ✅ |

### 提交
- `dbbeebd` PR-USAGE-FIX-FN + PR-USAGE-LOGMETA + PR-VOL15-SYNC
- `b3fe18e` PR-USAGE-FIX-IMPORT (删冗余 quota imports)
- 已 push origin/feat/phase2-fix

### 实测增量（V2 PID 20d164ab... project）
- characters 47→80 (+33)
- characters_with_profile 14→28 (+14)
- world_rules 111→194 (+83)
- foreshadows 463→472 (+9)
- orgs 36→51 (+15)
- items 49→110 (+61)
- chapters 全 5 卷各 150 行 (vol1+vol5 由 PR-VOL15-SYNC 补齐)
- usage_quotas king/2026-05 prompt_tokens 36915 / completion_tokens 3424 (从 0 起)
