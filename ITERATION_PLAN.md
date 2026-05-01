# AI Write 迭代计划

## 当前版本 v0.5.0 (2026-04-21)

### v0.5 交付内容：Prompt Routing + 可观测性 + AskUser

| 模块 | 内容 | 状态 |
|------|------|------|
| PromptRegistry | RouteSpec + resolve_route 统一路由 | ✅ |
| ModelRouter | 改用 PromptAsset 路由，废弃 ModelConfig + /tasks 端点 | ✅ |
| LLM 日志 | llm_call_logger async ctx + run/stream_text_prompt 包裹 | ✅ |
| ChapterGenerator | 基于 ContextPack + PromptRegistry 重写，generate.py / cascade_regenerator 迁移 | ✅ |
| VectorStore | QdrantStore 扩展 list/delete/stats/search_by_vector + /api/vector-store | ✅ |
| RAG Rebuild | rag_rebuild 服务 + Celery 任务 | ✅ |
| CallLogs API | /api/call-logs 读/删 | ✅ |
| AskUserService | /api/ask-user/pending｜answer｜cancel | ✅ |
| 前端 /prompts | endpoint/model/temp/max_tokens + 分类 | ✅ |
| 前端 /settings | 去掉 task routing，端点为本 + v0.5 notice | ✅ |
| 前端 /vector | 3 集合 + points CRUD + 搜索 + rebuild | ✅ |
| 前端 /logs | 过滤 + 详情视图（messages + RAG hits） | ✅ |
| 前端 AskUserPrompt | 组件 + 挂载到 /workspace（URL 驱动 projectId） | ✅ |

烟测：`/api/ask-user/pending` 已在后端 `:8000` 注册，未鉴权返回 401；tsc --noEmit 通过。

---

## 历史版本 v0.4.0 (2026-04-19)

### 已完成功能

| 阶段 | 内容 | 状态 |
|------|------|------|
| Phase 1 | 核心写作管道（大纲→章节→双Agent生成→SSE流式） | ✅ |
| Phase 2 | 知识库（Legado书源+TXT/EPUB导入+风格聚类+质量评分） | ✅ |
| Phase 3 | 记忆系统（5层金字塔+Neo4j实体时间线+伏笔管理+Hook系统） | ✅ |
| Phase 4 | 质量保障（版本控制+批量生成+语义缓存+划线改写） | ✅ |
| 补完 | 所有 gap 修复 + LoRA 微调支持 | ✅ |
| 重构 | 前端可配置多端点模型路由 | ✅ |
| 认证 | JWT 登录 + 导航栏 + API 中间件 | ✅ |
| UX | 工作区项目选择+大纲向导+编辑器自动保存 | ✅ |
| 引擎 | 三层 Context Pack (SCORE/CFPG/DOME/CoKe/ToM) | ✅ |
| 审查 | 6 独立 Checker 并行执行 + 加权评分 | ✅ |
| 写作 | 写作指南引擎（7模块+13钩子+12题材+64 AI词库） | ✅ |
| 面板 | 质量检查+三线平衡+写作指南+去AI味 共4个新面板 | ✅ |
| 中文化 | 设置/知识库/面板/登录 全面中文化 | ✅ |
| 移动端 | 独立轻量 MobileWorkspace + 底部tab | ✅ |
| 过滤词 | 可配置词库 + AI 自动发现新词 | ✅ |
| 书源 | 健康评分 + 自动停用 + 分页搜索 + 启用/停用 | ✅ |
| 排行榜 | 夸克热搜/好评 + 分类 + 书籍搜索 | ✅ |
| 上传 | 大文件书源JSON上传（12MB+） | ✅ |
| **v0.4 项目管理页** | `/` 网格卡片 + `/trash` 回收站 + 软删 + 批量 + 重命名 + type-to-confirm 确认删除 | ✅ |
| **v0.4 工作区重构** | URL 驱动 `/workspace?id=X` + 返回按钮；向导步骤可跳可编辑；大纲 / 分卷行内编辑保存 | ✅ |
| **v0.4 分卷生成健壮化** | 空返回重试 + 跳过失败卷；前传/外传/番外/终章等非数字卷识别；已有卷跳过避免重复堆叠 | ✅ |
| **v0.4 字数目标系统** | 项目级 `target_total_words` / `target_chapter_words` + 章节 `target_words` 覆盖 + 生成时注入 prompt | ✅ |
| **v0.4 单卷重新生成** | `POST /volumes/{id}/regenerate` SSE 端点 + 侧栏三点菜单入口 | ✅ |
| **v0.4 关系表与关系图** | 新增 `relationships` 表 + CRUD + 批量 + extractor 自动提取写入 + 带 label 与 sentiment 色的 SVG 关系图 | ✅ |
| **v0.4 设定集提取 v2** | BOOK_OUTLINE_SYSTEM 扩展到 9 块（加主角小传 / 能力成长表 / 世界观设定集）；提取后同时写 characters / world_rules / relationships | ✅ |
| **v0.4 性能 + 体验 bug 修复** | lightweight chapters list（体积 -63%）；WritingGuidePanel / StyleSelector / StructureSelector 偏好持久化；SettingsPanel envelope 解包；修复刷新后误导按钮 | ✅ |

