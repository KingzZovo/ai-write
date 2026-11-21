'use client'

import { useEffect, useRef, useState } from 'react'
import { apiFetch, ApiError } from '@/lib/api'
import { ForeshadowDiscoveryHistory, scanTime, type DiscoveryRun } from './ForeshadowDiscoveryHistory'

type Candidate = {
  id: string; description: string; evidence: string; reason: string
  status: 'pending' | 'accepted' | 'dismissed'
  source_state: 'current' | 'changed' | 'unavailable' | 'unknown'
  source_snapshot: { global_idx?: number }
  handled_at?: string | null; handled_by?: string | null; record_present?: boolean | null
  scan_reason?: string | null; reused_in_scan?: boolean
}
type Props = { projectId: string; chapterId?: string; refreshToken: string; onAccepted?: () => void }
const SOURCE_LABELS = {
  current: '原句与读取时的保存稿一致', changed: '正文或章节位置已变化，请重新核查',
  unavailable: '原章节暂不可核查，保留历史证据', unknown: '来源暂时无法核验',
}

export function ForeshadowDiscoveryList(props: Props) {
  return <DiscoverySession key={`${props.projectId}:${props.chapterId || ''}`} {...props} />
}

function DiscoverySession({ projectId, chapterId, refreshToken, onAccepted }: Props) {
  const [status, setStatus] = useState<Candidate['status'] | 'all'>('pending')
  const [selectedRun, setSelectedRun] = useState<DiscoveryRun | null>(null)
  const [page, setPage] = useState(0)
  const [reload, setReload] = useState(0)
  const [items, setItems] = useState<Candidate[]>([])
  const [hasMore, setHasMore] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [actionError, setActionError] = useState('')
  const [busy, setBusy] = useState<string | null>(null)
  const active = useRef(true)
  const busyRef = useRef(false)
  const version = useRef(0)
  const scope = useRef('pending:0')
  const accepted = useRef(onAccepted)
  accepted.current = onAccepted

  useEffect(() => {
    active.current = true
    return () => { active.current = false; version.current += 1 }
  }, [])

  useEffect(() => {
    const request = ++version.current
    let cancelled = false
    setLoading(true); setError('')
    if (scope.current !== `${status}:${page}:${selectedRun?.id || ''}`) {
      scope.current = `${status}:${page}:${selectedRun?.id || ''}`; setItems([]); setHasMore(false)
    }
    void apiFetch<{ discoveries: Candidate[]; has_more: boolean }>(
      `/api/projects/${projectId}/foreshadows/discoveries?status=${status}&limit=20&offset=${page * 20}${selectedRun ? `&run_id=${encodeURIComponent(selectedRun.id)}` : ''}`,
    ).then(data => {
      if (cancelled || !active.current || request !== version.current) return
      if (!Array.isArray(data.discoveries) || typeof data.has_more !== 'boolean') throw new Error('invalid discovery response')
      setItems(data.discoveries); setHasMore(data.has_more)
    }).catch(() => {
      if (!cancelled && active.current && request === version.current) setError('线索候选读取失败，已显示的证据仍保留。请刷新后核对。')
    }).finally(() => {
      if (!cancelled && active.current && request === version.current) setLoading(false)
    })
    return () => { cancelled = true }
  }, [projectId, refreshToken, status, page, reload, selectedRun?.id])

  const decide = async (id: string, action: 'accept' | 'dismiss') => {
    if (busyRef.current || !active.current) return
    busyRef.current = true; setBusy(id); setActionError('')
    version.current += 1
    try {
      const result = await apiFetch<{ id: string; status: string; foreshadow_id?: string | null }>(
        `/api/projects/${projectId}/foreshadows/discoveries/${id}/${action}`, { method: 'POST' },
      )
      if (result.id !== id || result.status !== (action === 'accept' ? 'accepted' : 'dismissed') ||
          (action === 'accept' && result.foreshadow_id !== id)) throw new Error('missing acknowledgment')
      if (!active.current) return
      if (status !== 'all') setItems(current => current.filter(item => item.id !== id))
      setLoading(true); setReload(n => n + 1)
      if (action === 'accept') accepted.current?.()
    } catch (err) {
      if (active.current) setActionError(err instanceof ApiError && err.status === 409 ? err.message
        : '尚未确认处理结果，请刷新核对后重试同一条线索。')
    } finally {
      busyRef.current = false
      if (active.current) setBusy(null)
    }
  }

  return <section aria-label="正文线索候选" className="space-y-2 border-t border-stone-200 pt-3 text-xs">
    <div className="flex items-center justify-between">
      <h4 className="font-semibold text-stone-700">正文线索候选</h4>
      <button disabled={loading || busy !== null} onClick={() => { setActionError(''); setReload(n => n + 1) }}
        className="text-blue-600 disabled:opacity-40">刷新线索</button>
    </div>
    <p className="text-stone-500">请对照前文判断是否需要登记。已登记的线索或普通细节可忽略；候选不会自动加入后续写作的伏笔资料。</p>
    <ForeshadowDiscoveryHistory projectId={projectId} chapterId={chapterId} refreshToken={refreshToken} busy={busy !== null}
      selectedId={selectedRun?.id} onSelect={run => { setSelectedRun(run); setStatus('all'); setPage(0); setActionError('') }} />
    {selectedRun && <div className="space-y-1 rounded bg-stone-50 p-2">
      <p>当前查看：{scanTime(selectedRun.created_at)} 的识别清单；处理状态显示作者的最新决定。</p>
      {selectedRun.result_state !== 'complete' && <p className="text-rose-600">该轮清单暂无法完整核验，请刷新历史核对。</p>}
      <button disabled={busy !== null} onClick={() => { setSelectedRun(null); setPage(0); setStatus('pending') }}
        className="text-blue-600 disabled:opacity-40">返回全部线索</button>
    </div>}
    <label className="flex items-center gap-2 text-stone-600">查看线索
      <select aria-label="线索处理状态" value={status} disabled={busy !== null}
        onChange={event => { setStatus(event.target.value as Candidate['status'] | 'all'); setPage(0); setActionError('') }}
        className="rounded border border-stone-200 p-1">
        <option value="pending">待核对</option><option value="accepted">已登记</option><option value="dismissed">已忽略</option>
        <option value="all">全部处理状态</option>
      </select>
    </label>
    {loading && <p role="status">正在读取线索…</p>}
    {error && <p role="alert" className="text-rose-600">{error}</p>}
    {actionError && <p role="alert" className="text-rose-600">{actionError}</p>}
    {items.map(item => <article key={item.id} className="space-y-1 rounded border border-stone-200 p-2">
      <p className="font-medium text-stone-700">{item.description}</p>
      <p className="text-stone-500">
        {typeof item.source_snapshot.global_idx === 'number' && `全书第 ${item.source_snapshot.global_idx} 章 · `}
        {SOURCE_LABELS[item.source_state] || SOURCE_LABELS.unknown}
      </p>
      <blockquote className="whitespace-pre-wrap border-l-2 border-stone-300 pl-2 text-stone-700">{item.evidence}</blockquote>
      <p className="whitespace-pre-wrap text-stone-500">{selectedRun ? '本轮说明' : '首次识别说明'}：{item.scan_reason ?? item.reason}</p>
      {item.scan_reason && item.scan_reason !== item.reason && <p className="whitespace-pre-wrap text-stone-500">首次识别说明：{item.reason}</p>}
      {item.reused_in_scan && <p className="text-stone-500">与此前线索的描述、类型及原句位置完全相同，沿用原处理记录；本轮说明未单独确认。</p>}
      {status === 'all' && <p className="text-stone-500">处理状态：{item.status === 'pending' ? '待核对' : item.status === 'accepted' ? '已登记' : '已忽略'}</p>}
      {item.status === 'pending' ? <div className="flex gap-3">
        <button disabled={busy !== null || loading || !!error || item.source_state !== 'current'}
          onClick={() => decide(item.id, 'accept')} className="text-emerald-700 underline disabled:opacity-40">核对后登记为伏笔</button>
        <button disabled={busy !== null || loading} onClick={() => decide(item.id, 'dismiss')}
          className="text-stone-500 underline disabled:opacity-40">忽略此线索</button>
        {busy === item.id && <span role="status">正在提交…</span>}
      </div> : <p className="text-stone-500">处理账号：{item.handled_by || '未记录'} · 处理时间：
        {item.handled_at && Number.isFinite(Date.parse(item.handled_at))
          ? <time dateTime={item.handled_at}>{new Date(item.handled_at).toLocaleString()}</time> : '未记录'}
      </p>}
      {item.status === 'accepted' && item.record_present === false && <p className="text-stone-500">登记记录已不可用，原核对证据仍保留。</p>}
    </article>)}
    {!loading && !error && items.length === 0 && <p className="text-stone-500">
      {selectedRun?.result_state === 'unknown' ? '本轮清单无法完整核验，不能判断是否没有线索。'
        : selectedRun?.result_state === 'complete' && selectedRun.candidate_count === 0 ? '本轮已完成识别，未发现线索候选；此前各轮记录仍保留。'
        : selectedRun ? '本轮没有符合当前处理状态的线索。'
        : status === 'pending' ? '暂无待核对线索；是否已完成本章核查请查看任务进度。' : status === 'accepted' ? '暂无已登记线索。'
        : status === 'dismissed' ? '暂无已忽略线索。' : '暂无线索候选。'}
    </p>}
    {(page > 0 || hasMore) && <div className="flex justify-between">
      <button disabled={loading || busy !== null || page === 0} onClick={() => setPage(n => n - 1)}>上一页线索</button>
      <span>第 {page + 1} 页</span>
      <button disabled={loading || busy !== null || !hasMore} onClick={() => setPage(n => n + 1)}>下一页线索</button>
    </div>}
  </section>
}
