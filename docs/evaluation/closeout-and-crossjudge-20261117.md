# 「仍未做的」收尾 + 交叉盲评结果(2026-11-17 第三批)

## 先回答"为什么没做"

五项里有**四项根本不是"不能做",是我上一轮自己划的界限**:

| 项 | 上一轮我写的理由 | 实际 |
|---|---|---|
| 交叉盲评实跑 | "等长跑,免得抢网关" | 判断错了:评审走的是**另外两个模型**,与长跑的 `gemini-3.1-pro` 不同,抢的是同一个网关但代价可控。**已跑完** |
| 台账真实 PG 端到端 | 没写理由 | 评估库就在本机、且是一次性库可随便建。**已做** |
| 迁移降级 NULL 加固 | "建议加固,本轮未做" | 纯代码改动。**已做** |
| 手工 DDL 库 upgrade | "需要运维侧的 stamp 策略" | 一半是代码问题(幂等),一半确实是运维策略。**代码这半已做** |
| 降级可观测性 | "(回执里区分不出降级)" | **这条的技术理由成立**:回执是带哈希校验的版本化契约,加字段要同时改生产端与校验端并升版本、波及弧规划。改用等价的低风险方式:**指标计数器** |

## 1. 台账真实 PG 端到端(此前只用方言编译核对过 SQL)

一次性库 `aiwrite_ledger_verify`(用完 `DROP`)。用**产品自己的**读/写器跑完整回路:

```
[写器] ch1 版本行数=0(0 = 无版本行可写,与非阻断设计一致)
[读器] ch2 读到台账条目=[{'item': '校服工钱总额', 'amount': 820.0, 'chapter_idx': 1}]
[判定] 同名目 840 vs 台账 820 → [{'item': '校服工钱总额', 'written': 840, ... 'ledger_amounts': [820.0], 'ledger_chapters': [1]}]
[判定] 同名目 820 vs 台账 820 → []
台账真实 PG 回路通过
```

位置游标、JSON 解析、`chapter_idx` 回填、同名目比对、冲突证据全部在真库上验过。
写器在"该章还没有版本行"时只记 warning 并继续 —— 这正是非阻断设计。

**插曲**:我第一次把 `planned_text` 写成"校服工钱总额 820"(没有元/块后缀),
`amount_spans` 不认 → 直接走"零成本边界"返回 `[]`。这反而验证了成本边界(无金额钉子时连抽取都不发生)。

## 2. 交叉盲评:两评审 28 条逐条一致

`deepseek-chat`(既有)与 `claude-sonnet-4-6`(新,不同家族)各自盲评同一批章:

```
[deepseek-chat]     篇=6 条目=28 交付率=1.0 有据交付率=1.0
[claude-sonnet-4-6] 篇=6 条目=28 交付率=1.0 有据交付率=1.0
共同比对条目=28 一致=28 分歧=0 一致率=1.0
```

**这说明什么**:此前那条"结果交付 220 条里 214 交付"的结论**不是某一个评审模型的偏好** ——
换一个家族的模型给出完全相同的逐条判定,零分歧。

**不说明什么**:样本只有 6 个场景 × 1 个变体,而且评的是"要求的**结果**是否交付",
不是"小说好不好看"。文学质量仍然没有读者侧证据。

**过程中修掉评审器一个真缺陷**:`claude-sonnet-4-6` 会把对白里的引号**原样**写进 JSON 字符串值
(例:`"quote": "...低声道:"这回......真成穷光蛋了。"林叶没说话..."`)→ 未转义引号 → `JSONDecodeError`,
**6 篇里有 4 篇被判失败**。评审工具本身把"换一个模型家族"变成了不可用。
已加 `_escape_inner_quotes`(只在字符串内的 `"` 不是紧跟 `,`/`}`/`]`/`:` 时才补转义;`: ` 必须算结束符,
否则对象键会被误转义),严格解析优先。修完 **6/6 通过**。

## 3. 迁移:两条边界都修掉了

改前:手工 DDL 库 `DuplicateColumnError`;`created_at IS NULL` 的重复行让降级 `UniqueViolationError`。

改后(一次性库 `aiwrite_migtest_verify`,`create_all` 后 `stamp a1001958`):

| 用例 | 改前 | 改后 |
|---|---|---|
| 手工 DDL 库 `upgrade a1001959`(列与唯一键已存在) | `DuplicateColumnError`, exit=1 | **exit=0** |
| 含 3 条 `created_at IS NULL` 同章重复行的 `downgrade -1` | `UniqueViolationError` | **exit=0**,3 行去重为 1 |
| 降级后的旧形态再 `upgrade` | — | **exit=0** |

做法:每一步都**先查现状再动手**(列在就不加、约束在该建才建);降级去重改用窗口函数
`row_number() OVER (PARTITION BY project_id, character_id, chapter_start ORDER BY created_at ASC NULLS LAST, id ASC)`
—— 自连接写法在 `created_at` 为 NULL 时一条都删不掉。

**仍属运维策略的一半**:对"手工 DDL 建好、alembic 认为停在旧 revision"的库,正确动作是
`alembic stamp <该迁移>`,而不是 `upgrade` —— 从 `a1001956` 起 upgrade 会连带跑
`a1001957/a1001958`,而它们建的库表 `create_all` 已经建好了。这条写进报告,不改代码。

## 4. 降级可观测性:不动契约,加计数器

`selection_receipt` 是 `version=1` + 哈希校验的**版本化契约**(`CognitionSelectionVerifier` 校验精确键集),
加字段要同时改生产端与校验端、升版本,并波及弧规划的持久输入 —— 风险与收益不成比例。

改用仓库已有的做法(`SCENE_PLAN_FALLBACK_TOTAL` 先例):新增 Prometheus 计数器
`cognition_retrieval_degraded_total{reason="embedding_unavailable"}`,
在降级路径自增(指标不可用时静默,绝不影响写作)。测试断言降级时 +1。
配合已有的 `cognition_retrieval_degraded … 降级` warning,运维侧"这一轮语义召回没起作用"是可见的。

## 5. 顺带发现的真缺陷:又一种"把错误当正文"

交叉盲评最后一篇的失败原因是上游诊断:

```
[错误] 账号未验证,需要人工完成验证: 创建会话 HTTP 403: {"error":{"details":{"error_code":
"account_needs_verification","error_visibility":"user_facing"},"message":"account_needs_verification",
"type":"permission_error"},"request_id":"req_..."}
```

我的两道守卫**都不认识**它(`looks_like_relay_error: False`)。这不是只有评审工具会踩:
同样的事情发生在章节生成上,这段中文诊断就会被当成正文落库。已补:

- `provider_response_guard._DIAGNOSTIC_PREFIXES`:`account_needs_verification`、`[错误]`
  (中文小说正文不可能以 `[错误]` 开头,按开头判定安全)→ 流开头即拒;
- `chapter_quality_gate` STRONG:`account_needs_verification` / `[错误]` → 落库前判 `draft`;
- 3 项回归。

## 6. 验证

- 全量离线回归 **4865 passed / 762 skipped / 0 failed**(310.94s);
- 100 章长跑仍在跑:**55/100**;
- 密钥未进仓库;所有临时库用完即删(`aiwrite_ledger_verify` / `aiwrite_migtest_verify`),
  除一次性库外未动任何数据库。
