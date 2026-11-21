# 换模型测试:`deepseek-v4-flash`(2026-11-15)

用户要求试这个模型。模型本身可用,但**目前不能稳定跑通本平台的连续写作链**,
而且**不能用来做 A/B 测量**。下面是证据。

## 1. 可用性(单次调用)

| 项目 | 结果 |
|---|---|
| 非流式 `chat/completions` | 正常返回中文正文,`finish_reason=stop`,无空文本(vs `deepseek-v4.1-flash` 会返回空文本) |
| 流式 `stream=true` | 正常,逐 chunk 返回 |
| JSON 任务(分场规划式) | 正常,`finish_reason=stop`,输出可解析 |
| 速度 | 一个约 1400 字场景约 22 秒;比 `gemini-3.1-pro` 略慢 |
| 篇幅 | 固定样本集里平均 1737 字/次(gemini `after-r8` 是 1231 字/次) |

## 2. 它和平台的两个结构性冲突

**它是"先思考后输出"的模型。** 实测(长提示,流式):一个章节响应共 **1388 个 chunk**,
其中正文到**第 89 个 chunk** 才出现,前面 87 个只有 `reasoning_content`;
一次 8000 字任务里它把 3856 个 completion token 全用在思考上、**正文长度为 0**。

**(a) `guard_chat_stream` 的缓冲上界按"所有 chunk"计,推理前缀会撑爆它。**
生产链第 1 章即报:

```
All stream tier-fallback attempts failed for task 'scene_writer':
LLMRelayResponseError: Relay stream opening exceeded validation buffer
```

原因:`provider_response_guard.guard_chat_stream` 在判定"这段开头是不是中转层诊断文本"时,
把**所有** chunk 都记进同一个 256 上限。它的本意是"暂存到看见正文为止",
所以正确口径是:**只有带 `content` 的 chunk 才算候选诊断文本**。

已修(见下节)。修后该错误不再出现。

**(b) `require_normal_finish` 只接受 `stop`/`end_turn`/`stop_sequence`。**
修掉 (a) 之后,链条推进到规划步就停在:

```
scene_planner_failed: provider_incomplete_response
```

`incomplete_delivery` 来自这一条硬规则:除了那三种结束原因,一律算"交付不完整"。
单次复现里这个模型的结束原因大多是 `stop`,但也出现过**空正文 + `stop`**、
以及被这条规则判为不完整的响应。**我没有改动这条规则**——它保护的是所有模型
("模型说完了"和"被截断了"必须分清),为了迁就一个模型放宽它,是把守卫变成摆设。
要支持这类模型,需要的是**按模型声明的结束原因策略**或更大的输出额度,
这是一项独立改动,本批没做。

## 3. 更严重:这个网关对响应做了缓存

同一份请求(messages + 参数相同)重复发送,**返回逐字节相同**的内容:

```
温度 0.9,同一 prompt 两次: a67ec7b139 / a67ec7b139   ← 完全相同
温度 0.9,只改一个字:        7ee6b36cc1
```

固定样本集 18 次调用里只有 **7 个互不相同的文本**:6 个案例中 5 个的三次重复完全一样,
第 6 个(`fantasy_herb`)第一轮被截断成 134 字,`attempts` 显示**重试 5 次**都拿到同一份
被缓存的截断响应,最后失败。

后果:

- `--repeats N` 退化成 **n=1**;
- 重试**逃不出**一条坏响应;
- 用它做任何 A/B 或方差分析,**结论无效**。

## 4. 因此,固定样本集的结果只能当"单次采样",不是 n=3

同一运行(`scripts/quality_eval run`,6 场景,`--repeats 3`,契约与 gemini 臂相同):

| 指标(每千字) | `deepseek-v4-flash` | `gemini-3.1-pro` after-r8 |
|---|---|---|
| `fact_violation` | **1.25** | 1.24 |
| `rule_recitation`(复述设定) | **0.26** | 0.10 |
| `metaphor_stack`(比喻堆叠) | **0.19** | 0.02 |
| `pov_breach` | **0.00** | 0.12 |
| `outcome_unfulfilled` | 0.29 | 1.02(该指标已被证明是检测器产物) |
| `instruction_leak` | 0 | 0 |

人工看两条命中:

- `mechanism_explained`(真):"桥墩顶部平整,只够一个人蹲着,两个人站上去就得有一方退下水去。"
  —— 把条件与上限当规则讲出来,正是契约要压的那类写法;
- `paragraph_simile_density`(边缘):同一段里"像纸似地""像有一道看不见的堤"共 3–4 处,
  阈值 3,判为堆叠合理但偏严。

**样本只有一个有效采样/案例,以上只是"这个模型不是不能写",不是质量结论。**

## 5. 这一轮的实际产出

| 改动 | 文件 |
|---|---|
| 流式开头守卫:诊断判定只看带 `content` 的 chunk;思考前缀单独设有界(8192 chunk),空白/诊断前缀仍是 4096 字上界 | [`provider_response_guard.py`](/root/ai-write/backend/app/services/provider_response_guard.py:19) |
| 回归 3 项:400 个思考 chunk 不误杀 / 诊断文本仍被拒 / 空白与超长思考前缀仍被挡 | [`test_stream_opening_guard.py`](/root/ai-write/backend/tests/test_stream_opening_guard.py:1) |
| 探针新增 `--max-tokens`(推理模型需要更大额度;原先写死 4096) | [`continuity_probe.py`](/root/ai-write/backend/scripts/quality_eval/continuity_probe.py:1057) |

## 6. 结论与下一步

- **可用**:单次生成、JSON 任务都正常;中文散文质量与 `gemini-3.1-pro` 同档(事实类持平,
  设定复述与比喻堆叠偏差,视角更稳)。
- **不可用于长链(现状)**:规划步仍被 `provider_incomplete_response` 挡住;
  要打通需要"按模型的结束原因策略"或显著更大的输出额度,属独立改动。
- **不可用于测量**:网关缓存使重复与重试都失去意义。
- 上述结论都基于本机这个网关;换直连或关掉缓存的网关需要重测。
