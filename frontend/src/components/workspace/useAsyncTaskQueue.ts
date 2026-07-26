'use client'

// PR-WS-QUEUE (2026-07-26): React hook driving the background generation
// queue (后台生成). Resume-on-load fetches the project's tasks once, then
// polls every 5s while any task is non-terminal (mirrors MobileWorkspace's
// usePolling pattern). Pure list/merge logic lives in ./asyncTaskQueue so it
// can be unit-tested without React.

import { useCallback, useEffect, useRef, useState } from 'react'
import { apiFetch } from '@/lib/api'
import { usePolling } from '@/lib/usePolling'
import {
  type AsyncTask,
  hasActiveTask,
  isTerminal,
  mergeTasks,
  normalizeTask,
  upsertTask,
} from './asyncTaskQueue'

export interface SubmitChapterTaskArgs {
  chapterId: string
  styleId?: string | null
  userInstruction?: string
  targetWords?: number | null
}

export function useAsyncTaskQueue(projectId: string | null | undefined) {
  const [tasks, setTasks] = useState<AsyncTask[]>([])
  const [cancelUnavailable, setCancelUnavailable] = useState(false)
  // Ref mirror so async refresh() can merge against the latest list without
  // re-creating the callback on every poll tick.
  const tasksRef = useRef<AsyncTask[]>([])

  const applyTasks = useCallback((updater: (prev: AsyncTask[]) => AsyncTask[]) => {
    setTasks((prev) => {
      const next = updater(prev)
      tasksRef.current = next
      return next
    })
  }, [])

  const refresh = useCallback(async () => {
    if (!projectId) return
    const raw = await apiFetch<unknown>(`/api/generate/async/project/${projectId}`)
    const incoming = (Array.isArray(raw) ? raw : [])
      .map(normalizeTask)
      .filter((t): t is AsyncTask => t !== null)
    let base = mergeTasks(tasksRef.current, incoming)
    // The pre-extension list endpoint omits progress/error fields — fill them
    // in from the detail endpoint for tasks that still need them.
    const detailTargets = base.filter(
      (t) => !isTerminal(t.status) || (t.status === 'failed' && !t.errorMessage),
    )
    for (const t of detailTargets) {
      try {
        const detail = normalizeTask(
          await apiFetch<unknown>(`/api/generate/async/${t.taskId}`),
        )
        if (detail) base = upsertTask(base, detail)
      } catch {
        // transient — keep the list row as-is
      }
    }
    const merged = base
    applyTasks((prev) => {
      // Keep tasks submitted while this refresh was in flight.
      const extras = prev.filter((p) => !merged.some((m) => m.taskId === p.taskId))
      return [...extras, ...merged]
    })
  }, [projectId, applyTasks])

  // Resume-on-load: fetch the project's tasks once per project.
  useEffect(() => {
    applyTasks(() => [])
    if (!projectId) return
    void refresh().catch((err) => console.warn('加载后台任务失败:', err))
  }, [projectId, refresh, applyTasks])

  usePolling(async () => {
    await refresh()
  }, 5000, hasActiveTask(tasks))

  const submitChapterTask = useCallback(
    async (args: SubmitChapterTaskArgs): Promise<AsyncTask> => {
      if (!projectId) throw new Error('未选择项目')
      const res = await apiFetch<{ task_id: string; status?: string }>(
        '/api/generate/async',
        {
          method: 'POST',
          body: JSON.stringify({
            project_id: projectId,
            task_type: 'chapter',
            chapter_id: args.chapterId,
            ...(args.styleId ? { style_id: args.styleId } : {}),
            // The async worker reads the instruction from params user_input
            // and target words from params.target_words.
            ...(args.userInstruction ? { user_input: args.userInstruction } : {}),
            ...(args.targetWords ? { params: { target_words: args.targetWords } } : {}),
          }),
        },
      )
      const task: AsyncTask = {
        taskId: res.task_id,
        taskType: 'chapter',
        status: res.status || 'pending',
        charCount: null,
        progressText: '',
        errorMessage: '',
        chapterId: args.chapterId,
        createdAt: null,
        updatedAt: null,
      }
      applyTasks((prev) => [task, ...prev.filter((t) => t.taskId !== task.taskId)])
      return task
    },
    [projectId, applyTasks],
  )

  /** Returns an error message, or null on success. */
  const cancelTask = useCallback(
    async (taskId: string): Promise<string | null> => {
      try {
        const res = await apiFetch<{ status?: string }>(
          `/api/generate/async/${taskId}/cancel`,
          { method: 'POST' },
        )
        applyTasks((prev) =>
          prev.map((t) =>
            t.taskId === taskId ? { ...t, status: res.status || 'cancelled' } : t,
          ),
        )
        return null
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err)
        if (msg === 'Not Found' || msg === 'Method Not Allowed') {
          // Backend without the cancel endpoint yet — hide cancel buttons.
          setCancelUnavailable(true)
          return '当前后端暂不支持取消任务。'
        }
        // e.g. 409 already terminal — resync so the UI shows the final state.
        void refresh().catch(() => {})
        return msg === 'task_already_terminal' ? '任务已结束，无法取消。' : msg
      }
    },
    [applyTasks, refresh],
  )

  return { tasks, cancelUnavailable, refresh, submitChapterTask, cancelTask }
}
