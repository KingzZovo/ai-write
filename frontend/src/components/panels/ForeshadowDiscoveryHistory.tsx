'use client'

import { useEffect, useState } from 'react'
import { apiFetch } from '@/lib/api'

export type DiscoveryRun = {
  id: string; chapter_id: string; chapter_title: string | null; created_at: string
  candidate_count: number | null; result_state: 'complete' | 'unknown'; scan_revision: number
  model_name?: string | null; source_snapshot: { global_idx?: number }
  source_state: 'current' | 'changed' | 'unavailable' | 'unknown'
}

export function scanTime(value: string) {
  return Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString() : '时间未记录'
}

export function ForeshadowDiscoveryHistory({ projectId, chapterId, refreshToken, busy, selectedId, onSelect }: {
  projectId: string; chapterId?: string; refreshToken: string; busy: boolean
  selectedId?: string; onSelect: (run: DiscoveryRun) => void
}) {
  const [open, setOpen] = useState(false)
  const [page, setPage] = useState(0)
  const [reload, setReload] = useState(0)
  const [runs, setRuns] = useState<DiscoveryRun[]>([])
  const [hasMore, setHasMore] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  useEffect(() => {
    if (!open) return
    let active = true
    setLoading(true); setError(''); setRuns([]); setHasMore(false)
    const chapter = chapterId ? `&chapter_id=${encodeURIComponent(chapterId)}` : ''
    void apiFetch<{ runs: DiscoveryRun[]; has_more: boolean }>(
      `/api/projects/${projectId}/foreshadows/discovery-runs?limit=10&offset=${page * 10}${chapter}`,
    ).then(data => {
      if (!active) return
      if (!Array.isArray(data.runs) || typeof data.has_more !== 'boolean') throw new Error('invalid scan history')
      setRuns(data.runs); setHasMore(data.has_more)
    }).catch(() => { if (active) setError('识别历史读取失败，请刷新历史。') })
      .finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [projectId, chapterId, refreshToken, open, page, reload])

  return <div className="space-y-2">
    <button aria-expanded={open} onClick={() => setOpen(value => !value)} className="text-blue-600">
      {open ? '收起识别历史' : '查看识别历史'}</button>
    {open && <div aria-label="线索识别历史" className="space-y-2 rounded bg-stone-50 p-2">
      <div className="flex justify-between"><span>{chapterId ? '当前章节的识别记录' : '本作品的识别记录'}</span>
        <button disabled={loading} onClick={() => setReload(n => n + 1)} className="text-blue-600 disabled:opacity-40">刷新历史</button></div>
      {loading && <p role="status">正在读取识别历史…</p>}
      {error && <p role="alert" className="text-rose-600">{error}</p>}
      {runs.map(run => <article key={run.id} className="space-y-1 border-t border-stone-200 pt-2">
        <p>{run.chapter_title || '原章节已不可用'} · {scanTime(run.created_at)} · {run.scan_revision > 0 ? '重新识别' : '首次识别'}</p>
        <p>{run.result_state !== 'complete' ? '本轮清单暂无法完整核验'
          : run.candidate_count === 0 ? '本轮未发现线索候选' : `本轮识别 ${run.candidate_count} 条线索（含已处理）`}</p>
        <p className="text-stone-500">{run.source_state === 'current' ? '原稿及位置与读取时一致'
          : run.source_state === 'changed' ? '正文或章节位置已变化' : run.source_state === 'unavailable' ? '原稿暂不可核查' : '来源暂无法核验'}</p>
        <p className="text-stone-500">调用模型名称：{run.model_name || '未记录'}；此名称可能由端点转发。</p>
        <button disabled={busy || loading || !!error || selectedId === run.id} onClick={() => onSelect(run)}
          className="text-blue-600 disabled:opacity-40">{selectedId === run.id ? '正在查看此轮' : '查看此轮线索'}</button>
      </article>)}
      {!loading && !error && runs.length === 0 && <p className="text-stone-500">暂无已完成的识别记录；进行中或失败的任务请查看核查进度。</p>}
      {(page > 0 || hasMore) && <div className="flex justify-between">
        <button disabled={loading || page === 0} onClick={() => setPage(n => n - 1)}>上一页识别记录</button>
        <button disabled={loading || !hasMore} onClick={() => setPage(n => n + 1)}>下一页识别记录</button>
      </div>}
    </div>}
  </div>
}
