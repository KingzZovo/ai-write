'use client'

/**
 * v1.7 X5 / v1.7.1 Z2: cascade_tasks status panel.
 *
 * Reads the read-only API surface added in v1.7 X5
 * (`/api/projects/{pid}/cascade-tasks` + `/summary` + `/{tid}`).
 *
 * Shows queue-style table of upstream-fix tasks the cascade planner
 * enqueued when a chapter's evaluation fell below threshold:
 *   - status badge (pending/running/done/failed/skipped) with color
 *   - severity badge (high/critical)
 *   - target_entity_type (outline/character/world_rule/chapter)
 *   - source chapter (truncated id)
 *   - issue_summary (line-clamped)
 *   - created_at relative time
 *   - manual refresh button + auto-refresh every 15s while any row is
 *     pending or running
 *   - clickable rows open a detail modal showing the full task record
 *     (issue_summary, error_message, started_at/completed_at, parent task)
 *
 * NOT to be confused with the legacy `CascadePanel.tsx` which surfaces
 * a chapter-edit downstream-impact analysis & re-generate flow.
 */

import React, { useCallback, useEffect, useMemo, useState } from 'react'
import { apiFetch } from '@/lib/api'

export interface CascadeTaskRow {
  id: string
  project_id: string
  source_chapter_id: string
  source_evaluation_id: string
  target_entity_type: 'chapter' | 'outline' | 'character' | 'world_rule'
  target_entity_id: string
  severity: 'high' | 'critical'
  issue_summary: string | null
  status: 'pending' | 'running' | 'done' | 'failed' | 'skipped'
  parent_task_id: string | null
  attempt_count: number
  error_message: string | null
  created_at: string
  started_at: string | null
  completed_at: string | null
}

export interface CascadeTaskSummary {
  pending: number
  running: number
  done: number
  failed: number
  skipped: number
  total: number
}

interface CascadeTasksPanelProps {
  projectId: string
  chapterId?: string  // optional: scope to one chapter
  pollIntervalMs?: number  // default 15s when active rows exist
}

const STATUS_STYLES: Record<CascadeTaskRow['status'], string> = {
  pending: 'bg-gray-100 text-gray-700 border-gray-200',
  running: 'bg-blue-100 text-blue-700 border-blue-300',
  done: 'bg-green-100 text-green-700 border-green-200',
  failed: 'bg-red-100 text-red-700 border-red-200',
  skipped: 'bg-amber-50 text-amber-700 border-amber-200',
}

const SEVERITY_STYLES: Record<CascadeTaskRow['severity'], string> = {
  high: 'bg-orange-50 text-orange-700 border-orange-200',
  critical: 'bg-red-50 text-red-700 border-red-200',
}

function relativeTime(iso: string | null): string {
  if (!iso) return ''
  const t = new Date(iso).getTime()
  if (Number.isNaN(t)) return ''
  const diffSec = Math.round((Date.now() - t) / 1000)
  if (diffSec < 60) return `${diffSec}s ago`
  if (diffSec < 3600) return `${Math.floor(diffSec / 60)}m ago`
  if (diffSec < 86400) return `${Math.floor(diffSec / 3600)}h ago`
  return `${Math.floor(diffSec / 86400)}d ago`
}

function truncate(s: string | null, n: number): string {
  if (!s) return ''
  return s.length <= n ? s : s.slice(0, n) + '…'
}

function formatDuration(startedAt: string | null, completedAt: string | null): string {
  if (!startedAt) return '—'
  const startMs = new Date(startedAt).getTime()
  if (Number.isNaN(startMs)) return '—'
  const endMs = completedAt ? new Date(completedAt).getTime() : Date.now()
  if (Number.isNaN(endMs)) return '—'
  const sec = Math.max(0, Math.round((endMs - startMs) / 1000))
  if (sec < 60) return `${sec}s`
  if (sec < 3600) return `${Math.floor(sec / 60)}m ${sec % 60}s`
  return `${Math.floor(sec / 3600)}h ${Math.floor((sec % 3600) / 60)}m`
}

