'use client'

import { useState, useEffect, useCallback, useMemo } from 'react'
import { apiFetch } from '@/lib/api'

// v1.4 — LLM tier-routing matrix page.
// Consumes GET /api/llm-routing/matrix to show the effective tier routing
// across every (task_type, mode) pair, grouped by task_type.

interface MatrixRow {
  task_type: string
  mode: string
  prompt_id: string
  prompt_name: string
  endpoint_id: string | null
  endpoint_name: string | null
  endpoint_tier: string | null
  model_name: string | null
  model_tier: string | null
  effective_tier: string
  overridden: boolean
}

interface MatrixResponse {
  rows: MatrixRow[]
  total: number
  tier: string | null
  error?: string
}

const MODE_LABELS: Record<string, string> = { text: '文本', structured: '结构化(JSON)' }

// Matches settings/prompts pages (chunk-12 / chunk-13).
const TIER_OPTIONS = [
  { value: '', label: '全部 tier' },
  { value: 'flagship', label: 'Flagship' },
  { value: 'standard', label: 'Standard' },
  { value: 'small', label: 'Small' },
  { value: 'distill', label: 'Distill' },
  { value: 'embedding', label: 'Embedding' },
]
const TIER_BADGE_CLASS: Record<string, string> = {
  flagship: 'bg-purple-50 text-purple-700',
  standard: 'bg-blue-50 text-blue-700',
  small: 'bg-gray-100 text-gray-700',
  distill: 'bg-amber-50 text-amber-700',
  embedding: 'bg-emerald-50 text-emerald-700',
}

