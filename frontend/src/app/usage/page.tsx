'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import { apiFetch, ApiError } from '@/lib/api'
import {
  CACHE_UNKNOWN_HINT,
  CACHE_UNKNOWN_TEXT,
  GROUP_BY_HEADING,
  GROUP_BY_OPTIONS,
  barPercent,
  formatCacheRate,
  formatExact,
  formatTokens,
  usageBudgetPath,
  usageSummaryPath,
  type ChapterUsageBudget,
  type UsageGroupBy,
  type UsageSummary,
} from '@/lib/usageTypes'

interface ProjectBrief {
  id: string
  title: string
}

interface ChapterBrief {
  id: string
  title?: string | null
  chapter_idx?: number | null
}

const DAY_OPTIONS = [7, 30, 90]
const LIMIT_OPTIONS = [50_000, 100_000, 200_000, 500_000]

function describeError(err: unknown): string {
  if (err instanceof ApiError) {
    const reason =
      err.status === 404 ? '接口不存在(用量服务可能尚未上线)'
        : err.status === 403 ? '没有权限查看该书的用量'
          : err.status === 401 ? '登录状态已失效,请重新登录'
            : err.status >= 500 ? '服务端错误'
              : '请求被拒绝'
    const detail = err.message && err.message !== 'API Error' ? `:${err.message}` : ''
    return `${reason}${detail}(HTTP ${err.status})`
  }
  if (err instanceof Error) return err.message || '网络错误,请稍后重试'
  return '未知错误'
}

function chapterLabel(chapter: ChapterBrief): string {
  return chapter.title || chapter.id
}

