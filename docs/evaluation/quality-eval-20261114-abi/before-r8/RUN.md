# 质量验收运行记录(机器生成)

本文件只陈述本次实际执行的事实。文学判断见同目录 `REVIEW.md`。

- 样本集:sample_set_v1(6 个场景,3 个题材)
- 网关:http://127.0.0.1:8317/v1
- 模型:gemini-3.1-pro
- 生产契约:{"narrative_contract_version": "world_logic_contract_v4_15_scene_planning", "blueprint_version": "v4.14", "writer_contract_sha256": "7109b170f9908bf2", "scene_writer_system_sha256": "806255474cc40ff3"}
- 调用:48 次,成功 48,失败 0
- 正文总字数:65641
- token 用量:输入 22376,输出 16388
- 累计耗时:555.023 秒

## 确定性检测计数

| 类别 | 命中数 | 其中 hard |
|---|---|---|
| pov_breach | 11 | 0 |
| instruction_leak | 0 | 0 |
| rule_recitation | 19 | 0 |
| metaphor_stack | 2 | 0 |
| outcome_unfulfilled | 42 | 42 |
| fact_violation | 100 | 50 |

## 未兑现的合同条目

| 条目 | 出现次数 |
|---|---|
| forbidden:both_saved | 2 |
| forbidden:herb_stays | 1 |
| forbidden:monster | 1 |
| must_keep:bell_destroyed | 4 |
| must_keep:deadline | 2 |
| must_keep:debt_total | 1 |
| must_keep:father_gone | 5 |
| must_keep:fourteen_rooms | 5 |
| must_keep:half_jar | 1 |
| must_keep:no_neighbor_loan | 3 |
| must_keep:rain | 1 |
| must_keep:rule | 3 |
| must_keep:sister_ignorant | 4 |
| must_keep:ten_breaths | 3 |
| must_keep:three_years | 1 |
| must_keep:unsealed_letter | 2 |
| must_keep:wet_shoe | 5 |
| required_outcomes:acts | 1 |
| required_outcomes:both_ashore | 1 |
| required_outcomes:brother_boards | 4 |
| required_outcomes:concrete_action | 5 |
| required_outcomes:decision | 1 |
| required_outcomes:discovers_shortage | 1 |
| required_outcomes:handles_waiter | 2 |
| required_outcomes:judgment_changes | 8 |
| required_outcomes:makes_choice | 5 |
| required_outcomes:plan_made | 3 |
| required_outcomes:reads_letter | 2 |
| required_outcomes:recognizes_conflict | 5 |
| required_outcomes:shortfall_declared | 1 |
| required_outcomes:temporary_deal | 2 |

## 按变体

| 变体 | 场景数 | 字数 | hard 合计 |
|---|---|---|---|
| scene_writer | 42 | 57474 | 80 |
| unknown | 6 | 8167 | 12 |

## 逐次调用

