# 质量验收运行记录(机器生成)

本文件只陈述本次实际执行的事实。文学判断见同目录 `REVIEW.md`。

- 样本集:sample_set_v1(6 个场景,3 个题材)
- 网关:http://127.0.0.1:8317/v1
- 模型:gemini-3.1-pro
- 生产契约:{"narrative_contract_version": "world_logic_contract_v4_15_scene_planning", "blueprint_version": "v4.14", "writer_contract_sha256": "6cc3311ac6f3baf0", "scene_writer_system_sha256": "806255474cc40ff3"}
- 调用:36 次,成功 36,失败 0
- 正文总字数:49563
- token 用量:输入 19941,输出 12376
- 累计耗时:407.453 秒

## 确定性检测计数

| 类别 | 命中数 | 其中 hard |
|---|---|---|
| pov_breach | 4 | 0 |
| instruction_leak | 0 | 0 |
| rule_recitation | 14 | 0 |
| metaphor_stack | 1 | 0 |
| outcome_unfulfilled | 30 | 30 |
| fact_violation | 69 | 33 |

## 未兑现的合同条目

| 条目 | 出现次数 |
|---|---|
| forbidden:forced_decision | 1 |
| must_keep:bell_destroyed | 1 |
| must_keep:clear_out | 1 |
| must_keep:debt_total | 2 |
| must_keep:father_gone | 4 |
| must_keep:fourteen_rooms | 2 |
| must_keep:no_neighbor_loan | 3 |
| must_keep:one_person_wide | 2 |
| must_keep:rain | 2 |
| must_keep:rule | 2 |
| must_keep:sister_ignorant | 3 |
| must_keep:ten_breaths | 1 |
| must_keep:unsealed_letter | 1 |
| must_keep:wet_shoe | 5 |
| required_outcomes:acts | 2 |
| required_outcomes:both_ashore | 1 |
| required_outcomes:brother_boards | 1 |
| required_outcomes:concrete_action | 4 |
| required_outcomes:consequence | 1 |
| required_outcomes:judgment_changes | 6 |
| required_outcomes:makes_choice | 3 |
| required_outcomes:next_morning | 1 |
| required_outcomes:plan_made | 2 |
| required_outcomes:reads_letter | 1 |
| required_outcomes:recognizes_conflict | 4 |
| required_outcomes:shortfall_declared | 1 |

## 按变体

| 变体 | 场景数 | 字数 | hard 合计 |
|---|---|---|---|
| chapter_single_shot | 12 | 16275 | 17 |
| scene_writer | 12 | 16551 | 22 |
| unknown | 12 | 16737 | 24 |

## 逐次调用

