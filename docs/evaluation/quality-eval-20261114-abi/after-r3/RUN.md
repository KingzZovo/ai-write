# 质量验收运行记录(机器生成)

本文件只陈述本次实际执行的事实。文学判断见同目录 `REVIEW.md`。

- 样本集:sample_set_v1(6 个场景,3 个题材)
- 网关:http://127.0.0.1:8317/v1
- 模型:gemini-3.1-pro
- 生产契约:{"narrative_contract_version": "world_logic_contract_v4_15_scene_planning", "blueprint_version": "v4.14", "writer_contract_sha256": "6cc3311ac6f3baf0", "scene_writer_system_sha256": "806255474cc40ff3"}
- 调用:36 次,成功 36,失败 0
- 正文总字数:47656
- token 用量:输入 21624,输出 11898
- 累计耗时:373.165 秒

## 确定性检测计数

| 类别 | 命中数 | 其中 hard |
|---|---|---|
| pov_breach | 4 | 0 |
| instruction_leak | 0 | 0 |
| rule_recitation | 10 | 0 |
| metaphor_stack | 2 | 0 |
| outcome_unfulfilled | 31 | 31 |
| fact_violation | 65 | 29 |

## 未兑现的合同条目

| 条目 | 出现次数 |
|---|---|
| forbidden:both_saved | 1 |
| must_keep:bell_destroyed | 2 |
| must_keep:clear_out | 1 |
| must_keep:deadline | 1 |
| must_keep:father_gone | 4 |
| must_keep:fourteen_rooms | 1 |
| must_keep:master_away | 1 |
| must_keep:no_neighbor_loan | 4 |
| must_keep:rain | 1 |
| must_keep:rule | 2 |
| must_keep:sister_ignorant | 4 |
| must_keep:tonight_seal | 1 |
| must_keep:unsealed_letter | 1 |
| must_keep:wet_shoe | 2 |
| required_outcomes:acts | 2 |
| required_outcomes:both_ashore | 1 |
| required_outcomes:brother_boards | 1 |
| required_outcomes:concrete_action | 2 |
| required_outcomes:decision | 1 |
| required_outcomes:handles_waiter | 1 |
| required_outcomes:judgment_changes | 5 |
| required_outcomes:makes_choice | 2 |
| required_outcomes:next_morning | 1 |
| required_outcomes:plan_made | 2 |
| required_outcomes:reads_letter | 2 |
| required_outcomes:recognizes_conflict | 4 |
| required_outcomes:shortfall_declared | 2 |
| required_outcomes:temporary_deal | 1 |

## 按变体

| 变体 | 场景数 | 字数 | hard 合计 |
|---|---|---|---|
| chapter_single_shot | 12 | 17530 | 18 |
| scene_writer | 12 | 15348 | 15 |
| unknown | 12 | 14778 | 27 |

## 逐次调用

