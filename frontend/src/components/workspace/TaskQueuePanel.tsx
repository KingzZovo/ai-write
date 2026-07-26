'use client'

// PR-WS-QUEUE (2026-07-26): presentational list for the 任务队列 card in the
// 写作中枢 tab. State/polling lives in useAsyncTaskQueue (DesktopWorkspace).

import { useEffect, useState } from 'react'
import { Inbox } from 'lucide-react'
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

// Dot indicator colors matching the badges above (visual only).
const STATUS_DOTS: Record<string, string> = {
  pending: 'bg-gray-400',
  running: 'bg-blue-500',
  polishing: 'bg-violet-500',
  completed: 'bg-green-500',
  failed: 'bg-red-500',
  cancelled: 'bg-gray-400',
  needs_review: 'bg-amber-500',
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
      <div className="flex flex-col items-center gap-1 py-4 text-center">
        <Inbox className="h-5 w-5 text-gray-300" aria-hidden />
        <p className="text-xs font-medium text-gray-400">暂无后台任务</p>
        <p className="text-[11px] leading-relaxed text-gray-400">
          点「后台生成本章」提交任务后，可离开页面稍后回来查看进度。
        </p>
      </div>
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
            className={`rounded-md border border-gray-200 bg-white px-2.5 py-2 text-xs transition-colors duration-150 hover:border-gray-300 ${
              t.status === 'cancelled' ? 'opacity-60' : ''
            }`}
          >
            <div className="flex items-center justify-between gap-2">
              <div className="flex min-w-0 items-center gap-1.5">
                <span className="truncate font-medium text-gray-800">
                  {taskTypeLabel(t.taskType)}
                </span>
                <span
                  className={`inline-flex shrink-0 items-center gap-1 rounded-full px-1.5 py-0.5 text-[10px] font-medium ${
                    STATUS_BADGES[t.status] || STATUS_BADGES.pending
                  }`}
                >
                  <span
                    aria-hidden
                    className={`h-1 w-1 shrink-0 rounded-full ${
                      STATUS_DOTS[t.status] || STATUS_DOTS.pending
                    } ${!isTerminal(t.status) ? 'motion-safe:animate-pulse' : ''}`}
                  />
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
            {!isTerminal(t.status) && (
              <div className="mt-1.5 h-1 overflow-hidden rounded-full bg-gray-100">
                <div className="h-full w-full rounded-full bg-gradient-to-r from-blue-200 via-blue-400 to-blue-200 motion-safe:animate-pulse" />
              </div>
            )}
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
                    className="rounded-md border border-transparent px-2 py-0.5 text-[11px] text-red-600 transition-colors duration-150 hover:border-red-200 hover:bg-red-50"
                  >
                    取消
                  </button>
                )}
                {showRefresh && (
                  <button
                    type="button"
                    onClick={() => onRefreshChapter(t.chapterId!)}
                    className="rounded-md border border-emerald-300 bg-emerald-50 px-2 py-0.5 text-[11px] font-medium text-emerald-700 transition-colors duration-150 hover:bg-emerald-100"
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