| 产出 | 模型返回 | finish_reason | 字数 | 耗时(s) | 输出 token | 正文 sha256 |
|---|---|---|---|---|---|---|
| real_ferry-scene_writer | gemini-3.1-pro | stop | 1597 | 9.625 | 399 | `0c223e3760786764` |
| real_ferry-scene_writer | gemini-3.1-pro | stop | 1526 | 8.696 | 381 | `bc56e2206e5720e5` |
| real_ferry-scene_writer | gemini-3.1-pro | stop | 1514 | 9.37 | 378 | `e786d57689ae7a3d` |
| real_ferry-scene_writer | gemini-3.1-pro | stop | 2116 | 17.278 | 529 | `22c9ee085075e0f3` |
| real_ferry-scene_writer | gemini-3.1-pro | stop | 1419 | 8.477 | 354 | `408a0c750824ea98` |
| real_ferry-scene_writer | gemini-3.1-pro | stop | 1941 | 10.628 | 485 | `4643d675659661c8` |
| real_ferry-scene_writer | gemini-3.1-pro | stop | 1820 | 10.346 | 455 | `716db60b7895f884` |
| real_ferry-scene_writer | gemini-3.1-pro | stop | 1651 | 11.603 | 412 | `016341f0b908e83d` |
| real_stall-scene_writer | gemini-3.1-pro | stop | 1433 | 14.224 | 358 | `81dc8250214646c1` |
| real_stall-scene_writer | gemini-3.1-pro | stop | 1539 | 8.441 | 384 | `cd5141695c5efbc1` |
| real_stall-scene_writer | gemini-3.1-pro | stop | 1278 | 14.441 | 319 | `6d0289b8852e9a4b` |
| real_stall-scene_writer | gemini-3.1-pro | stop | 1720 | 13.822 | 430 | `f2dd2c4fdb63e3da` |
| real_stall-scene_writer | gemini-3.1-pro | stop | 1419 | 14.319 | 354 | `e66829e8f66a2849` |
| real_stall-scene_writer | gemini-3.1-pro | stop | 1621 | 8.931 | 405 | `16f47bbabb844012` |
| real_stall-scene_writer | gemini-3.1-pro | stop | 1435 | 16.393 | 358 | `3ae688200387d688` |
| real_stall-scene_writer | gemini-3.1-pro | stop | 1378 | 7.311 | 344 | `282a5ada3bf26206` |
| fantasy_bell-scene_writer | gemini-3.1-pro | stop | 1378 | 7.599 | 344 | `906c72c62eb6bf1a` |
| fantasy_bell-scene_writer | gemini-3.1-pro | stop | 1429 | 8.659 | 357 | `6361eb0f79e8060f` |
| fantasy_bell-scene_writer | gemini-3.1-pro | stop | 1217 | 11.043 | 304 | `61bbc15d8d136f7c` |
| fantasy_bell-scene_writer | gemini-3.1-pro | stop | 1231 | 7.774 | 307 | `1d0619ead6ad5279` |
| fantasy_bell-scene_writer | gemini-3.1-pro | stop | 1165 | 7.42 | 291 | `d814fc479740f2fd` |
| fantasy_bell-scene_writer | gemini-3.1-pro | stop | 1278 | 7.558 | 319 | `0007c7c992576cf3` |
| fantasy_bell-scene_writer | gemini-3.1-pro | stop | 1010 | 6.382 | 252 | `7458beadb3f5c792` |
| fantasy_bell-scene_writer | gemini-3.1-pro | stop | 1366 | 15.979 | 341 | `8f19616241c7e2cf` |
| fantasy_herb-scene_writer | gemini-3.1-pro | stop | 1147 | 7.938 | 286 | `adea51c762e0af1b` |
| fantasy_herb-scene_writer | gemini-3.1-pro | stop | 1382 | 9.004 | 345 | `b6c1dd6df41a8fca` |
| fantasy_herb-scene_writer | gemini-3.1-pro | stop | 1313 | 7.558 | 328 | `26fa60d220eb8043` |
| fantasy_herb-scene_writer | gemini-3.1-pro | stop | 1038 | 7.094 | 259 | `f7a5e0bef22a2478` |
| fantasy_herb-scene_writer | gemini-3.1-pro | stop | 1363 | 8.515 | 340 | `e89e57064070cee0` |
| fantasy_herb-scene_writer | gemini-3.1-pro | stop | 1426 | 21.707 | 356 | `2a4cad228bc4cbf5` |
| fantasy_herb-scene_writer | gemini-3.1-pro | stop | 1363 | 17.58 | 340 | `efe30a532c66d8a8` |
| fantasy_herb-scene_writer | gemini-3.1-pro | stop | 1067 | 7.773 | 266 | `d0a88eae95eb04a7` |
| mystery_bridge-scene_writer | gemini-3.1-pro | stop | 1595 | 18.538 | 398 | `1ad604d0fc06e1ea` |
| mystery_bridge-scene_writer | gemini-3.1-pro | stop | 1270 | 8.106 | 317 | `bc897fe6f6eebffe` |
| mystery_bridge-scene_writer | gemini-3.1-pro | stop | 1106 | 33.115 | 276 | `abd3ec10a6c8b6e4` |
| mystery_bridge-scene_writer | gemini-3.1-pro | stop | 1067 | 7.174 | 266 | `ca8e93132e4cabfb` |
| mystery_bridge-scene_writer | gemini-3.1-pro | stop | 1147 | 7.169 | 286 | `15506cfc0d67abea` |
| mystery_bridge-scene_writer | gemini-3.1-pro | stop | 1170 | 26.456 | 292 | `f0e45b39a94addc5` |
| mystery_bridge-scene_writer | gemini-3.1-pro | stop | 1163 | 7.741 | 290 | `e04d4457cea915c9` |
| mystery_bridge-scene_writer | gemini-3.1-pro | stop | 1763 | 23.449 | 440 | `32d93b146b6aa1c4` |
| mystery_lodger-scene_writer | gemini-3.1-pro | stop | 1017 | 6.332 | 254 | `dcf53627ea76381e` |
| mystery_lodger-scene_writer | gemini-3.1-pro | stop | 1292 | 7.639 | 323 | `31be253a06748149` |
| mystery_lodger-scene_writer | gemini-3.1-pro | stop | 1191 | 7.005 | 297 | `d240ea03337369c7` |
| mystery_lodger-scene_writer | gemini-3.1-pro | stop | 936 | 6.413 | 234 | `bf0663db02252af5` |
| mystery_lodger-scene_writer | gemini-3.1-pro | stop | 1682 | 18.802 | 420 | `ed6cb6b363ca1466` |
| mystery_lodger-scene_writer | gemini-3.1-pro | stop | 1220 | 6.589 | 305 | `1cabb63e0d4b8a4a` |
| mystery_lodger-scene_writer | gemini-3.1-pro | stop | 1113 | 19.641 | 278 | `4c7c36a54d543ba7` |
| mystery_lodger-scene_writer | gemini-3.1-pro | stop | 1329 | 7.366 | 332 | `77e76d76b99a14bd` |
