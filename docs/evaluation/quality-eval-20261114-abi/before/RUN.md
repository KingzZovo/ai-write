# 质量验收运行记录(机器生成)

本文件只陈述本次实际执行的事实。文学判断见同目录 `REVIEW.md`。

- 样本集:sample_set_v1(6 个场景,3 个题材)
- 网关:http://127.0.0.1:8317/v1
- 模型:gemini-3.1-pro
- 生产契约:{"narrative_contract_version": "world_logic_contract_v4_15_scene_planning", "blueprint_version": "v4.14", "writer_contract_sha256": "6cc3311ac6f3baf0", "scene_writer_system_sha256": "806255474cc40ff3"}
- 调用:12 次,成功 12,失败 0
- 正文总字数:17348
- token 用量:输入 6647,输出 4334
- 累计耗时:135.717 秒

## 确定性检测计数

| 类别 | 命中数 | 其中 hard |
|---|---|---|
| pov_breach | 1 | 0 |
| instruction_leak | 0 | 0 |
| rule_recitation | 2 | 0 |
| metaphor_stack | 1 | 0 |
| outcome_unfulfilled | 13 | 13 |
| fact_violation | 25 | 15 |

## 未兑现的合同条目

| 条目 | 出现次数 |
|---|---|
| must_keep:deadline | 1 |
| must_keep:debt_total | 1 |
| must_keep:father_gone | 1 |
| must_keep:no_neighbor_loan | 1 |
| must_keep:price | 1 |
| must_keep:sister_ignorant | 2 |
| must_keep:ten_breaths | 1 |
| must_keep:tonight_seal | 1 |
| must_keep:two_days | 1 |
| must_keep:wet_shoe | 2 |
| required_outcomes:brother_boards | 2 |
| required_outcomes:concrete_action | 1 |
| required_outcomes:handles_waiter | 1 |
| required_outcomes:judgment_changes | 2 |
| required_outcomes:next_morning | 1 |
| required_outcomes:plan_made | 1 |
| required_outcomes:price_named | 1 |
| required_outcomes:reads_letter | 1 |
| required_outcomes:shortfall_declared | 1 |
| required_outcomes:sister_stays | 1 |

## 按变体

| 变体 | 场景数 | 字数 | hard 合计 |
|---|---|---|---|
| chapter_single_shot | 6 | 8589 | 11 |
| scene_writer | 6 | 8759 | 17 |

## 逐次调用

| 产出 | 模型返回 | finish_reason | 字数 | 耗时(s) | 输出 token | 正文 sha256 |
|---|---|---|---|---|---|---|
| real_ferry-scene_writer | gemini-3.1-pro | stop | 1924 | 10.628 | 481 | `cfb5114485aca21a` |
| real_ferry-chapter_single_shot | gemini-3.1-pro | stop | 1688 | 17.16 | 422 | `03760749ec098348` |
| real_stall-scene_writer | gemini-3.1-pro | stop | 2156 | 11.283 | 539 | `0d993d8e0c77b14c` |
| real_stall-chapter_single_shot | gemini-3.1-pro | stop | 1518 | 8.52 | 379 | `53262aa1c33663f8` |
| fantasy_bell-scene_writer | gemini-3.1-pro | stop | 1156 | 7.281 | 289 | `da841b7df1a84198` |
| fantasy_bell-chapter_single_shot | gemini-3.1-pro | stop | 1333 | 19.431 | 333 | `62b35739e52e01e7` |
| fantasy_herb-scene_writer | gemini-3.1-pro | stop | 1411 | 7.671 | 352 | `dbe529615a0f0df1` |
| fantasy_herb-chapter_single_shot | gemini-3.1-pro | stop | 1482 | 24.303 | 370 | `0773d6bfbfdcc782` |
| mystery_bridge-scene_writer | gemini-3.1-pro | stop | 1072 | 6.967 | 268 | `eec4fe4eeec3216a` |
| mystery_bridge-chapter_single_shot | gemini-3.1-pro | stop | 1119 | 6.142 | 279 | `99b5172d026b08a5` |
| mystery_lodger-scene_writer | gemini-3.1-pro | stop | 1040 | 7.353 | 260 | `7d79f5e9a0edda6f` |
| mystery_lodger-chapter_single_shot | gemini-3.1-pro | stop | 1449 | 8.978 | 362 | `93eb61778c21b50c` |
