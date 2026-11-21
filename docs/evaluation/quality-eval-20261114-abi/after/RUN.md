# 质量验收运行记录(机器生成)

本文件只陈述本次实际执行的事实。文学判断见同目录 `REVIEW.md`。

- 样本集:sample_set_v1(6 个场景,3 个题材)
- 网关:http://127.0.0.1:8317/v1
- 模型:gemini-3.1-pro
- 生产契约:{"narrative_contract_version": "world_logic_contract_v4_15_scene_planning", "blueprint_version": "v4.14", "writer_contract_sha256": "6cc3311ac6f3baf0", "scene_writer_system_sha256": "806255474cc40ff3"}
- 调用:12 次,成功 11,失败 1
- 正文总字数:14998
- token 用量:输入 6671,输出 3743
- 累计耗时:118.231 秒

## 确定性检测计数

| 类别 | 命中数 | 其中 hard |
|---|---|---|
| pov_breach | 0 | 0 |
| instruction_leak | 0 | 0 |
| rule_recitation | 4 | 0 |
| metaphor_stack | 2 | 0 |
| outcome_unfulfilled | 9 | 9 |
| fact_violation | 16 | 8 |

## 未兑现的合同条目

| 条目 | 出现次数 |
|---|---|
| must_keep:bell_destroyed | 1 |
| must_keep:debt_total | 1 |
| must_keep:father_gone | 1 |
| must_keep:fourteen_rooms | 1 |
| must_keep:rain | 1 |
| must_keep:three_years | 1 |
| must_keep:wet_shoe | 1 |
| required_outcomes:acts | 1 |
| required_outcomes:concrete_action | 1 |
| required_outcomes:consequence | 1 |
| required_outcomes:handles_waiter | 1 |
| required_outcomes:judgment_changes | 1 |
| required_outcomes:recognizes_conflict | 1 |
| required_outcomes:temporary_deal | 1 |

## 按变体

| 变体 | 场景数 | 字数 | hard 合计 |
|---|---|---|---|
| chapter_single_shot | 6 | 8707 | 7 |
| scene_writer | 5 | 6291 | 10 |

## 逐次调用

| 产出 | 模型返回 | finish_reason | 字数 | 耗时(s) | 输出 token | 正文 sha256 |
|---|---|---|---|---|---|---|
| real_ferry-scene_writer | - | - | - | - | - | 失败:None |
| real_ferry-chapter_single_shot | gemini-3.1-pro | stop | 2295 | 19.43 | 573 | `0d026c30e7dc8927` |
| real_stall-scene_writer | gemini-3.1-pro | stop | 1847 | 16.07 | 461 | `c18696b4abe6bcb2` |
| real_stall-chapter_single_shot | gemini-3.1-pro | stop | 1688 | 10.331 | 422 | `f6014fa826b5e126` |
| fantasy_bell-scene_writer | gemini-3.1-pro | stop | 927 | 6.307 | 231 | `e6cc742ea05da92d` |
| fantasy_bell-chapter_single_shot | gemini-3.1-pro | stop | 1231 | 8.216 | 307 | `2af674ff0d7fbc65` |
| fantasy_herb-scene_writer | gemini-3.1-pro | stop | 1015 | 6.742 | 253 | `01df42b536485c7e` |
| fantasy_herb-chapter_single_shot | gemini-3.1-pro | stop | 1041 | 7.837 | 260 | `7f4eaf595f95dff3` |
| mystery_bridge-scene_writer | gemini-3.1-pro | stop | 1111 | 10.114 | 277 | `e654a084a08198b3` |
| mystery_bridge-chapter_single_shot | gemini-3.1-pro | stop | 1071 | 7.618 | 267 | `f70e3a965e3704f8` |
| mystery_lodger-scene_writer | gemini-3.1-pro | stop | 1391 | 9.213 | 347 | `e6f838e6193f70ac` |
| mystery_lodger-chapter_single_shot | gemini-3.1-pro | stop | 1381 | 16.353 | 345 | `e3d226e033eed9d7` |

## 失败调用

- real_ferry-scene_writer:None
