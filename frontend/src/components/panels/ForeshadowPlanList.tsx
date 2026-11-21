'use client'

import { useEffect, useState } from 'react'
import { apiFetch } from '@/lib/api'

type Plan = {
  id: string; description: string; hint: string; source_label: string
  source_kind: 'volume_outline' | 'chapter_outline'; is_confirmed: boolean | null
}
type PlanPage = { plans: Plan[]; total: number; has_more: boolean; unavailable_sources: number }
type Props = { projectId: string; refreshVersion: number }

export function ForeshadowPlanList(props: Props) {
  return <PlanSession key={props.projectId} {...props} />
}

function PlanSession({ projectId, refreshVersion }: Props) {
  const [expanded, setExpanded] = useState(false)
  const [page, setPage] = useState(0)
  const [reload, setReload] = useState(0)
  const [result, setResult] = useState<PlanPage | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    if (!expanded) return
    let cancelled = false
    setLoading(true); setError('')
    void apiFetch<PlanPage>(`/api/projects/${projectId}/foreshadows/plans?limit=20&offset=${page * 20}`)
      .then(data => {
        if (cancelled) return
        if (!Array.isArray(data.plans) || typeof data.has_more !== 'boolean' ||
            !Number.isSafeInteger(data.total) || !Number.isSafeInteger(data.unavailable_sources) ||
            data.plans.some(plan => !plan || typeof plan.id !== 'string' ||
              typeof plan.description !== 'string' || typeof plan.source_label !== 'string' || typeof plan.hint !== 'string')) {
          throw new Error('invalid plan response')
        }
        setResult(data)
      }).catch(() => {
        if (!cancelled) setError('大纲计划读取失败，已显示的内容仍保留，请刷新核对。')
      }).finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [expanded, projectId, page, refreshVersion, reload])

  const move = (next: number) => { setPage(next); setResult(null) }
  return <section aria-label="大纲伏笔计划" className="space-y-2 border-t border-stone-200 pt-3 text-xs">
    <button aria-expanded={expanded} onClick={() => setExpanded(value => !value)} className="font-semibold text-stone-700">
      大纲中的伏笔计划 {expanded ? '收起' : '展开'}
    </button>
    {expanded && <>
      <p className="text-stone-500">这里显示当前卷纲和章纲中的安排。修改请到对应大纲保存，再刷新核对；计划不表示正文已经埋设或兑现。</p>
      <button onClick={() => setReload(value => value + 1)} disabled={loading} className="text-blue-600 disabled:opacity-40">刷新大纲计划</button>
      {loading && <p role="status">正在读取大纲计划…</p>}
      {error && <p role="alert" className="text-rose-600">{error}</p>}
      {!!result?.unavailable_sources && <p className="text-amber-700">有 {result.unavailable_sources} 处大纲计划的格式或章节位置需要核对，未把不完整的条目列入计划。</p>}
      {result?.plans.map(plan => <article key={plan.id} className="space-y-1 rounded border border-stone-200 p-2">
        <p className="text-stone-500">{plan.source_label} · {plan.source_kind === 'chapter_outline' ? '当前章纲'
          : plan.is_confirmed ? '已确认卷纲' : '待确认卷纲'}</p>
        <p className="whitespace-pre-wrap text-stone-700">{plan.description}</p>
        {plan.hint && <p className="whitespace-pre-wrap text-stone-500">回应计划：{plan.hint}</p>}
      </article>)}
      {!loading && !error && result?.total === 0 && <p className="text-stone-500">当前大纲没有可列出的伏笔计划。</p>}
      {(page > 0 || result?.has_more) && <div className="flex justify-between">
        <button disabled={loading || page === 0} onClick={() => move(page - 1)}>上一页计划</button>
        <span>第 {page + 1} 页</span>
        <button disabled={loading || !result?.has_more} onClick={() => move(page + 1)}>下一页计划</button>
      </div>}
    </>}
  </section>
}