### 技术统计

```
60+ commits | 70+ 后端文件 | 50+ 前端文件 | ~30,000 行代码 | 90+ API 端点 | 18 ORM 模型
```

---

## Iteration 1: E2E 验证与 Bug 修复 ✅

**状态：** 主要目标已达成

- [x] 在 Settings 页面配置 LLM 端点
- [x] 完整流程：创建项目 → 生成全书大纲 → 分卷 → 章节 → 正文
- [x] 验证 SSE 流式生成在浏览器中正常工作
- [x] 测试书源导入 + 爬虫抓取
- [x] PC 端 DesktopWorkspace 稳定性修复（URL 驱动 + 向导重构）
- [ ] 完整跑通一本 5 章短篇小说自动化（留给用户实测）

## Iteration 2: 测试套件与 CI（上调优先级）

**目标：** 消除测试 gap，建立自动化质量保障。当前只有少量 pytest 覆盖 + asyncpg 事件循环冲突需要修。

- [ ] 修复 conftest.py 的 session-scoped event_loop 与 asyncpg 跨协程冲突
- [ ] 核心 API 集成测试：projects / volumes / chapters / outlines / relationships CRUD
- [ ] OutlineGenerator / settings_extractor 单元测试（可 mock LLM）
- [ ] detectVolumeCount / parseVolumeOutline 前端纯函数单测
- [ ] GitHub Actions CI（pytest + tsc + eslint + next build）
- [ ] pre-commit hooks（ruff / prettier）

## Iteration 3: 写法引擎资产化

**目标：** 将写法从静态模板升级为可编辑、可绑定、可编译的持久资产。

**参考：** AI-Novel-Writing-Assistant 的 StyleEngine（10 个服务模块）

- [ ] StyleProfile 持久化（DB 存储已有，前端 CRUD 待完善）
- [ ] StyleCompiler：将写法规则编译为 prompt 指令（带权重：≥0.85 必须保持 / ≥0.65 优先保持）
- [ ] StyleBinding：写法绑定到整本书 / 单个章节 / 单次生成（优先级层级）
- [ ] StyleDetection：从现有文本提取写法特征 → 保存为 Profile
- [ ] StyleRuntime：生成时动态解析当前激活的写法规则
- [ ] Anti-AI 规则升级：增加 `autoRewrite` 和 `detectPatterns` 字段
- [ ] 写法试写功能：选定写法 → 试写一段 → 评估匹配度
- [ ] 前端写法管理页面强化

## Iteration 4: Prompt Registry 统一管理

**目标：** 所有产品级 prompt 统一注册、版本化、可追溯。

**参考：** AI-Novel-Writing-Assistant 的 prompting/ 系统

- [ ] `PromptAsset` 数据结构（id/version/taskType/mode/contextPolicy/outputSchema）
- [ ] `PromptRegistry` 注册表
- [ ] 统一 Runner（`runStructuredPrompt` / `runTextPrompt` / `streamTextPrompt`）
- [ ] 将散落在 agent/service 中的 prompt（outline_generator / chapter_generator / settings_extractor / rewrite 等）迁入 registry
- [ ] 前端 prompt 管理页面（查看/编辑/版本对比）
- [ ] Prompt 效果追踪（每个 prompt 的成功率/评分）

## Iteration 5: 生产 Pipeline 增强

**目标：** 整本书生产流水线，支持断点恢复、快照回滚、状态机追踪、导出。

**参考：** AI-Novel-Writing-Assistant 的 NovelPipelineService

- [ ] Pipeline 状态机（planning → generating → reviewing → polishing → completed）
- [ ] 快照系统：生产前自动快照，失败可回滚
- [ ] 章节审校服务（生成 → 审校 → 修复 → 确认，最多 3 轮）
- [ ] 整本批量执行 + 断点恢复
- [ ] 生产状态面板（前端实时显示当前阶段/进度/失败原因）
- [ ] 导出：TXT（已有）→ EPUB / PDF / DOCX

