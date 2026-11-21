# E2E 小说生成验收测试计划（2026-07-26）

**目的**：重启部署后（config 收归 + tasks 拆分 + 生成质量四机制上线），端到端生成一本小说的前几章，验证全链路与新机制在真实 LLM 流量下工作正常。

## 0. 前置状态

- backend / celery-worker 已重启（2026-07-26），router 日志确认 `Model router loaded: 2 providers, 26 task routes`
- relay (51.83.5.205:8317) 实测新增可用：`claude-opus-5` / `claude-sonnet-5` / `claude-fable-5`（600 字散文探针全部通过：finish=stop、无空文本、无截断；延迟 opus5≈33s / sonnet5≈20s / fable5≈24s）
- 本次 E2E 用 **claude-sonnet-5** 做 flagship（量产主力候选，速度最快、成本最低、质量接近 opus）

## 1. 测试矩阵

| # | 步骤 | 端点 | 通过标准 |
|---|------|------|----------|
| 1 | 切 flagship default_model → claude-sonnet-5 + restart backend | DB + compose | router 重载日志出现 |
| 2 | 登录/铸 JWT | auth | 200 + 后续调用鉴权通过 |
| 3 | 创建测试项目（twc=40000 → 1卷≈10章；target_chapter_words=3200） | POST /api/projects | 201/200，拿到 project_id |
| 4 | 全书大纲 | POST /api/generate/outline {level:book} (SSE 直连:8000) | data:[DONE] 收尾；outlines 表 book 行 content_json 有意义（题材对齐 user_input，非通用武侠跑偏） |
| 5 | 第 1 卷大纲 | POST /api/generate/outline {level:volume,volume_idx:1} | Volume + Chapter 行物化；章骨架 outline_json 含 chapter_idx/title/summary/key_events；题材锚定（premise anchor 生效） |
| 6 | 前 3 章正文 | POST /api/generate/chapter {target_words:3200} | 每章：status=completed；word_count 落 4000-8000 带（允许 LLM 方差 1 章出带但非空）；SSE 无 error 事件 |
| 7 | 新机制断言（对生成正文） | DB 检查 | ① 无 `[CH-n]`/`[VOL-n]`/`第X章` meta 泄漏（sanitizer + 检查门）② 无图像拒绝话术 ③ 结尾为终止标点（looks_truncated 未触发或已正确标 draft）④ 角色名与 roster 一致 |
| 8 | 质量门/管线日志 | docker logs | 无 unhandled exception；若有 sanitizer hits 日志，属正常拦截行为并记录数量 |

## 2. 失败处理

- 单章出带（<4000 或 >8000）：LLM 方差，非 bug；记录不阻塞
- SSE 中断 >600s：绕 nginx 直连 :8000，curl --max-time 1800
- 空响应/拒绝：persist-on-block + refusal/truncated 机制应保底（status=draft 不清空）——若清空即 bug

## 3. 模型选型建议（官方 API 定价口径）

| 模型 | 官方价 $/1M in/out | 定位 |
|------|-----|------|
| claude-fable-5 | 10/50 | 最强长程写作；thinking 常开额外耗 token；适合全书/分卷大纲这类低量高杠杆环节，正文量产成本×2 不划算 |
| claude-opus-5 | 5/25 | 大纲默认 + 正文质量升级选项 |
| claude-sonnet-5 | 3/15（宣传期 2/10 至 2026-08-31） | **正文量产主力**：质量接近 opus，探针最快 |

推荐组合：大纲/审校路由 → opus-5；scene_writer/正文路由 → sonnet-5；质量门返工率异常再升级。经中转 relay 时实际计费以 relay 为准。

## 4. 产物

- 测试项目保留（不删），project_id 记录在验收报告中供人工审阅正文质量
