# 本机 CPA Gemini 上游诊断

2026-10-06。用户指出Gemini经本机CPA调用后，检查实际进程、监听端口及脱敏配置。没有改动CPA配置、账户或其他服务。

## 已核实的链路

AI-WRITE T1/T2 → 本机51.83.5.205:8317的CLIProxyAPI → OpenAI兼容上游。进程位于 `/root/migration-deploy/cliproxyapi/cli-proxy-api`，版本文件8.0.16，配置 `config.yaml`。CPA目录策略为round-robin，Gemini有三条同名模型上游：geminiweb优先级999，gemini2/gemini3优先级0；均指向外部Cloudflare Worker。不能仅凭round-robin推断具体一次请求的路由，优先级与可用状态同样影响选择。未取得此前请求的逐次路由证据。

本机未检索到该错误提示或三个Worker源码；上游错误文本关于IP/GEMINI_BL的建议不是根因诊断。这里不公开访问密钥，也不把Worker地址当作官方Google接口。

## 逐上游直测

使用前一轮姐弟场景的相同system/user，gemini-3.1-pro、temperature0.3、max_tokens3200、stream=True，分别直接访问三个已配置上游。每个上游先baseline后candidate，三个上游并行；SDK不重试，每次总限120秒。这是隔离服务诊断，不改应用的Registry路由。与前轮调用时刻、流式usage参数不同，不能用六次请求计算稳定成功率或归因某个参数。

| 上游 | baseline | candidate |
|---|---|---|
| geminiweb | 正文1162字符，11.167秒 | 正文1732字符，11.463秒 |
| gemini2 | 正文1034字符，9.793秒 | 正文1615字符，47.223秒 |
| gemini3 | 252字符上游错误，0.441秒 | 同样错误，0.251秒 |

两条线路支持本轮长提示词，否定“长提示词必然失败”的推断；gemini3两次均返回和此前完全相同的错误文本。不能据此证明前轮所有失败都经过gemini3，也不能证明Gemini文学能力已合格。正文只是成功返回，仍需要编辑式验收；例如geminiweb baseline的谈钱过程仍有债务方向含混及新增过去事件。

## 应用边界修复与真实复验

新增provider_response_guard.py，只识别回答开头已观察到的Gemini服务错误签名，不按文学偏好判错、不扫描小说对白中的同名字符串。非流式及内部组装流在返回前拒绝；外部流在前缀尚未判明时缓冲少量chunk，包括开头role/usage，防止错误字符抢先外泄。普通正文一旦与签名前缀不同立即放行，不缓冲全章。

错误转换为LLMRelayResponseError，在既有有限重试/降级预算内处理，不额外放大次数；已经向下游交付正文后不重放。关闭、失败和取消释放底层stream；不把完整代理诊断复制进异常，避免日志泄漏其潜在敏感内容。

直接对gemini3使用实际OpenAIProvider复验：组装模式和流式模式均拒绝上游错误，exposed_chars均为0，见guard-verification.json。没有宣称上游账号/Worker本身已修好，应用改动尚未部署。

## 证据限制与下一步

前一轮editorial-prose对照经过CPA，本轮发现其模型名称背后存在多个上游，之前文学观察仍是真实原稿证据，但不能把条件差异单独归因于Prompt。后续质量对照需要固定上游、记录实际来源并重复采样，再回到应用主链验证。

先完成完整回归，再针对固定健康上游继续人物与情节规划优化。服务失败和文学质量失败分别记录；不能让一次HTTP200或模型总分宣告章节完成。

验证：38项针对性测试、完整后端1516 passed（5条既有warnings），日志 `/tmp/aiwrite-relay-guard-full-suite.log`。本地正确性/架构复核后提交，未做生产部署。