## Iteration 6: 关系与设定集深化

**目标：** 把 v0.4 提取出来的 characters / world_rules / relationships 变成可交互可编辑的一等公民。

- [ ] 设定集页面强化：角色卡详情 + 能力成长表可视化 + 世界观按 category 分组 + 搜索
- [ ] 角色关系图：力导向布局（d3-force）替代圆形排列
- [ ] 关系 CRUD UI：拖线新建关系、右键删除、点击编辑 label/sentiment
- [ ] 关系变化时间线：按卷记录关系演变（如第一卷盟友 → 第三卷宿敌）
- [ ] 设定集修改联动：修改角色 profile_json 时触发 context pack 重建

## Iteration 7: LoRA 训练集成

**目标：** 从 Web UI 完成微调全流程。

- [ ] LoRA 管理页面
- [ ] 训练任务监控
- [ ] Adapter 浏览器（列表/预览/激活/A-B对比）
- [ ] 多风格 LoRA 运行时切换
- [ ] RWKV-7 模型集成
- [ ] vLLM/Ollama 自动检测

## Iteration 8: 高级写作技术

**目标：** 深度集成研究级写作技术。

- [ ] LangGraph 工作流编排（替代线性 Celery 任务链）
- [ ] Agent Tool Registry（参考 AI-Novel-Writing-Assistant 的 22 工具系统）
- [ ] ConStory-Checker 跨章节一致性深度检查
- [ ] BVSR 抽卡机制：生成多版本段落供选择
- [ ] SWAG 动作引导：限制模型的动作空间
- [ ] webnovel-writer 的 Context Contract v2 完整实现
- [ ] 追读力 Reading Power Taxonomy 完整矩阵
- [ ] 40+ 题材专属规则库
- [ ] 多 Agent Teams 并行写作

---

## 参考项目

| 项目 | 借鉴内容 | 优先级 |
|------|---------|--------|
| [AI-Novel-Writing-Assistant](https://github.com/ExplosiveCoderflome/AI-Novel-Writing-Assistant) | 写法引擎资产化、Prompt Registry、Agent Tool System、生产Pipeline、Anti-AI规则 | **高** |
| [chinese-novelist-skill](https://github.com/PenglongHuang/chinese-novelist-skill) | 悬念十三式、章节检查清单、去AI味规则、子Agent并行 | 高 |
| [webnovel-writer](https://github.com/lingfengQAQ/webnovel-writer) | Context Contract、双Agent数据流、Strand Weave、题材模板 | 高 |
| [RWKV-Runner](https://github.com/josStorer/RWKV-Runner) | OpenAI 兼容推理服务 | 中 |
| [AI-Writer](https://github.com/BlinkDL/AI-Writer) | RWKV 中文网文生成 | 中 |

## 版本历史

| 版本 | 日期 | 里程碑 |
|------|------|--------|
| v0.1.0 | 2026-04-15 | Phase 1-4 初始发布 |
| v0.2.0 | 2026-04-15 | 认证+上下文引擎+Checker+写作指南+面板 |
| v0.3.0 | 2026-04-16 | 中文化+移动端+过滤词+书源评分+排行榜+搜索 |
| v0.4.0 | 2026-04-19 | 项目管理页+回收站+向导可编辑+字数目标+单卷重生+关系表+7 bug 修复 |
| v0.5.0 | 2026-04-21 | Prompt Routing + ModelRouter 重构 + LLM 日志 + /api/ask-user + Vector/RAG + 前端 5 新页 |
| v0.6.0 | In progress | 离线反编译 + 三库语义解耦（详见 docs/V06_DESIGN.md）；设定集写入收敛为 Neo4j 真相源 + materialize→PG 投影 |
| v0.7.0 | TBD | 状态机 + Critic + 记忆压缩 + 全局大纲（详见 docs/V07_DESIGN.md） |
| v0.8.0 | TBD | 写法引擎资产化 + 去 AI 味 + Agent Tool Registry v1（详见 docs/V08_DESIGN.md） |
| v0.9.0 | TBD | 设定集一等公民 + 关系图 + 版本 Diff（详见 docs/V09_DESIGN.md） |
| v1.0.0 | TBD | LangGraph + BVSR + 多 Agent 协作 + 生产就绪（详见 docs/V10_DESIGN.md） |