function TierBadge({ tier, testId }: { tier: string | null | undefined; testId?: string }) {
  if (!tier) {
    return (
      <span
        className="inline-flex items-center rounded-full bg-gray-100 px-2 py-0.5 text-xs text-gray-500"
        data-testid={testId}
      >
        —
      </span>
    )
  }
  const cls = TIER_BADGE_CLASS[tier] ?? 'bg-gray-100 text-gray-700'
  return (
    <span
      className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${cls}`}
      data-testid={testId}
    >
      {tier}
    </span>
  )
}

export default function LlmRoutingPage() {
  const [rows, setRows] = useState<MatrixRow[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [tierFilter, setTierFilter] = useState<string>('')

  const fetchMatrix = useCallback(async (tier: string) => {
    setLoading(true)
    setError(null)
    try {
      const qs = tier ? `?tier=${encodeURIComponent(tier)}` : ''
      const data = await apiFetch<MatrixResponse>(`/api/llm-routing/matrix${qs}`)
      setRows(Array.isArray(data?.rows) ? data.rows : [])
      setTotal(typeof data?.total === 'number' ? data.total : (data?.rows?.length ?? 0))
      if (data?.error) setError(data.error)
    } catch (e) {
      setRows([])
      setTotal(0)
      setError(e instanceof Error ? e.message : '加载路由矩阵失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchMatrix(tierFilter)
  }, [fetchMatrix, tierFilter])

  const overriddenCount = useMemo(() => rows.filter((r) => r.overridden).length, [rows])

  const tierCounts = useMemo(() => {
    const counts: Record<string, number> = {}
    for (const r of rows) {
      const t = r.effective_tier || 'unknown'
      counts[t] = (counts[t] ?? 0) + 1
    }
    return counts
  }, [rows])

  // Group rows by task_type while preserving the first-seen order.
  const grouped = useMemo(() => {
    const order: string[] = []
    const map = new Map<string, MatrixRow[]>()
    for (const r of rows) {
      if (!map.has(r.task_type)) {
        order.push(r.task_type)
        map.set(r.task_type, [])
      }
      map.get(r.task_type)!.push(r)
    }
    return order.map((task_type) => ({ task_type, rows: map.get(task_type)! }))
  }, [rows])

  return (
    <div className="mx-auto max-w-6xl p-6">
      <div className="mb-6">
        <h1 className="text-2xl font-semibold text-gray-900">LLM 路由矩阵</h1>
        <p className="mt-1 text-sm text-gray-500">
          按 task_type × mode 展示每个 prompt 的 endpoint / model / 生效 tier（prompt.model_tier ≫ endpoint.tier ≫ standard）。
        </p>
      </div>

      <div className="mb-4 flex flex-wrap items-center gap-3 rounded-lg border border-gray-200 bg-white p-4">
        <label className="flex items-center gap-2 text-sm text-gray-700">
          <span>过滤 tier：</span>
          <select
            data-testid="llm-routing-tier-filter"
            className="rounded-md border border-gray-300 px-2 py-1 text-sm"
            value={tierFilter}
            onChange={(e) => setTierFilter(e.target.value)}
          >
            {TIER_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>{o.label}</option>
            ))}
          </select>
        </label>

        <div className="ml-auto flex flex-wrap items-center gap-2 text-xs text-gray-600">
          <span className="rounded-full bg-gray-100 px-2 py-0.5">总数 {total}</span>
          <span className="rounded-full bg-amber-50 px-2 py-0.5 text-amber-700">覆盖 {overriddenCount}</span>
          {Object.entries(tierCounts).map(([tier, count]) => (
            <span
              key={tier}
              data-testid={`llm-routing-tier-chip-${tier}`}
              className={`rounded-full px-2 py-0.5 ${TIER_BADGE_CLASS[tier] ?? 'bg-gray-100 text-gray-700'}`}
            >
              {tier}: {count}
            </span>
          ))}
        </div>
      </div>

      {error && (
        <div
          data-testid="llm-routing-error"
          className="mb-4 rounded-md border border-red-200 bg-red-50 px-4 py-2 text-sm text-red-700"
        >
          {error}
        </div>
      )}

      {loading ? (
        <div className="rounded-lg border border-gray-200 bg-white p-8 text-center text-sm text-gray-500">
          加载中…
        </div>
      ) : rows.length === 0 ? (
        <div className="rounded-lg border border-gray-200 bg-white p-8 text-center text-sm text-gray-500">
          没有匹配的路由记录。
        </div>
      ) : (
        <div
          data-testid="llm-routing-matrix"
          className="overflow-hidden rounded-lg border border-gray-200 bg-white"
        >
          {grouped.map((group, gi) => (
            <div key={group.task_type} className={gi > 0 ? 'border-t border-gray-200' : ''}>
              <div className="bg-gray-50 px-4 py-2 text-sm font-medium text-gray-700">
                {group.task_type}
                <span className="ml-2 text-xs text-gray-400">({group.rows.length})</span>
              </div>
              <table className="w-full text-sm">
                <thead className="bg-white text-left text-xs uppercase tracking-wide text-gray-500">
                  <tr>
                    <th className="px-4 py-2 font-medium">Mode</th>
                    <th className="px-4 py-2 font-medium">Prompt</th>
                    <th className="px-4 py-2 font-medium">Endpoint (tier)</th>
                    <th className="px-4 py-2 font-medium">Model</th>
                    <th className="px-4 py-2 font-medium">Effective tier</th>
                  </tr>
                </thead>
                <tbody>
                  {group.rows.map((r) => (
                    <tr
                      key={`${r.task_type}:${r.mode}:${r.prompt_id}`}
                      data-testid="llm-routing-row"
                      className="border-t border-gray-100 align-top"
                    >
                      <td className="px-4 py-2 text-gray-700">{MODE_LABELS[r.mode] ?? r.mode}</td>
                      <td className="px-4 py-2">
                        <div className="font-medium text-gray-900">{r.prompt_name}</div>
                        <div className="text-xs text-gray-400">{r.prompt_id}</div>
                      </td>
                      <td className="px-4 py-2">
                        {r.endpoint_name ? (
                          <div className="flex items-center gap-2">
                            <span className="text-gray-700">{r.endpoint_name}</span>
                            <TierBadge tier={r.endpoint_tier} />
                          </div>
                        ) : (
                          <span className="text-gray-400">—</span>
                        )}
                      </td>
                      <td className="px-4 py-2 text-gray-700">
                        {r.model_name || <span className="text-gray-400">—</span>}
                      </td>
                      <td className="px-4 py-2">
                        <div className="flex items-center gap-2">
                          <TierBadge
                            tier={r.effective_tier}
                            testId="llm-routing-effective-tier"
                          />
                          {r.overridden && (
                            <span
                              className="text-xs font-semibold text-amber-600"
                              title="prompt.model_tier 覆盖了 endpoint.tier"
                            >
                              *
                            </span>
                          )}
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
