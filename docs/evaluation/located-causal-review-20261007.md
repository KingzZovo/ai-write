# 有原文定位的事实问题不被高分覆盖（2026-10-07）

## 实测反例与变更

真实运行稿“六本台账”被评为9.8，评审同时指出交钥匙和离场重复，给出逐字匹配的 quote，但 severity=low。旧 should_revise 只比较总分，ChapterAcceptance只拦high/critical事实问题，因此已报告的问题被跳过。

新增统一谓词 has_located_causal_issue：属于既有因果/连续性类型，具有非空实际诊断，quote非空且quote_verified必须是布尔True。自动修订在低分或该条件成立时进入既有有界修订流程；验收遇到该条件也返回待审核，不由高分覆盖。修订预算和作者关闭自动修订的选择不变。

原文定位只证明引文存在，不证明模型诊断正确。这是进入修订/审核的条件，不是文学质量的自动证明。纯审美建议、无定位标签、空诊断及模型自己编造的quote不因此触发付费改写。原本high/critical但无定位的事实问题仍阻止验收；解析失败仍保留正文，不用伪零分驱动改写。

## 验证

- 直接读取上一轮实际保存正文和9.8分评审：[样本](runtime-chain-20261006/read-session/evaluations.json)。确认引文在正文内后，should_revise=True、evaluation_passes=False，当前正文状态应为needs_review。
- 初轮针对性52 passed，含现有自动修订、回滚及验收用例。随后补充空白包围标签和真实评审解析器对伪造quote_verified的防护测试。
- 全量后端1816 passed，4项既有类别warnings，262.22秒；日志 /tmp/aiwrite-located-review-full.log。
- 本地按shadow-mind无可信只读隔离退路复核：SSE和Worker均调用同一should_revise；最终状态通过同一ChapterAcceptance，不另设平行评分。核对类型、定位标志、空内容和解析失败边界。

## 剩余范围

本批没有调用模型，没有重新生成正文，没有更新常驻隔离镜像或生产。对于模型给10分且完全漏报的鞋子变化、动作路径和视角问题，本条件不会凭空发现缺陷；编辑评审的取证质量仍需修复。纯语言形式触发的机械改写也尚未整体治理。生产未上线，文学验收未完成。
