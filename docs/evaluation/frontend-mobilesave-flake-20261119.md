# 结清挂账:`mobileSave.test.tsx` 的"间歇失败"(2026-11-19)

## 一、这次到底发生了什么

此前记录写着:

> `mobileSave.test.tsx` 的间歇失败已确认不是超时(3s 仍失败),疑似任务对象身份变化导致 detail 被丢弃,待单独排查。

实测**不是间歇,是稳定失败**(单文件连跑 3 次全红,约 50% 命中另一个失败点)。定位后修掉两层问题。

## 二、根因 1(测试):点了被禁用的按钮

`MobileTaskPanel` 的"重新读取任务"按钮带 `disabled={loading}` —— 任务详情正在读取时它是禁用的。

用例 `requires a clear volume target and labels the completed volume outline as a candidate` 在点击它之前**没有等它可用**:

```tsx
await screen.findByText(/任务交付的是卷纲候选/)   // 这段文案由 task.status 驱动,可能立即为真
fireEvent.click(screen.getByText('重新读取任务'))  // 此时按钮可能仍 disabled → 点击落空
expect(screen.getByLabelText('任务候选全文')).toHaveValue(...)  // 详情没重读 → 失败
```

点击落空后 watch 停在那个 3 秒的轮询节拍上,而断言只有 1 秒超时 —— 于是"看起来像间歇失败"。

**修**:点击前 `await waitFor(() => expect(...).not.toBeDisabled())`;并把断言顺序改成**先等详情节点**(`findByLabelText('任务候选全文')`)再断内容 —— 原来先等的那段文案不依赖详情,失败信息会指向错误的地方。

## 三、根因 2(产品):详情被"引用比较"丢掉

`useMobileTaskProgress` 原本这样判断"这份详情还属不属于当前任务":

```ts
setSnapshot((previous) => ({ task, detail: previous?.task === task ? previous.detail : null, ... }))
...
const current = snapshot?.task === task ? snapshot : null
```

`task` 是**对象引用**。而任务列表按 3 秒轮询刷新,每次都会重建任务对象 —— 同一条任务、同样的内容,引用却变了。后果:

- 已读回的详情被反复丢弃 → 界面上"候选全文 / 进度记录"闪烁或直接看不到;
- 每次都要重新读一遍详情。

**修**:按**稳定身份**比较(`projectId::taskId`),身份不变就保留详情;来源字段(`task_type` / `chapter_id` / `volume_idx`)的真实变化仍由 `parseMobileTaskDetail` 的严格校验拦截并给出"来源已变化"提示。

同一处还修了一个竞态:effect 里先 `setSnapshot(loading)` 再立即发起读取,读取回调可能在 React 应用那次更新**之前**返回 —— 此时"只在 previous 匹配时才写入"会把刚到手的详情丢掉。现在 key 不匹配时也写入当前 key 的快照。

## 四、验证

| 项 | 结果 |
|---|---|
| 单文件连跑 8 次 | **8/8 全通过**(修复前 3/6 失败) |
| 前端全量用例 | **616 passed / 61 files**(exit 0) |
| `npx tsc --noEmit` | **0 错** |
| `npm run build` | **成功**(Compiled successfully,21 页静态生成) |

## 五、改动文件

- `frontend/src/components/workspace/useMobileTaskProgress.ts`(产品:稳定身份 + 快照写入竞态)
- `frontend/src/__tests__/mobileSave.test.tsx`(测试:等按钮可用 + 先等详情节点)

调试过程中临时加的 `console.error` 已全部清除(已验证两文件 0 处 `DBG`)。