| 产出 | 模型返回 | finish_reason | 字数 | 耗时(s) | 输出 token | 正文 sha256 |
|---|---|---|---|---|---|---|
| real_ferry-scene_writer | gemini-3.1-pro | stop | 1347 | 8.074 | 336 | `cca239f059655ce7` |
| real_ferry-scene_writer | gemini-3.1-pro | stop | 1923 | 18.624 | 480 | `e4c48fc58c7fcc44` |
| real_ferry-scene_writer | gemini-3.1-pro | stop | 1071 | 7.274 | 267 | `8520b99c03f90684` |
| real_ferry-chapter_single_shot | gemini-3.1-pro | stop | 1307 | 6.976 | 326 | `4854558593aa9b24` |
| real_ferry-chapter_single_shot | gemini-3.1-pro | stop | 1467 | 8.851 | 366 | `a8e86a587ade8843` |
| real_ferry-chapter_single_shot | gemini-3.1-pro | stop | 1957 | 17.915 | 489 | `1897e921210240d4` |
| real_stall-scene_writer | gemini-3.1-pro | stop | 1776 | 9.801 | 444 | `23f543b35d0335bc` |
| real_stall-scene_writer | gemini-3.1-pro | stop | 1395 | 9.119 | 348 | `192e7c296b6fea7d` |
| real_stall-scene_writer | gemini-3.1-pro | stop | 1361 | 8.387 | 340 | `2f8ce57b8d0815f2` |
| real_stall-chapter_single_shot | gemini-3.1-pro | stop | 1513 | 9.637 | 378 | `1bc1b40b73db177c` |
| real_stall-chapter_single_shot | gemini-3.1-pro | stop | 1289 | 7.525 | 322 | `30a67dd876b0bb78` |
| real_stall-chapter_single_shot | gemini-3.1-pro | stop | 1363 | 6.865 | 340 | `478ec1a9f57fd6ca` |
| fantasy_bell-scene_writer | gemini-3.1-pro | stop | 1263 | 10.436 | 315 | `8b03d6828fa22531` |
| fantasy_bell-scene_writer | gemini-3.1-pro | stop | 1238 | 8.979 | 309 | `45503e0ce1ec4a45` |
| fantasy_bell-scene_writer | gemini-3.1-pro | stop | 1179 | 7.449 | 294 | `32654a5fa62c7027` |
| fantasy_bell-chapter_single_shot | gemini-3.1-pro | stop | 1349 | 8.441 | 337 | `654205b69e7f6879` |
| fantasy_bell-chapter_single_shot | gemini-3.1-pro | stop | 1530 | 10.625 | 382 | `276f5ced18664ea7` |
| fantasy_bell-chapter_single_shot | gemini-3.1-pro | stop | 1335 | 7.866 | 333 | `72197d98c6383ecb` |
| fantasy_herb-scene_writer | gemini-3.1-pro | stop | 1158 | 19.368 | 289 | `62e2c80e2837da48` |
| fantasy_herb-scene_writer | gemini-3.1-pro | stop | 976 | 7.027 | 244 | `e1fcc0e36367c764` |
| fantasy_herb-scene_writer | gemini-3.1-pro | stop | 962 | 7.89 | 240 | `f71faeccce4dacbe` |
| fantasy_herb-chapter_single_shot | gemini-3.1-pro | stop | 954 | 6.695 | 238 | `83661b446cc6ac0f` |
| fantasy_herb-chapter_single_shot | gemini-3.1-pro | stop | 1661 | 18.429 | 415 | `a87b97f7c5c71998` |
| fantasy_herb-chapter_single_shot | gemini-3.1-pro | stop | 1142 | 7.291 | 285 | `bf40c8e965fe8026` |
| mystery_bridge-scene_writer | gemini-3.1-pro | stop | 988 | 7.615 | 247 | `d444a99546940481` |
| mystery_bridge-scene_writer | gemini-3.1-pro | stop | 1034 | 6.426 | 258 | `d431c6f66ea04295` |
| mystery_bridge-scene_writer | gemini-3.1-pro | stop | 1126 | 7.353 | 281 | `3c9746b2bf5c3f62` |
| mystery_bridge-chapter_single_shot | gemini-3.1-pro | stop | 992 | 5.664 | 248 | `65b57092d6a5d899` |
| mystery_bridge-chapter_single_shot | gemini-3.1-pro | stop | 1701 | 21.323 | 425 | `f615fb70ba6ea3d3` |
| mystery_bridge-chapter_single_shot | gemini-3.1-pro | stop | 1377 | 22.575 | 344 | `1a40954bfac9d358` |
| mystery_lodger-scene_writer | gemini-3.1-pro | stop | 1080 | 7.325 | 270 | `1bd652909cacdfaa` |
| mystery_lodger-scene_writer | gemini-3.1-pro | stop | 1579 | 10.324 | 394 | `33c85704d874cea7` |
| mystery_lodger-scene_writer | gemini-3.1-pro | stop | 1504 | 8.907 | 376 | `32cf481a4009892b` |
| mystery_lodger-chapter_single_shot | gemini-3.1-pro | stop | 1051 | 7.563 | 262 | `4f6b5c5b67dadde7` |
| mystery_lodger-chapter_single_shot | gemini-3.1-pro | stop | 1389 | 9.104 | 347 | `f294c88647aeeb4c` |
| mystery_lodger-chapter_single_shot | gemini-3.1-pro | stop | 1319 | 19.442 | 329 | `e1e7f690f1261dbd` |
