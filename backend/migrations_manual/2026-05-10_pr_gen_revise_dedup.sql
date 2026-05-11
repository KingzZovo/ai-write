-- PR-GEN-REVISE-DEDUP
-- Hardens scene_planner / scene_writer system prompts with explicit mutex /
-- no-redo constraints. Idempotent: guarded by NOT LIKE markers.
--
-- Root cause (chixin ch8/10/12 batch 5/9-5/10):
--   * scene_planner emitted overlapping key_action across adjacent scenes
--     (one meeting / verify / hall-entry repeated as 2-3 scenes).
--   * scene_writer treated 「已写场景凝缩」 as background not a hard
--     constraint, then redid the prior scene's action under fresh framing.
-- Pairs with backend/app/services/scene_orchestrator.py mutex_block /
-- prior_block hardening in the same commit.

UPDATE prompt_assets
SET system_prompt = system_prompt || E'\n【场景互斥硬约束（与 prompt_assets.scene_planner.user_content 中 mutex_block 同效安装）】\n- 各场景的 location / time_cue / key_action 必须互不重复；同一情节（一次会面、一次验证、一次入廊、一次表态）只能在 1 个 scene 中推进，禁止跨 scene 复述、回拨、补做。\n- 同一线索需分阶段呈现时，必须以可辨别的状态切片（前置铺垫 / 当下推进 / 后续余响），brief 中不得重复上一 scene 的「动词 + 受事」组合。\n'
WHERE task_type='scene_planner'
  AND system_prompt NOT LIKE '%场景互斥硬约束%';

UPDATE prompt_assets
SET system_prompt = system_prompt || E'\n【已发生场景互斥（硬约束，与 prior_block 同效安装）】\n- 你收到的「已发生场景 / 已写场景凝缩」中描述的动作、对白、地点切换、状态变化均已在前序场景完成；本场严禁重写、严禁改述、严禁让人物再次进入这些动作或场景。\n- 只能从凝缩末尾的状态向前推进到本场 key_action；如需提及前事，只可用一句类似「刚才验证了某某」的转场句，不得以动作 / 对白重现。\n'
WHERE task_type='scene_writer'
  AND system_prompt NOT LIKE '%已发生场景互斥%';
