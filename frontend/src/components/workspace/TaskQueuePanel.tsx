'use client'

// PR-WS-QUEUE (2026-07-26): presentational list for the 任务队列 card in the
// 写作中枢 tab. State/polling lives in useAsyncTaskQueue (DesktopWorkspace).

import { useEffect, useState } from 'react'
import {
  type AsyncTask,
  formatElapsed,
  hasActiveTask,
  isTerminal,
  statusLabel,
  taskElapsedSeconds,
  taskTypeLabel,
} from './asyncTaskQueue'

const STATUS_BADGES: Record<string, string> = {
  pending: 'bg-gray-100 text-gray-600',
  running: 'bg-blue-100 text-blue-700',
  polishing: 'bg-violet-100 text-violet-700',
  completed: 'bg-green-100 text-green-700',
  failed: 'bg-red-100 text-red-700',
  cancelled: 'bg-gray-100 text-gray-500',
  needs_review: 'bg-amber-100 text-amber-700',
}

export function TaskQueuePanel({
  tasks,
  cancelUnavailable,
  onCancel,
  onRefreshChapter,
}: {
  tasks: AsyncTask[]
  cancelUnavailable: boolean
  onCancel: (taskId: string) => void
  onRefreshChapter: (chapterId: string) => void
}) {
  const active = hasActiveTask(tasks)
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!active) return
    const id = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(id)
  }, [active])

  if (tasks.length === 0) {
    return (
      <p className="text-xs leading-relaxed text-gray-400">
        暂无后台任务。点「后台生成本章」提交任务后，可离开页面稍后回来查看进度。
      </p>
    )
  }

  return (
    <div className="space-y-1.5">
      {tasks.map((t) => {
        const elapsed = taskElapsedSeconds(t, now)
        const hint = !isTerminal(t.status) && t.progressText ? t.progressText.slice(-60) : ''
        const showCancel = !isTerminal(t.status) && !cancelUnavailable
        const showRefresh =
          (t.status === 'completed' || t.status === 'needs_review') &&
          t.taskType === 'chapter' &&
          Boolean(t.chapterId)
        return (
          <div
            key={t.taskId}
            className="rounded-md border border-gray-200 bg-white px-2.5 py-2 text-xs"
          >
            <div className="flex items-center justify-between gap-2">
              <div className="flex min-w-0 items-center gap-1.5">
                <span className="truncate font-medium text-gray-800">
                  {taskTypeLabel(t.taskType)}
                </span>
                <span
                  className={`shrink-0 rounded px-1.5 py-0.5 text-[10px] font-medium ${
                    STATUS_BADGES[t.status] || STATUS_BADGES.pending
                  }`}
                >
                  {statusLabel(t.status)}
                </span>
              </div>
              <span className="shrink-0 tabular-nums text-gray-400">
                {typeof t.charCount === 'number' && t.charCount > 0
                  ? `${t.charCount.toLocaleString()} 字`
                  : ''}
                {elapsed !== null
                  ? `${t.charCount ? ' · ' : ''}${formatElapsed(elapsed)}`
                  : ''}
              </span>
            </div>
            {hint && <p className="mt-1 truncate text-[11px] text-gray-500">…{hint}</p>}
            {t.errorMessage && (
              <p className="mt-1 break-all text-[11px] leading-relaxed text-red-600">
                {t.errorMessage}
              </p>
            )}
            {(showCancel || showRefresh) && (
              <div className="mt-1.5 flex items-center gap-2">
                {showCancel && (
                  <button
                    type="button"
                    onClick={() => onCancel(t.taskId)}
                    className="rounded border border-gray-300 bg-white px-2 py-0.5 text-[11px] text-gray-600 hover:bg-gray-50"
                  >
                    取消
                  </button>
                )}
                {showRefresh && (
                  <button
                    type="button"
                    onClick={() => onRefreshChapter(t.chapterId!)}
                    className="rounded border border-emerald-300 bg-emerald-50 px-2 py-0.5 text-[11px] text-emerald-700 hover:bg-emerald-100"
                  >
                    刷新章节
                  </button>
                )}
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}
