# 评审面板接入真实异步接口

## 已确认的问题

桌面和移动端共用 EvaluationPanel。原面板 GET /api/chapters/{id}/evaluate 没有对应 GET handler；同路径 POST 存在，但面板未传 EvaluateRequest 请求体，且仍期待 overallScore/scores 驼峰结构，后端实际返回 overall/plot_coherence 等字段。未知或缺失 severity 又会直接访问 undefined.color。不是给后端添加另一套无关评审逻辑即可解决的问题。

## 改动

面板接入 POST /api/evaluate/start 和 GET /api/evaluate/tasks/{task_id}。新增只读 GET /api/evaluate/chapters/{chapter_id}/latest：分别有界读取最近 ChapterEvaluation 与 EvaluateTask，按创建顺序返回最新记录；进行中的任务继续轮询，首次无记录返回 null，不触发模型。未知章节 404、非法 UUID 422。保存的 system 错误反馈标为不可信，不伪装正常零分。

前端按实际蛇形字段映射，检查分数范围与 issues 数组；未知 severity 降为可展示的普通提示。故障结果显示安全中文原因，隐藏占位分数及原始服务异常。明确标注为已保存正文的历史反馈，改稿后须重新评审，不把无来源版本绑定的历史高分当成当前稿验收。

按章 keyed 子组件隔离状态，卸载/切章取消网络与后续计时器；结果需匹配当前章节，轮询还需匹配请求任务。网络失败后先刷新服务端任务状态，再允许重新发起，避免响应丢失就直接重试提交。页面提供刷新，去掉原来一直禁用且无实现的“重新生成问题段落”按钮；定点改写仍由既有正文改写工作流承担。

## 验证与复核

后端 7 项针对性通过：新读接口综合覆盖空记录、保存评分/异步任务新旧顺序、pending 恢复、已完成错误原因、404/422、读操作不入队，另回归既有 SSE 保存与验收。前端新增 6 项组件测试覆盖正确字段、未知 severity、故障隐藏分数、创建一次并轮询、切章迟到结果、错误归属与网络刷新；前端全量 107 项、TypeScript、生产构建通过。

按 shadow-mind 的无可信只读隔离回退要求，冻结代码后本地分别核对接口/数据正确性及界面完成度：新读接口不启动模型；异步结果和已有评分都能读取；历史标记不暗示当前稿已获验收；AbortController 与 key 生命周期阻止跨章覆盖。复核补充了轮询任务 ID 核验和网络故障后先刷新保护。后端全量 1995 passed、4 条既有 warnings（317.35 秒）。

真实 Chromium 使用隔离账号与实际项目打开 /workspace，展开卷册、选择“早班”、进入审查中心，0 个 pageerror。此时后端仍为旧镜像，新 latest 接口未部署，面板正确显示获取失败并允许刷新；尚不能据此声称真实历史读取及点击评审验收成功。私有脚本、DOM 文本、截图与日志：/root/.aiwrite-evaluation/evaluation-panel-browser-20261007/。

生产未更新。此次不是整个浏览器工作流或全书文学验收；其他编辑、生成、导出及历史来源重建仍按总计划推进。


## 更新后的真实浏览器验收

隔离四服务已部署 610d563/a1001930，233 个 Python 源文件逐项一致。前端生产构建在本机 127.0.0.1:53000 运行，API rewrite 指向既有隔离 API 隧道 58000；没有替换生产前端。私有部署记录：/root/.aiwrite-evaluation/evaluation-panel-runtime-20261007/。

Chromium 真实登录并进入隔离书籍，展开卷册，选中“早班”，打开审查中心：读到先前历史评分，再点击“运行评估”，经过实际 POST/轮询显示新评分。数据库确认仅新增一个 frontend_quality_panel 任务，24.778 秒完成；真实日志为 Gemini error→Luna ok，9.4 分/零问题。页面 pageerror=0，两章仍 needs_review、正文 SHA256 不变。此评分只证明界面与服务链路工作，不改变此前对成稿的编辑式意见。

初次截图中文方框，fc-list :lang=zh 为空，环境只有 DejaVu 字体。为本机 Chromium 安装 fonts-noto-cjk 后只读重新打开页面核对；没有为截图再次生成或评审。测试浏览器字体依赖不作为产品源码改动。

浏览器工具安装在私有 /root/.aiwrite-evaluation/browser-tools/，没有加入项目依赖。前端临时服务保留用于后续浏览器验收；本批未覆盖移动端完整操作、全书生成、导出和其他工具面板。
