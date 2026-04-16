# AI Write 迭代计划

## 当前版本 v0.3.0 (2026-04-16)

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

### 技术统计

```
42 commits | 70+ 后端文件 | 30+ 前端文件 | ~25,000 行代码 | 85+ API 端点
```

---

## Iteration 1: E2E 验证与 Bug 修复

**目标：** 接入真实 LLM API，跑通全流程，修复运行时 bug。

- [ ] 在 Settings 页面配置 LLM 端点
- [ ] 完整流程：创建项目 → 生成全书大纲 → 分卷 → 章节 → 正文
- [ ] 验证 SSE 流式生成在浏览器中正常工作
- [ ] 测试书源导入 + 爬虫抓取
- [ ] 测试文件上传 → 清洗 → 风格提取完整链路
- [ ] 验证 6 Checker 带真实 ContextPack 的运行结果
- [ ] PC 端 DesktopWorkspace 稳定性修复

**成功标准：** 从零生成一本 5 章短篇小说，全程无手动干预。

## Iteration 2: 写法引擎资产化

**目标：** 将写法从静态模板升级为可编辑、可绑定、可编译的持久资产。

**参考：** AI-Novel-Writing-Assistant 的 StyleEngine（10 个服务模块）

- [ ] StyleProfile 持久化（DB 存储，前端 CRUD）
- [ ] StyleCompiler：将写法规则编译为 prompt 指令（带权重：≥0.85 必须保持 / ≥0.65 优先保持）
- [ ] StyleBinding：写法绑定到整本书 / 单个章节 / 单次生成（优先级层级）
- [ ] StyleDetection：从现有文本提取写法特征 → 保存为 Profile
- [ ] StyleRuntime：生成时动态解析当前激活的写法规则
- [ ] Anti-AI 规则升级：增加 `autoRewrite` 和 `detectPatterns` 字段
- [ ] 写法试写功能：选定写法 → 试写一段 → 评估匹配度
- [ ] 前端写法管理页面

## Iteration 3: Prompt Registry 统一管理

**目标：** 所有产品级 prompt 统一注册、版本化、可追溯。

**参考：** AI-Novel-Writing-Assistant 的 prompting/ 系统

- [ ] `PromptAsset` 数据结构（id/version/taskType/mode/contextPolicy/outputSchema）
- [ ] `PromptRegistry` 注册表
- [ ] 统一 Runner（`runStructuredPrompt` / `runTextPrompt` / `streamTextPrompt`）
- [ ] 将所有散落在 agent/service 中的 prompt 迁入 registry
- [ ] 前端 prompt 管理页面（查看/编辑/版本对比）
- [ ] Prompt 效果追踪（每个 prompt 的成功率/评分）

## Iteration 4: 生产 Pipeline 增强

**目标：** 整本书生产流水线，支持断点恢复、快照回滚、状态机追踪。

**参考：** AI-Novel-Writing-Assistant 的 NovelPipelineService

- [ ] Pipeline 状态机（planning → generating → reviewing → polishing → completed）
- [ ] 快照系统：生产前自动快照，失败可回滚
- [ ] 章节审校服务（生成 → 审校 → 修复 → 确认，最多 3 轮）
- [ ] 整本批量执行 + 断点恢复
- [ ] 生产状态面板（前端实时显示当前阶段/进度/失败原因）
- [ ] 导出功能：TXT / EPUB / PDF / DOCX

## Iteration 5: 测试套件与 CI

**目标：** 消除测试 gap，建立自动化质量保障。

- [ ] pytest + pytest-asyncio 后端测试框架
- [ ] 核心服务单元测试
- [ ] API 集成测试
- [ ] 前端组件测试
- [ ] GitHub Actions CI
- [ ] pre-commit hooks

## Iteration 6: LoRA 训练集成

**目标：** 从 Web UI 完成微调全流程。

- [ ] LoRA 管理页面
- [ ] 训练任务监控
- [ ] Adapter 浏览器（列表/预览/激活/A-B对比）
- [ ] 多风格 LoRA 运行时切换
- [ ] RWKV-7 模型集成
- [ ] vLLM/Ollama 自动检测

## Iteration 7: 高级写作技术

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
| v0.4.0 | TBD | Iteration 1: E2E 验证 |
| v0.5.0 | TBD | Iteration 2: 写法引擎资产化 |
| v1.0.0 | TBD | Iteration 4: 生产就绪 |