// ----------------------------------------------------------------
// Detail modal
// ----------------------------------------------------------------

function CascadeTaskDetailModal({
  projectId,
  task,
  onClose,
}: {
  projectId: string
  task: CascadeTaskRow
  onClose: () => void
}) {
  const [full, setFull] = useState<CascadeTaskRow>(task)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Re-fetch the row from /{task_id} for freshest data (covers running rows).
  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    apiFetch<CascadeTaskRow>(
      `/api/projects/${projectId}/cascade-tasks/${task.id}`,
    )
      .then((row) => {
        if (!cancelled) setFull(row)
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : 'Failed to load task detail')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [projectId, task.id])

  // Close on Escape
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [onClose])

  return (
    <div
      className="fixed inset-0 z-50 bg-black/40 flex items-center justify-center p-4"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
      aria-label="Cascade task detail"
    >
      <div
        className="bg-white rounded-lg shadow-xl max-w-2xl w-full max-h-[85vh] overflow-y-auto"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-3 border-b border-gray-200 sticky top-0 bg-white">
          <div className="flex items-center gap-2 min-w-0">
            <span
              className={`inline-block px-1.5 py-0.5 rounded border text-[10px] font-medium ${STATUS_STYLES[full.status]}`}
            >
              {full.status}
            </span>
            <span
              className={`inline-block px-1.5 py-0.5 rounded border text-[10px] font-medium ${SEVERITY_STYLES[full.severity]}`}
            >
              {full.severity}
            </span>
            <h3 className="text-sm font-semibold text-gray-900 truncate" title={full.id}>
              Cascade task · {full.id.slice(0, 8)}
            </h3>
            {loading && <span className="text-[11px] text-gray-400">refreshing…</span>}
          </div>
          <button
            onClick={onClose}
            className="text-gray-400 hover:text-gray-600 text-lg leading-none p-1"
            aria-label="Close"
          >
            ×
          </button>
        </div>

        {/* Body */}
        <div className="p-5 space-y-4">
          {error && (
            <div className="text-xs text-red-600 bg-red-50 border border-red-200 rounded px-2 py-1">
              {error}
            </div>
          )}

          {/* Field grid */}
          <dl className="grid grid-cols-3 gap-x-4 gap-y-2 text-xs">
            <dt className="text-gray-500">Target entity</dt>
            <dd className="col-span-2 text-gray-800 font-mono">
              {full.target_entity_type} · {full.target_entity_id}
            </dd>

            <dt className="text-gray-500">Source chapter</dt>
            <dd className="col-span-2 text-gray-800 font-mono">{full.source_chapter_id}</dd>

            <dt className="text-gray-500">Source evaluation</dt>
            <dd className="col-span-2 text-gray-800 font-mono">{full.source_evaluation_id}</dd>

            <dt className="text-gray-500">Parent task</dt>
            <dd className="col-span-2 text-gray-800 font-mono">
              {full.parent_task_id || <span className="text-gray-400">—</span>}
            </dd>

            <dt className="text-gray-500">Attempts</dt>
            <dd className="col-span-2 text-gray-800">{full.attempt_count}</dd>

            <dt className="text-gray-500">Created</dt>
            <dd className="col-span-2 text-gray-800">
              {full.created_at} <span className="text-gray-400">({relativeTime(full.created_at)})</span>
            </dd>

            <dt className="text-gray-500">Started</dt>
            <dd className="col-span-2 text-gray-800">
              {full.started_at || <span className="text-gray-400">not started</span>}
            </dd>

            <dt className="text-gray-500">Completed</dt>
            <dd className="col-span-2 text-gray-800">
              {full.completed_at || <span className="text-gray-400">not completed</span>}
            </dd>

            <dt className="text-gray-500">Duration</dt>
            <dd className="col-span-2 text-gray-800">
              {formatDuration(full.started_at, full.completed_at)}
            </dd>
          </dl>

          {/* Issue summary */}
          <div>
            <div className="text-xs text-gray-500 mb-1">Issue summary</div>
            <div className="text-sm text-gray-800 bg-gray-50 border border-gray-200 rounded p-2 whitespace-pre-wrap">
              {full.issue_summary || <span className="text-gray-400">—</span>}
            </div>
          </div>

          {/* Error message */}
          {full.error_message && (
            <div>
              <div className="text-xs text-red-600 mb-1">Error</div>
              <div className="text-sm text-red-700 bg-red-50 border border-red-200 rounded p-2 whitespace-pre-wrap">
                {full.error_message}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

// ----------------------------------------------------------------
// Main panel
// ----------------------------------------------------------------

export function CascadeTasksPanel({
  projectId,
  chapterId,
  pollIntervalMs = 15000,
}: CascadeTasksPanelProps) {
  const [rows, setRows] = useState<CascadeTaskRow[]>([])
  const [summary, setSummary] = useState<CascadeTaskSummary | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [statusFilter, setStatusFilter] = useState<string>('')
  const [openTask, setOpenTask] = useState<CascadeTaskRow | null>(null)

  const baseQuery = useMemo(() => {
    const params = new URLSearchParams()
    if (chapterId) params.set('chapter_id', chapterId)
    if (statusFilter) params.set('status', statusFilter)
    params.set('limit', '100')
    const qs = params.toString()
    return qs ? `?${qs}` : ''
  }, [chapterId, statusFilter])

  const summaryQuery = useMemo(() => {
    const params = new URLSearchParams()
    if (chapterId) params.set('chapter_id', chapterId)
    const qs = params.toString()
    return qs ? `?${qs}` : ''
  }, [chapterId])

  const refresh = useCallback(async () => {
    if (!projectId) return
    setLoading(true)
    setError(null)
    try {
      const [list, sum] = await Promise.all([
        apiFetch<CascadeTaskRow[]>(
          `/api/projects/${projectId}/cascade-tasks${baseQuery}`,
        ),
        apiFetch<CascadeTaskSummary>(
          `/api/projects/${projectId}/cascade-tasks/summary${summaryQuery}`,
        ),
      ])
      setRows(list)
      setSummary(sum)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load cascade tasks')
    } finally {
      setLoading(false)
    }
  }, [projectId, baseQuery, summaryQuery])

  useEffect(() => {
    refresh()
  }, [refresh])

  // Auto-refresh while any row is still active.
  const hasActive = rows.some((r) => r.status === 'pending' || r.status === 'running')
  useEffect(() => {
    if (!hasActive || pollIntervalMs <= 0) return
    const id = window.setInterval(refresh, pollIntervalMs)
    return () => window.clearInterval(id)
  }, [hasActive, pollIntervalMs, refresh])

  return (
    <div className="space-y-3">
      {/* Header + summary chips */}
      <div className="flex items-center justify-between flex-wrap gap-2">
        <h3 className="text-sm font-semibold text-gray-900">
          Cascade Tasks
          {chapterId && (
            <span className="ml-2 text-xs font-normal text-gray-500">
              · chapter {chapterId.slice(0, 8)}
            </span>
          )}
        </h3>
        <div className="flex items-center gap-2">
          <select
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value)}
            className="text-xs border border-gray-200 rounded px-2 py-1 bg-white"
            aria-label="Filter by status"
          >
            <option value="">all status</option>
            <option value="pending">pending</option>
            <option value="running">running</option>
            <option value="done">done</option>
            <option value="failed">failed</option>
            <option value="skipped">skipped</option>
          </select>
          <button
            onClick={refresh}
            disabled={loading}
            className="text-xs px-2 py-1 border border-gray-200 rounded bg-white hover:bg-gray-50 disabled:opacity-50"
          >
            {loading ? '…' : 'Refresh'}
          </button>
        </div>
      </div>

      {summary && (
        <div className="flex flex-wrap gap-1.5 text-[11px]">
          <span className="px-2 py-0.5 rounded bg-gray-50 text-gray-700">
            total <b>{summary.total}</b>
          </span>
          <span className="px-2 py-0.5 rounded bg-gray-100 text-gray-700">
            pending <b>{summary.pending}</b>
          </span>
          <span className="px-2 py-0.5 rounded bg-blue-100 text-blue-700">
            running <b>{summary.running}</b>
          </span>
          <span className="px-2 py-0.5 rounded bg-green-100 text-green-700">
            done <b>{summary.done}</b>
          </span>
          <span className="px-2 py-0.5 rounded bg-red-100 text-red-700">
            failed <b>{summary.failed}</b>
          </span>
          <span className="px-2 py-0.5 rounded bg-amber-50 text-amber-700">
            skipped <b>{summary.skipped}</b>
          </span>
        </div>
      )}

      {error && (
        <div className="text-xs text-red-600 bg-red-50 border border-red-200 rounded px-2 py-1">
          {error}
        </div>
      )}

      {/* Table */}
      {rows.length === 0 ? (
        <p className="text-xs text-gray-400 px-2 py-4 text-center">
          {loading ? 'Loading…' : 'No cascade tasks for this scope.'}
        </p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-xs border-collapse">
            <thead>
              <tr className="bg-gray-50 border-b border-gray-200 text-left">
                <th className="px-2 py-1.5 font-medium text-gray-600">Status</th>
                <th className="px-2 py-1.5 font-medium text-gray-600">Sev.</th>
                <th className="px-2 py-1.5 font-medium text-gray-600">Target</th>
                <th className="px-2 py-1.5 font-medium text-gray-600">Source ch.</th>
                <th className="px-2 py-1.5 font-medium text-gray-600">Issue</th>
                <th className="px-2 py-1.5 font-medium text-gray-600">Attempts</th>
                <th className="px-2 py-1.5 font-medium text-gray-600">Created</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr
                  key={r.id}
                  className="border-b border-gray-100 hover:bg-blue-50/40 align-top cursor-pointer"
                  title={`Click for detail · id=${r.id}`}
                  onClick={() => setOpenTask(r)}
                >
                  <td className="px-2 py-1.5">
                    <span
                      className={`inline-block px-1.5 py-0.5 rounded border text-[10px] font-medium ${STATUS_STYLES[r.status]}`}
                    >
                      {r.status}
                    </span>
                  </td>
                  <td className="px-2 py-1.5">
                    <span
                      className={`inline-block px-1.5 py-0.5 rounded border text-[10px] font-medium ${SEVERITY_STYLES[r.severity]}`}
                    >
                      {r.severity}
                    </span>
                  </td>
                  <td className="px-2 py-1.5 text-gray-700 whitespace-nowrap">
                    {r.target_entity_type}
                    <span className="text-gray-400">
                      {' · '}{r.target_entity_id.slice(0, 8)}
                    </span>
                  </td>
                  <td className="px-2 py-1.5 text-gray-500 font-mono whitespace-nowrap">
                    {r.source_chapter_id.slice(0, 8)}
                  </td>
                  <td className="px-2 py-1.5 text-gray-700 max-w-md">
                    <span className="line-clamp-2">{truncate(r.issue_summary, 240)}</span>
                    {r.error_message && (
                      <div className="text-red-600 text-[10px] mt-0.5">
                        ! {truncate(r.error_message, 200)}
                      </div>
                    )}
                  </td>
                  <td className="px-2 py-1.5 text-gray-600 text-center">{r.attempt_count}</td>
                  <td className="px-2 py-1.5 text-gray-500 whitespace-nowrap">
                    {relativeTime(r.created_at)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {openTask && (
        <CascadeTaskDetailModal
          projectId={projectId}
          task={openTask}
          onClose={() => setOpenTask(null)}
        />
      )}
    </div>
  )
}