export default function UsagePage() {
  const [projects, setProjects] = useState<ProjectBrief[]>([])
  const [projectsError, setProjectsError] = useState<string | null>(null)
  const [projectsLoaded, setProjectsLoaded] = useState(false)

  const [projectId, setProjectId] = useState('')
  const [chapters, setChapters] = useState<ChapterBrief[]>([])
  const [chapterId, setChapterId] = useState('')

  const [groupBy, setGroupBy] = useState<UsageGroupBy>('chapter')
  const [days, setDays] = useState(30)
  const [limitTokens, setLimitTokens] = useState(100_000)

  const [summary, setSummary] = useState<UsageSummary | null>(null)
  const [summaryError, setSummaryError] = useState<string | null>(null)
  const [summaryLoading, setSummaryLoading] = useState(false)

  const [budget, setBudget] = useState<ChapterUsageBudget | null>(null)
  const [budgetError, setBudgetError] = useState<string | null>(null)
  const [budgetLoading, setBudgetLoading] = useState(false)

  // 项目列表(现有接口 /api/projects)。
  useEffect(() => {
    let cancelled = false
    apiFetch<{ projects: ProjectBrief[] }>('/api/projects')
      .then((r) => {
        if (cancelled) return
        const list = Array.isArray(r?.projects) ? r.projects : []
        setProjects(list)
        setProjectId((prev) => {
          if (prev && list.some((p) => p.id === prev)) return prev
          return list[0]?.id ?? ''
        })
      })
      .catch((err) => {
        if (!cancelled) setProjectsError(describeError(err))
      })
      .finally(() => {
        if (!cancelled) setProjectsLoaded(true)
      })
    return () => { cancelled = true }
  }, [])

  const chaptersToken = useRef(0)
  const summaryToken = useRef(0)
  const budgetToken = useRef(0)

  // 章节列表(仅用于筛选/单章预算;失败时退化为"全书",不阻断页面)。
  const loadChapters = useCallback(async () => {
    const token = ++chaptersToken.current
    if (!projectId) {
      setChapters([])
      return
    }
    try {
      const r = await apiFetch<ChapterBrief[]>(
        `/api/projects/${encodeURIComponent(projectId)}/chapters?lightweight=true`,
      )
      if (token === chaptersToken.current) setChapters(Array.isArray(r) ? r : [])
    } catch {
      if (token === chaptersToken.current) setChapters([])
    }
  }, [projectId])

  useEffect(() => {
    void loadChapters()
  }, [loadChapters])

  // 用量汇总:全书,或某一章(chapter_id)。请求令牌忽略迟到响应,避免旧响应覆盖新选择。
  const loadSummary = useCallback(async () => {
    if (!projectId) return
    const token = ++summaryToken.current
    setSummaryLoading(true)
    setSummaryError(null)
    try {
      const r = await apiFetch<UsageSummary>(
        usageSummaryPath(projectId, { groupBy, days, chapterId: chapterId || undefined }),
      )
      if (token === summaryToken.current) setSummary(r)
    } catch (err) {
      if (token !== summaryToken.current) return
      setSummary(null)
      setSummaryError(describeError(err))
    } finally {
      if (token === summaryToken.current) setSummaryLoading(false)
    }
  }, [projectId, groupBy, days, chapterId])

  useEffect(() => {
    void loadSummary()
  }, [loadSummary])

  // 单章预算:仅在选择具体章节时请求。
  const loadBudget = useCallback(async () => {
    const token = ++budgetToken.current
    if (!chapterId) {
      setBudget(null)
      setBudgetError(null)
      setBudgetLoading(false)
      return
    }
    setBudgetLoading(true)
    setBudgetError(null)
    try {
      const r = await apiFetch<ChapterUsageBudget>(usageBudgetPath(chapterId, limitTokens))
      if (token === budgetToken.current) setBudget(r)
    } catch (err) {
      if (token !== budgetToken.current) return
      setBudget(null)
      setBudgetError(describeError(err))
    } finally {
      if (token === budgetToken.current) setBudgetLoading(false)
    }
  }, [chapterId, limitTokens])

  useEffect(() => {
    void loadBudget()
  }, [loadBudget])

  // 手动刷新:进入页面只加载一次,之后仅由按钮触发。
  const refreshAll = useCallback(() => {
    void loadSummary()
    void loadBudget()
  }, [loadSummary, loadBudget])

  const totals = summary?.totals ?? null
  const groups = Array.isArray(summary?.groups) ? summary!.groups : []
  const maxTotal = groups.reduce((max, g) => (g.total_tokens > max ? g.total_tokens : max), 0)
  const selectedChapter = chapters.find((c) => c.id === chapterId) ?? null
  const cacheHint = formatCacheRate(totals?.input_tokens, totals?.cached_input_tokens)
  const budgetPercent = barPercent(budget?.used_tokens, budget?.limit_tokens ?? limitTokens)
  // 2026-11-17: 供应商不回传 usage 时后端按字符数估算,这里必须让作者看见,
  // 不能把估算值当成真实计量(费用以厂商账单为准)。
  const estimatedCalls = totals?.estimated_calls ?? 0
  const estimatedNote =
    totals && estimatedCalls > 0
      ? `其中 ${estimatedCalls.toLocaleString()} 次调用没有拿到供应商回传的用量,已按字符数估算(${formatTokens(
          totals.estimated_tokens ?? 0,
        )} 计入总量)。估算值仅供趋势参考,费用以厂商账单为准。`
      : ''

  return (
    <div className="pt-14 px-4 md:px-8 max-w-7xl mx-auto pb-12">
      <div className="flex flex-wrap items-end justify-between gap-3 mb-4">
        <div>
          <h1 className="text-2xl font-bold">AI 用量</h1>
          <p className="text-xs text-gray-500 mt-1">
            全书 / 单章的 Token 消耗、缓存命中率与分布。进入页面加载一次,需要最新数据请点“刷新”。
          </p>
        </div>
        <button
          onClick={() => refreshAll()}
          disabled={summaryLoading || budgetLoading}
          className="px-3 py-1 text-sm bg-gray-100 rounded hover:bg-gray-200 disabled:opacity-50"
        >
          {summaryLoading ? '加载中…' : '刷新'}
        </button>
      </div>

      <div className="flex flex-wrap gap-3 mb-4 text-sm">
        <select
          aria-label="项目"
          value={projectId}
          onChange={(e) => {
            setProjectId(e.target.value)
            setChapterId('')
          }}
          className="px-2 py-1 border rounded max-w-[16rem]"
          disabled={projects.length === 0}
        >
          {projects.length === 0 && <option value="">-- 暂无项目 --</option>}
          {projects.map((p) => (
            <option key={p.id} value={p.id}>{p.title || p.id}</option>
          ))}
        </select>

        <select
          aria-label="章节"
          value={chapterId}
          onChange={(e) => setChapterId(e.target.value)}
          className="px-2 py-1 border rounded max-w-[16rem]"
          disabled={!projectId || chapters.length === 0}
        >
          <option value="">全书</option>
          {chapters.map((c) => (
            <option key={c.id} value={c.id}>{chapterLabel(c)}</option>
          ))}
        </select>

        <select
          aria-label="时间范围"
          value={days}
          onChange={(e) => setDays(Number(e.target.value))}
          className="px-2 py-1 border rounded"
        >
          {DAY_OPTIONS.map((d) => (
            <option key={d} value={d}>最近 {d} 天</option>
          ))}
        </select>
      </div>

      {projectsError && (
        <div data-testid="usage-projects-error" className="mb-4 p-3 rounded border border-red-200 bg-red-50 text-sm text-red-700">
          项目列表加载失败:{projectsError}
        </div>
      )}

      {projectsLoaded && !projectsError && projects.length === 0 && (
        <p className="mb-4 text-sm text-gray-400 text-center py-8">暂无项目,请先创建一本书。</p>
      )}

      {summaryError && (
        <div data-testid="usage-summary-error" className="mb-4 p-3 rounded border border-red-200 bg-red-50 text-sm text-red-700">
          用量数据加载失败:{summaryError}
          <button
            onClick={() => refreshAll()}
            className="ml-3 px-2 py-0.5 text-xs bg-white rounded border border-red-200"
          >
            重试
          </button>
        </div>
      )}

      {projectId && (
        <>
          {/* 总览 */}
          <section className="grid grid-cols-2 md:grid-cols-5 gap-3 mb-6">
            <StatCard
              testId="usage-stat-total"
              label="总 Tokens"
              value={formatTokens(totals?.total_tokens)}
              hint={totals ? `精确 ${formatExact(totals.total_tokens)}` : '暂无数据'}
            />
            <StatCard
              testId="usage-stat-input"
              label="输入 Tokens"
              value={formatTokens(totals?.input_tokens)}
              hint={totals ? `其中缓存 ${formatTokens(totals.cached_input_tokens)}` : '暂无数据'}
            />
            <StatCard
              testId="usage-stat-output"
              label="输出 Tokens"
              value={formatTokens(totals?.output_tokens)}
              hint={totals ? `精确 ${formatExact(totals.output_tokens)}` : '暂无数据'}
            />
            <StatCard
              testId="usage-stat-cache"
              label="缓存命中率"
              value={cacheHint}
              hint={CACHE_UNKNOWN_HINT}
              tone={cacheHint === CACHE_UNKNOWN_TEXT ? 'muted' : 'default'}
            />
            <StatCard
              testId="usage-stat-calls"
              label="调用次数"
              value={formatExact(totals?.calls)}
              hint={totals ? `最近 ${days} 天` : '暂无数据'}
            />
          </section>

          {estimatedNote && (
            <p
              data-testid="usage-estimated-note"
              className="text-xs text-amber-800 bg-amber-50 border border-amber-200 rounded px-3 py-2 mb-4"
            >
              {estimatedNote}
            </p>
          )}

          {/* 分组切换 */}
          <div className="flex flex-wrap items-center gap-2 mb-3">
            <span className="text-xs text-gray-500 mr-1">分组</span>
            {GROUP_BY_OPTIONS.map((option) => (
              <button
                key={option.value}
                onClick={() => setGroupBy(option.value)}
                aria-pressed={groupBy === option.value}
                data-testid={`usage-group-tab-${option.value}`}
                className={`px-3 py-1 rounded text-sm border ${
                  groupBy === option.value
                    ? 'border-blue-500 bg-blue-50 text-blue-700'
                    : 'border-gray-200 bg-white text-gray-600 hover:bg-gray-50'
                }`}
              >
                {option.label}
              </button>
            ))}
          </div>

          {/* 分组表格 */}
          <section className="bg-white rounded border border-gray-200 p-4 mb-6">
            <div className="flex flex-wrap items-center justify-between gap-2 mb-2">
              <h2 className="font-semibold">{GROUP_BY_HEADING[groupBy]}</h2>
              <span className="text-xs text-gray-500">
                共 {groups.length} 项 · 已按总 Tokens 降序
              </span>
            </div>
            <p className="text-xs text-gray-400 mb-3">
              total_tokens = 输入 + 输出;{CACHE_UNKNOWN_HINT}。
            </p>

            {summaryLoading && !summary ? (
              <p className="text-sm text-gray-400 text-center py-8">加载中…</p>
            ) : groups.length === 0 ? (
              <p data-testid="usage-empty" className="text-sm text-gray-400 text-center py-8">
                最近 {days} 天没有用量记录
              </p>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-xs md:text-sm">
                  <thead className="text-gray-500 text-left border-b border-gray-200">
                    <tr>
                      <th className="py-2 pr-3 font-medium">
                        {groupBy === 'chapter' ? '章节' : groupBy === 'task_type' ? '步骤' : groupBy === 'model' ? '模型' : '日期'}
                      </th>
                      <th className="py-2 pr-3 font-medium text-right">调用</th>
                      <th className="py-2 pr-3 font-medium text-right">输入</th>
                      <th className="py-2 pr-3 font-medium text-right">缓存</th>
                      <th className="py-2 pr-3 font-medium text-right">输出</th>
                      <th className="py-2 font-medium">总 Tokens(占比)</th>
                    </tr>
                  </thead>
                  <tbody>
                    {groups.map((group) => {
                      const percent = barPercent(group.total_tokens, maxTotal)
                      const rate = formatCacheRate(group.input_tokens, group.cached_input_tokens)
                      return (
                        <tr key={group.key} className="border-b border-gray-100 last:border-0">
                          <td className="py-2 pr-3 text-gray-800 truncate max-w-[18rem]" title={group.label || group.key}>
                            {group.label || group.key}
                          </td>
                          <td className="py-2 pr-3 text-right text-gray-600 tabular-nums">{formatExact(group.calls)}</td>
                          <td className="py-2 pr-3 text-right text-gray-600 tabular-nums">{formatTokens(group.input_tokens)}</td>
                          <td
                            className={`py-2 pr-3 text-right tabular-nums ${rate === CACHE_UNKNOWN_TEXT ? 'text-gray-300' : 'text-green-700'}`}
                            title={rate === CACHE_UNKNOWN_TEXT ? CACHE_UNKNOWN_HINT : `缓存命中率 ${rate}`}
                          >
                            {rate}
                          </td>
                          <td className="py-2 pr-3 text-right text-gray-600 tabular-nums">{formatTokens(group.output_tokens)}</td>
                          <td className="py-2">
                            <div className="flex items-center gap-2 min-w-[10rem]">
                              <div className="flex-1 h-2 bg-gray-100 rounded overflow-hidden">
                                <div className="h-full bg-blue-500 rounded" style={{ width: `${percent}%` }} />
                              </div>
                              <span className="tabular-nums text-gray-800 w-20 text-right" title={`精确 ${formatExact(group.total_tokens)}`}>
                                {formatTokens(group.total_tokens)}
                              </span>
                            </div>
                          </td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          {/* 单章预算 */}
          {chapterId && (
            <section data-testid="usage-budget" className="bg-white rounded border border-gray-200 p-4">
              <div className="flex flex-wrap items-center justify-between gap-2 mb-3">
                <h2 className="font-semibold">
                  单章预算 · {selectedChapter ? chapterLabel(selectedChapter) : chapterId}
                </h2>
                <select
                  value={limitTokens}
                  onChange={(e) => setLimitTokens(Number(e.target.value))}
                  className="px-2 py-1 text-xs border rounded"
                  aria-label="预算上限"
                >
                  {LIMIT_OPTIONS.map((v) => (
                    <option key={v} value={v}>上限 {formatTokens(v)} tokens</option>
                  ))}
                </select>
              </div>

              {budgetError ? (
                <div data-testid="usage-budget-error" className="p-3 rounded border border-red-200 bg-red-50 text-sm text-red-700">
                  单章预算加载失败:{budgetError}
                </div>
              ) : budgetLoading && !budget ? (
                <p className="text-sm text-gray-400 text-center py-6">加载中…</p>
              ) : !budget ? (
                <p className="text-sm text-gray-400 text-center py-6">暂无该章用量</p>
              ) : (
                <div className="space-y-3">
                  <div className="flex items-center gap-3">
                    <div className="flex-1 h-2 bg-gray-100 rounded overflow-hidden">
                      <div
                        className={`h-full rounded ${budget.exceeded ? 'bg-red-500' : 'bg-blue-500'}`}
                        style={{ width: `${budgetPercent}%` }}
                      />
                    </div>
                    <span className="text-xs text-gray-600 tabular-nums">
                      {formatTokens(budget.used_tokens)} / {formatTokens(budget.limit_tokens)}
                    </span>
                    {budget.exceeded && (
                      <span data-testid="usage-budget-exceeded" className="text-xs text-red-600 font-medium">已超限</span>
                    )}
                  </div>
                  <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-xs">
                    <div>
                      <div className="text-gray-500">输入</div>
                      <div className="text-gray-800 tabular-nums">{formatTokens(budget.input_tokens)}</div>
                    </div>
                    <div>
                      <div className="text-gray-500">缓存</div>
                      <div className={`tabular-nums ${formatCacheRate(budget.input_tokens, budget.cached_input_tokens) === CACHE_UNKNOWN_TEXT ? 'text-gray-300' : 'text-green-700'}`}>
                        {formatCacheRate(budget.input_tokens, budget.cached_input_tokens)}
                      </div>
                    </div>
                    <div>
                      <div className="text-gray-500">输出</div>
                      <div className="text-gray-800 tabular-nums">{formatTokens(budget.output_tokens)}</div>
                    </div>
                    <div>
                      <div className="text-gray-500">调用次数</div>
                      <div className="text-gray-800 tabular-nums">{formatExact(budget.calls)}</div>
                    </div>
                  </div>
                  <div>
                    <h3 className="text-sm font-semibold mb-1">按步骤</h3>
                    {budget.by_step.length === 0 ? (
                      <p className="text-xs text-gray-400">暂无步骤数据</p>
                    ) : (
                      <ul className="text-xs divide-y divide-gray-100">
                        {budget.by_step.map((step) => (
                          <li key={step.task_type} className="flex justify-between py-1">
                            <span className="text-gray-700">{step.task_type}</span>
                            <span className="text-gray-500 tabular-nums">
                              {formatExact(step.calls)} 次 · {formatTokens(step.total_tokens)}
                            </span>
                          </li>
                        ))}
                      </ul>
                    )}
                  </div>
                </div>
              )}
            </section>
          )}
        </>
      )}
    </div>
  )
}

function StatCard({
  testId,
  label,
  value,
  hint,
  tone = 'default',
}: {
  testId: string
  label: string
  value: string
  hint: string
  tone?: 'default' | 'muted'
}) {
  return (
    <div data-testid={testId} className="bg-white rounded border border-gray-200 p-3">
      <div className="text-xs text-gray-500">{label}</div>
      <div className={`text-xl font-semibold mt-1 tabular-nums ${tone === 'muted' ? 'text-gray-400' : 'text-gray-900'}`}>
        {value}
      </div>
      <div className="text-[10px] text-gray-400 mt-1 leading-tight">{hint}</div>
    </div>
  )
}