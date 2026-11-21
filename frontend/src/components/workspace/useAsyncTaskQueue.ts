'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import { apiFetch } from '@/lib/api'
import { usePolling } from '@/lib/usePolling'
import { useGenerationSubmission } from './useGenerationSubmission'
import { parseMobileTask, parseMobileTasks } from './mobileTaskProgress'
import { type AsyncTask, hasActiveTask, mergeTasks, normalizeTask, preferTaskObservation, upsertTask } from './asyncTaskQueue'

export interface SubmitChapterTaskArgs {
  chapterId: string
  expectedContentText: string
  styleId?: string | null
  userInstruction?: string
  targetWords?: number | null
}

export function useAsyncTaskQueue(projectId: string | null | undefined) {
  const [ownedTasks, setOwnedTasks] = useState<{ projectId: string; rows: AsyncTask[] } | null>(null)
  const [error, setError] = useState<string | null>(null)
  const { session: submission, state: submissionState } = useGenerationSubmission(projectId)
  const project = useRef(projectId)
  const epoch = useRef(0)
  const readSequence = useRef(0)
  const inFlight = useRef<{ projectId: string; epoch: number; controller: AbortController } | null>(null)
  const tasks = ownedTasks && ownedTasks.projectId === projectId ? ownedTasks.rows : []

  const invalidateReads = useCallback(() => {
    readSequence.current += 1
    inFlight.current?.controller.abort()
    inFlight.current = null
  }, [])

  const applyTasks = useCallback((pid: string, updater: (prev: AsyncTask[]) => AsyncTask[]) => {
    if (project.current !== pid) return
    setOwnedTasks((prev) => ({ projectId: pid, rows: updater(prev?.projectId === pid ? prev.rows : []) }))
  }, [])

  const refresh = useCallback(async () => {
    if (!projectId) return
    if (inFlight.current?.projectId === projectId && inFlight.current.epoch === epoch.current) return
    const owner = epoch.current, sequence = ++readSequence.current
    const controller = new AbortController()
    inFlight.current = { projectId, epoch: owner, controller }
    const timer = setTimeout(() => controller.abort(), 20_000)
    const valid = () => project.current === projectId && epoch.current === owner && sequence === readSequence.current
    try {
      const [recent, active] = await Promise.all([
        apiFetch<unknown>(`/api/generate/async/project/${projectId}?limit=20`, { signal: controller.signal }),
        apiFetch<unknown>(`/api/generate/async/project/${projectId}?limit=100&active_only=true`, { signal: controller.signal }),
      ])
      parseMobileTasks(recent, projectId)
      parseMobileTasks(active, projectId)
      const byId = new Map<string, AsyncTask>()
      for (const row of [...recent as unknown[], ...active as unknown[]]) {
        const task = normalizeTask(row)
        if (task) byId.set(task.taskId, preferTaskObservation(byId.get(task.taskId), task))
      }
      if (!valid()) return
      // The current list has bounded progress/error fields; reading every
      // active task's full candidate here would turn long queues into N+1 IO.
      applyTasks(projectId, (prev) => mergeTasks(prev, [...byId.values()]))
      setError(null)
    } catch (cause) {
      if (valid()) setError(cause instanceof Error ? cause.message : '后台任务读取失败，请重新核对。')
    } finally {
      clearTimeout(timer)
      if (inFlight.current?.controller === controller) inFlight.current = null
    }
  }, [projectId, applyTasks])

  useEffect(() => {
    project.current = projectId
    epoch.current += 1
    setError(null)
    setOwnedTasks(null)
    if (projectId) void refresh()
    return () => { epoch.current += 1; readSequence.current += 1; inFlight.current?.controller.abort(); inFlight.current = null }
  }, [projectId, refresh])

  const receipt = submissionState?.receipt
  useEffect(() => {
    if (!receipt || receipt.project_id !== projectId) return
    const task = normalizeTask(receipt)
    if (task) {
      invalidateReads()
      applyTasks(receipt.project_id, (prev) => upsertTask(prev, task))
      void refresh()
    }
  }, [receipt, projectId, applyTasks, invalidateReads, refresh])

  usePolling(refresh, 5000, hasActiveTask(tasks))

  const submitChapterTask = useCallback(async (args: SubmitChapterTaskArgs): Promise<AsyncTask> => {
    if (!projectId || !submission) throw new Error('未选择作品。')
    const result = await submission.submit({ project_id: projectId, task_type: 'chapter', chapter_id: args.chapterId,
      expected_content_text: args.expectedContentText, ...(args.styleId ? { style_id: args.styleId } : {}),
      ...(args.userInstruction ? { user_input: args.userInstruction } : {}),
      ...(args.targetWords ? { params: { target_words: args.targetWords } } : {}),
    })
    parseMobileTask(result, projectId)
    // All acknowledgements, including explicit retries and replies received
    // while away from this project, enter through the source-checked effect.
    return normalizeTask(result)!
  }, [projectId, submission])

  const cancelTask = useCallback(async (taskId: string): Promise<string | null> => {
    if (!projectId) return '未选择作品。'
    const owner = epoch.current
    try {
      const result = await apiFetch<{ task_id?: string; status?: string }>(`/api/generate/async/${taskId}/cancel`, { method: 'POST' })
      if (result.task_id !== taskId || result.status !== 'cancelled') throw new Error('取消结果未获确认，请重新读取原任务。')
      if (owner === epoch.current) {
        invalidateReads()
        applyTasks(projectId, (prev) => prev.map((task) => task.taskId === taskId ? { ...task, status: 'cancelled' } : task))
        void refresh()
      }
      return null
    } catch (cause) {
      if (project.current === projectId && owner === epoch.current) void refresh()
      const message = cause instanceof Error ? cause.message : '取消结果未确认。'
      return message === 'task_already_terminal' ? '任务已结束，无法取消。' : message
    }
  }, [projectId, refresh, applyTasks, invalidateReads])

  return { tasks, error, cancelUnavailable: false, refresh, submitChapterTask, cancelTask,
    submissionPending: Boolean(submissionState?.pending || submissionState?.storageError || !submissionState?.initialized) }
}
