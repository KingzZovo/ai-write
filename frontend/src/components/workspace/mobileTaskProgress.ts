import { apiFetch } from '@/lib/api'
import { isTerminal, preferTaskObservation } from './asyncTaskQueue'

export type MobileTask = {
  taskId: string
  projectId: string
  chapterId: string | null
  volumeIdx: number | null
  taskType: string
  status: string
}

export type MobileTaskDetail = MobileTask & {
  progressText: string
  resultText: string
  polishedText: string
  errorMessage: string | null
}

const active = new Set(['pending', 'running', 'polishing', 'evaluating', 'regenerating'])
export const isMobileTaskTerminal = isTerminal

function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('任务信息不完整，请刷新后台任务。')
  return value as Record<string, unknown>
}

function requiredText(value: unknown): string {
  if (typeof value !== 'string' || !value.trim()) throw new Error('任务来源不完整，请刷新后台任务。')
  return value
}

export function parseMobileTask(value: unknown, projectId: string): MobileTask {
  const row = record(value)
  if (row.project_id !== projectId) throw new Error('后台任务的作品来源不一致，请重新读取。')
  if (row.chapter_id !== null && (typeof row.chapter_id !== 'string' || !row.chapter_id.trim())) {
    throw new Error('后台任务的章节来源不完整，请重新读取。')
  }
  if (row.volume_idx !== null && (typeof row.volume_idx !== 'number' || !Number.isSafeInteger(row.volume_idx) || row.volume_idx < 1)) {
    throw new Error('后台任务的分卷来源不完整，请重新读取。')
  }
  return { projectId, taskId: requiredText(row.task_id), taskType: requiredText(row.task_type),
    chapterId: row.chapter_id as string | null, volumeIdx: row.volume_idx as number | null, status: requiredText(row.status) }
}

export function parseMobileTasks(value: unknown, projectId: string): MobileTask[] {
  if (!Array.isArray(value)) throw new Error('后台任务列表未能确认，请重新读取。')
  const tasks = value.map((row) => parseMobileTask(row, projectId))
  if (new Set(tasks.map((task) => task.taskId)).size !== tasks.length) throw new Error('后台任务列表有重复来源，请重新读取。')
  return tasks
}

export function mergeMobileTaskLists(previous: MobileTask[], incoming: MobileTask[]): MobileTask[] {
  const byId = new Map(previous.map((task) => [task.taskId, task]))
  for (const task of incoming) byId.set(task.taskId, preferTaskObservation(byId.get(task.taskId), task))
  return [...byId.values()]
}

export function parseMobileTaskDetail(value: unknown, expected: MobileTask): MobileTaskDetail {
  const row = record(value)
  const task = parseMobileTask(value, expected.projectId)
  if (task.taskId !== expected.taskId || task.taskType !== expected.taskType || task.chapterId !== expected.chapterId || task.volumeIdx !== expected.volumeIdx) {
    throw new Error('后台任务来源已变化，已停止更新；请刷新任务核对。')
  }
  for (const field of ['progress_text', 'result_text', 'polished_text']) {
    if (typeof row[field] !== 'string') throw new Error('后台任务文本未完整返回，请重新读取。')
  }
  if (row.error_message !== null && typeof row.error_message !== 'string') throw new Error('后台任务状态未完整返回，请重新读取。')
  return { ...task, progressText: row.progress_text as string, resultText: row.result_text as string,
    polishedText: row.polished_text as string, errorMessage: row.error_message as string | null }
}

/** One read at a time. A detached observer cannot deliver a late snapshot. */
export function watchMobileTask(task: MobileTask, callbacks: {
  received: (detail: MobileTaskDetail) => void
  failed: (message: string) => void
}, read = (signal: AbortSignal) => apiFetch<unknown>(`/api/generate/async/${encodeURIComponent(task.taskId)}`, { signal }), intervalMs = 3000) {
  const controller = new AbortController()
  let timer: ReturnType<typeof setTimeout> | null = null
  let stopped = false
  const poll = async () => {
    if (stopped) return
    try {
      const value = await read(controller.signal)
      if (stopped) return
      const detail = parseMobileTaskDetail(value, task)
      callbacks.received(detail)
      if (stopped) return
      if (isMobileTaskTerminal(detail.status)) return
      if (!active.has(detail.status)) throw new Error('任务状态暂不可识别，请刷新任务核对。')
      timer = setTimeout(() => { timer = null; void poll() }, intervalMs)
    } catch (error) {
      if (!stopped) callbacks.failed(error instanceof Error ? error.message : '任务进度读取失败，请重新读取。')
    }
  }
  void poll()
  return () => { stopped = true; controller.abort(); if (timer) clearTimeout(timer) }
}