| 产出 | 模型返回 | finish_reason | 字数 | 耗时(s) | 输出 token | 正文 sha256 |
|---|---|---|---|---|---|---|
| real_ferry-scene_writer | gemini-3.1-pro | stop | 1646 | 17.654 | 411 | `31594bf388ac5755` |
| real_ferry-scene_writer | gemini-3.1-pro | stop | 1725 | 9.584 | 431 | `d0935321e9922fcd` |
| real_ferry-scene_writer | gemini-3.1-pro | stop | 1903 | 10.126 | 475 | `02d231ce638f7d2d` |
| real_ferry-chapter_single_shot | gemini-3.1-pro | stop | 1575 | 8.788 | 393 | `d5c757701d5868d3` |
| real_ferry-chapter_single_shot | gemini-3.1-pro | stop | 1599 | 25.268 | 399 | `17a88c833a905aa2` |
| real_ferry-chapter_single_shot | gemini-3.1-pro | stop | 1382 | 14.607 | 345 | `e632595606d59672` |
| real_stall-scene_writer | gemini-3.1-pro | stop | 2039 | 10.866 | 509 | `663673adab540c6a` |
| real_stall-scene_writer | gemini-3.1-pro | stop | 1238 | 7.39 | 309 | `9ee0e1988f11624b` |
| real_stall-scene_writer | gemini-3.1-pro | stop | 1804 | 16.912 | 451 | `05aae113d781c7a7` |
| real_stall-chapter_single_shot | gemini-3.1-pro | stop | 1401 | 8.076 | 350 | `83956feb47cabd25` |
| real_stall-chapter_single_shot | gemini-3.1-pro | stop | 1774 | 9.525 | 443 | `39ed11b109119c76` |
| real_stall-chapter_single_shot | gemini-3.1-pro | stop | 982 | 8.693 | 245 | `d92d2e9d273bb89c` |
| fantasy_bell-scene_writer | gemini-3.1-pro | stop | 1153 | 6.979 | 288 | `807c080696d0cb48` |
| fantasy_bell-scene_writer | gemini-3.1-pro | stop | 784 | 5.965 | 196 | `8450a9d2d647e33f` |
| fantasy_bell-scene_writer | gemini-3.1-pro | stop | 1144 | 7.78 | 286 | `31b629853e65fab3` |
| fantasy_bell-chapter_single_shot | gemini-3.1-pro | stop | 1492 | 15.908 | 373 | `5d77ec6b4dfe3513` |
| fantasy_bell-chapter_single_shot | gemini-3.1-pro | stop | 944 | 17.872 | 236 | `42fa5d9d359aba98` |
| fantasy_bell-chapter_single_shot | gemini-3.1-pro | stop | 1369 | 19.81 | 342 | `f6c75b69a620ce31` |
| fantasy_herb-scene_writer | gemini-3.1-pro | stop | 1045 | 7.195 | 261 | `6e1339c81886a3ec` |
| fantasy_herb-scene_writer | gemini-3.1-pro | stop | 1095 | 7.864 | 273 | `6cb7581f2c45addc` |
| fantasy_herb-scene_writer | gemini-3.1-pro | stop | 795 | 5.93 | 198 | `cc641a41464d8313` |
| fantasy_herb-chapter_single_shot | gemini-3.1-pro | stop | 1646 | 6.467 | 411 | `7625504ccff70276` |
| fantasy_herb-chapter_single_shot | gemini-3.1-pro | stop | 1304 | 7.752 | 326 | `9a04cee72c36a0e0` |
| fantasy_herb-chapter_single_shot | gemini-3.1-pro | stop | 1567 | 18.399 | 391 | `71ad3faa5c97d7a7` |
| mystery_bridge-scene_writer | gemini-3.1-pro | stop | 862 | 5.867 | 215 | `52f1659998c8bed7` |
| mystery_bridge-scene_writer | gemini-3.1-pro | stop | 1949 | 21.766 | 487 | `4a957be9ba40664e` |
| mystery_bridge-scene_writer | gemini-3.1-pro | stop | 1503 | 20.834 | 375 | `b4f4965cd6022396` |
| mystery_bridge-chapter_single_shot | gemini-3.1-pro | stop | 1205 | 7.88 | 301 | `0f6759bfd23bc3e2` |
| mystery_bridge-chapter_single_shot | gemini-3.1-pro | stop | 1580 | 9.49 | 395 | `687854b0eea2cf28` |
| mystery_bridge-chapter_single_shot | gemini-3.1-pro | stop | 967 | 6.653 | 241 | `b7560bbaa7b4ce1a` |
| mystery_lodger-scene_writer | gemini-3.1-pro | stop | 1274 | 18.178 | 318 | `7150a2fae53d0752` |
| mystery_lodger-scene_writer | gemini-3.1-pro | stop | 1241 | 7.923 | 310 | `5ba7f5278639c6b2` |
| mystery_lodger-scene_writer | gemini-3.1-pro | stop | 1370 | 8.177 | 342 | `402d510439b851df` |
| mystery_lodger-chapter_single_shot | gemini-3.1-pro | stop | 1399 | 8.582 | 349 | `75f9742caef18144` |
| mystery_lodger-chapter_single_shot | gemini-3.1-pro | stop | 1544 | 8.458 | 386 | `911309a1647c472c` |
| mystery_lodger-chapter_single_shot | gemini-3.1-pro | stop | 1263 | 8.235 | 315 | `380e70744745b363` |
