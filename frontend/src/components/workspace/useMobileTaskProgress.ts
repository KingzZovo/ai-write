'use client'

import { useEffect, useState } from 'react'
import { MobileTask, MobileTaskDetail, watchMobileTask } from './mobileTaskProgress'

/**
 * 任务详情的读取状态。
 *
 * 2026-11-19 修:此前用**对象引用**判断"这份 detail 还属不属于当前 task"
 * (`previous?.task === task`)。任务列表每次轮询都会重建对象,于是同一条任务的
 * 详情会被反复丢弃:界面表现为候选全文/进度记录一闪就没,并且每次都要重新读取。
 * 现在按**稳定身份**(projectId + taskId)比较 —— 身份不变就保留已读回的详情,
 * 身份变了才重置。来源字段(task_type/chapter_id/volume_idx)的真实变化仍由
 * `parseMobileTaskDetail` 的严格校验拦截。
 */
export function useMobileTaskProgress(task: MobileTask | null) {
  const [attempt, setAttempt] = useState(0)
  const key = task ? `${task.projectId}::${task.taskId}` : null
  const [snapshot, setSnapshot] = useState<{
    key: string
    task: MobileTask
    detail: MobileTaskDetail | null
    error: string | null
    loading: boolean
  } | null>(null)

  useEffect(() => {
    if (!task || key === null) return
    setSnapshot((previous) => ({
      key,
      task,
      detail: previous?.key === key ? previous.detail : null,
      error: null,
      loading: true,
    }))
    const sameKey = (previous: { key: string } | null) => previous?.key === key
    // 注意:不能只在 `previous?.key === key` 时才写入 —— effect 里刚 setSnapshot(loading)
    // 之后立刻发起读取,而读取(尤其本地/测试)可能在 React 应用那次更新**之前**返回,
    // 此时 previous 还是旧的(或 null),按旧写法会把刚到手的详情直接丢掉,表现为
    // "读到了但界面没有"的间歇失败。key 不匹配时就用当前 key 建一份快照。
    return watchMobileTask(task, {
      received: (detail) => setSnapshot((previous) => (
        sameKey(previous)
          ? { ...previous!, task, detail, error: null, loading: false }
          : { key, task, detail, error: null, loading: false })),

      failed: (error) => setSnapshot((previous) => (
        sameKey(previous)
          ? { ...previous!, task, error, loading: false }
          : { key, task, detail: null, error, loading: false })),

    })
    // 依赖只写稳定身份与重试计数:列表刷新(引用变化)不应重启轮询或清空详情。
  }, [key, attempt])

  const current = snapshot?.key === key ? snapshot : null
  return { detail: current?.detail ?? null, error: current?.error ?? null, loading: current?.loading ?? !!task,
    retry: () => setAttempt((value) => value + 1) }
}
