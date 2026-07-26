// PR-WS-QUEUE (2026-07-26): types + pure helpers for the background
// generation task queue (任务队列). Kept free of React/api imports so vitest
// can cover the logic directly.
//
// The backend list endpoint (/api/generate/async/project/{pid}) currently
// returns only {task_id, task_type, status, char_count, created_at}; an
// in-flight backend change extends it with progress_text / error_message /
// updated_at / chapter_id. normalizeTask + mergeFields handle BOTH shapes:
// missing fields stay empty/null and previously-known values are preserved.

export interface AsyncTask {
  taskId: string
  taskType: string
  status: string
  charCount: number | null
  progressText: string
  errorMessage: string
  chapterId: string | null
  createdAt: string | null
  updatedAt: string | null
}

const TERMINAL_STATUSES = new Set(['completed', 'failed', 'cancelled', 'needs_review'])

export function isTerminal(status: string): boolean {
  return TERMINAL_STATUSES.has(status)
}

export function hasActiveTask(tasks: AsyncTask[]): boolean {
  return tasks.some((t) => !isTerminal(t.status))
}

function asString(v: unknown): string | null {
  return typeof v === 'string' && v ? v : null
}

/** Defensive parse of one task row (list or detail endpoint, either shape). */
export function normalizeTask(raw: unknown): AsyncTask | null {
  if (!raw || typeof raw !== 'object') return null
  const r = raw as Record<string, unknown>
  const taskId = asString(r.task_id)
  if (!taskId) return null
  return {
    taskId,
    taskType: asString(r.task_type) ?? '',
    status: asString(r.status) ?? 'pending',
    charCount: typeof r.char_count === 'number' ? r.char_count : null,
    progressText: typeof r.progress_text === 'string' ? r.progress_text : '',
    errorMessage: typeof r.error_message === 'string' ? r.error_message : '',
    chapterId: asString(r.chapter_id),
    createdAt: asString(r.created_at),
    updatedAt: asString(r.updated_at),
  }
}

/** Keep previously-known values when a newer row omits them (old list shape). */
function mergeFields(prev: AsyncTask, incoming: AsyncTask): AsyncTask {
  return {
    ...incoming,
    taskType: incoming.taskType || prev.taskType,
    charCount: incoming.charCount ?? prev.charCount,
    progressText: incoming.progressText || prev.progressText,
    errorMessage: incoming.errorMessage || prev.errorMessage,
    chapterId: incoming.chapterId ?? prev.chapterId,
    createdAt: incoming.createdAt ?? prev.createdAt,
    updatedAt: incoming.updatedAt ?? prev.updatedAt,
  }
}

/** Upsert one task (e.g. a detail poll result) into the list. */
export function upsertTask(tasks: AsyncTask[], task: AsyncTask): AsyncTask[] {
  const idx = tasks.findIndex((t) => t.taskId === task.taskId)
  if (idx === -1) return [task, ...tasks]
  const next = [...tasks]
  next[idx] = mergeFields(tasks[idx], task)
  return next
}

/**
 * Merge a fresh server list (newest first, limited) with the previous client
 * list. Locally-known tasks the server list no longer returns (e.g. a
 * just-submitted task racing the poll) are kept at the top.
 */
export function mergeTasks(prev: AsyncTask[], incoming: AsyncTask[]): AsyncTask[] {
  const byId = new Map(prev.map((t) => [t.taskId, t]))
  const merged = incoming.map((t) => {
    const old = byId.get(t.taskId)
    return old ? mergeFields(old, t) : t
  })
  const extras = prev.filter((t) => !incoming.some((i) => i.taskId === t.taskId))
  return [...extras, ...merged]
}

/** Parse a backend timestamp ("2026-07-26 03:12:45.123+00:00"). null if invalid. */
export function parseTimestamp(value: string | null): number | null {
  if (!value) return null
  const iso = value.includes('T') ? value : value.replace(' ', 'T')
  const ms = Date.parse(iso)
  return Number.isNaN(ms) ? null : ms
}

/**
 * Elapsed seconds for a task: created_at → now while active, created_at →
 * updated_at once terminal. null when timestamps are missing/implausible
 * (negative or > 48h) so the UI can hide the figure gracefully.
 */
export function taskElapsedSeconds(task: AsyncTask, nowMs: number): number | null {
  const start = parseTimestamp(task.createdAt)
  if (start === null) return null
  const end = isTerminal(task.status) ? parseTimestamp(task.updatedAt) : nowMs
  if (end === null) return null
  const sec = Math.floor((end - start) / 1000)
  if (sec < 0 || sec > 48 * 3600) return null
  return sec
}

export function formatElapsed(seconds: number): string {
  const h = Math.floor(seconds / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  const s = seconds % 60
  if (h > 0) return `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
  return `${m}:${String(s).padStart(2, '0')}`
}

const STATUS_LABELS: Record<string, string> = {
  pending: '排队中',
  running: '生成中',
  polishing: '润色中',
  completed: '已完成',
  failed: '失败',
  cancelled: '已取消',
  needs_review: '需复核',
}

export function statusLabel(status: string): string {
  return STATUS_LABELS[status] || status
}

const TYPE_LABELS: Record<string, string> = {
  chapter: '章节正文',
  outline_book: '全书大纲',
  outline_volume: '分卷大纲',
  outline_chapter: '章节大纲',
}

export function taskTypeLabel(taskType: string): string {
  return TYPE_LABELS[taskType] || taskType || '生成任务'
}
