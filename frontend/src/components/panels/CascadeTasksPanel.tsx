'use client'

/**
 * v1.7 X5: cascade_tasks status panel.
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

function relativeTime(iso: string): string {
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
                  className="border-b border-gray-100 hover:bg-gray-50/50 align-top"
                  title={r.id}
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
    </div>
  )
}
