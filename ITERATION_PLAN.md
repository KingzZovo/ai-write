# AI Write 迭代计划

## 当前版本 v0.2.0 (2026-04-15)

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
| 面板 | CheckerDashboard+StrandPanel+WritingGuidePanel+AntiAIPanel | ✅ |

### 技术统计

```
18 commits | 99 源码文件 | 23,655 行代码 | 79 API 端点 | 13 前端面板
```

---

## Iteration 1: E2E 验证与 Bug 修复

**目标：** 接入真实 LLM API，跑通全流程，修复运行时 bug。

- [ ] 在 Settings 页面配置至少一个 LLM 端点
- [ ] 完整流程：创建项目 → 生成全书大纲 → 分卷 → 章节 → 正文
- [ ] 验证 SSE 流式生成在浏览器中正常工作
- [ ] 测试书源导入 + 爬虫抓取
- [ ] 测试文件上传 → 清洗 → 风格提取完整链路
- [ ] 验证 6 Checker 带真实 ContextPack 的运行结果
- [ ] 修复所有运行时发现的 bug

**成功标准：** 从零生成一本 5 章短篇小说，全程无手动干预。

## Iteration 2: 测试套件与 CI

**目标：** 消除 594 个测试 gap，建立自动化质量保障。

- [ ] pytest + pytest-asyncio 后端测试框架
- [ ] 核心服务单元测试（text_pipeline, book_source_engine, checkers, context_pack）
- [ ] API 集成测试（使用测试数据库）
- [ ] 前端组件测试（Jest + React Testing Library）
- [ ] GitHub Actions CI（lint + test on push）
- [ ] pre-commit hooks（ruff lint + type check）

## Iteration 3: 前端深度交互

**目标：** ProseMirror 深度集成，提升写作体验。

- [ ] ProseMirror AI 内容标记（不同背景色）
- [ ] 选中文本悬浮菜单（缩写/扩写/重构/续写）→ 连接 RewriteMenu 组件
- [ ] 流式生成光标追踪
- [ ] 大纲编辑器可视化（树状拖拽排序）
- [ ] 中文全面本地化
- [ ] 暗色模式
- [ ] 移动端响应式

## Iteration 4: 性能与健壮性

**目标：** 100+ 章节长篇可靠运行。

- [ ] 长篇压测：100 章，验证记忆召回准确度
- [ ] Neo4j 1000+ 实体查询性能
- [ ] Qdrant 10000+ 向量检索延迟
- [ ] WebSocket 通知（增量同步/后台生成完成）
- [ ] 批量生成真实 SSE 进度（逐章推送）
- [ ] 数据库索引优化 + 连接池调优

## Iteration 5: LoRA 训练集成

**目标：** 从 Web UI 完成微调全流程。

- [ ] LoRA 管理页面（数据集导出向导 + 训练配置）
- [ ] 训练任务监控（连接远程 GPU WebSocket）
- [ ] Adapter 浏览器（列表/预览/激活/A-B对比）
- [ ] 多风格 LoRA 运行时切换
- [ ] RWKV-7 模型集成（通过 RWKV-Runner OpenAI 兼容端点）
- [ ] vLLM/Ollama 自动检测 + 健康监控

## Iteration 6: 导出与发布

**目标：** 作品输出到各平台。

- [ ] 导出格式：TXT / EPUB / PDF / DOCX
- [ ] 元数据编辑（标题/作者/简介/封面）
- [ ] Legado 兼容格式导出（直接在阅读 app 中阅读）
- [ ] 字数统计趋势图
- [ ] 质量评分历史曲线
- [ ] 生成成本追踪（按项目）

## Iteration 7: 高级写作技术

**目标：** 深度集成研究级写作技术。

- [ ] ConStory-Checker：跨章节一致性深度检查
- [ ] BVSR 抽卡机制：生成多版本段落供选择
- [ ] SWAG 动作引导：限制模型的动作空间防止失控
- [ ] 多 Agent Teams 并行写作（参考 chinese-novelist-skill）
- [ ] webnovel-writer 的 Context Contract v2 完整实现
- [ ] 追读力 Reading Power Taxonomy 完整矩阵
- [ ] 40+ 题材专属规则库（参考 webnovel-writer genres/）

---

## 版本历史

| 版本 | 日期 | 里程碑 |
|------|------|--------|
| v0.1.0 | 2026-04-15 | Phase 1-4 初始发布 |
| v0.2.0 | 2026-04-15 | 认证 + 上下文引擎 + Checker + 写作指南 + 面板 |
| v0.3.0 | TBD | Iteration 1: E2E 验证 |
| v1.0.0 | TBD | Iteration 3-4: 生产就绪 |

## 参考项目

| 项目 | 借鉴内容 |
|------|---------|
| [chinese-novelist-skill](https://github.com/PenglongHuang/chinese-novelist-skill) | 悬念十三式、章节检查清单、去AI味规则、子Agent并行 |
| [webnovel-writer](https://github.com/lingfengQAQ/webnovel-writer) | Context Contract、双Agent数据流、Strand Weave、40+题材模板 |
| [RWKV-Runner](https://github.com/josStorer/RWKV-Runner) | OpenAI 兼容推理服务、模型管理 |
| [AI-Writer](https://github.com/BlinkDL/AI-Writer) | RWKV 中文网文生成 |
