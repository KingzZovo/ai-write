'use client'

import { useEffect, useRef, useState } from 'react'
import { apiFetch } from '@/lib/api'
import { ForeshadowReviewJobs } from './ForeshadowReviewJobs'
import { ForeshadowDiscoveryList } from './ForeshadowDiscoveryList'

type Review = {
  id: string
  evidence: string
  reason: string
  confirmation_status?: string | null
  handled_at?: string | null
  handled_by?: string | null
  status: 'pending' | 'confirming' | 'accepted' | 'dismissed'
  source_state: 'accepted_current' | 'current' | 'changed' | 'unavailable' | 'unknown' | 'projection_current'
  source_snapshot: { global_idx?: number; foreshadow?: { description?: string; source?: string } }
}
const SOURCE_LABELS = {
  accepted_current: '读取时与作者确认的兑现结果一致',
  current: '读取时与稿件及伏笔记录一致',
  changed: '来源已修改，请重新核对',
  unavailable: '原章节或伏笔已不可用，以下保留历史证据',
  unknown: '来源暂时无法核验',
  projection_current: '记录副本一致，同步状态待核对',
}
const PAGE_SIZE = 20

export function ForeshadowReviewList({ projectId, refreshVersion = 0, onAccepted, chapterId, editorContent }: { chapterId?: string; editorContent?: string; projectId: string; refreshVersion?: number; onAccepted?: () => void }) {
  const [status, setStatus] = useState<'pending' | 'confirming' | 'accepted' | 'dismissed'>('pending')
  const observedConfirming = useRef(new Set<string>())
  const acceptedCallback = useRef(onAccepted)
  acceptedCallback.current = onAccepted
  const [page, setPage] = useState(0)
  const [reload, setReload] = useState(0)
  const [reviews, setReviews] = useState<Review[]>([])
  const [hasMore, setHasMore] = useState(false)
  const [loading, setLoading] = useState(true)
  const [failed, setFailed] = useState(false)
  const [busyId, setBusyId] = useState<string | null>(null)
  const [actionError, setActionError] = useState('')
  const [busyAction, setBusyAction] = useState<'dismiss' | 'accept'>('dismiss')

  const act = async (reviewId: string, action: 'dismiss' | 'accept') => {
    setBusyAction(action)
    setBusyId(reviewId)
    setActionError('')
    try {
      const result = await apiFetch<{ id: string; status: string }>(
        `/api/projects/${projectId}/foreshadows/reviews/${reviewId}/${action}`, { method: 'POST' },
      )
      if (result.id !== reviewId || !(action === 'accept' ? ['accepted', 'confirming'] : ['dismissed']).includes(result.status)) throw new Error('missing acknowledgment')
      if (result.status === 'confirming') { observedConfirming.current.add(reviewId); setStatus('confirming'); setPage(0) }
      setReviews(items => items.filter(item => item.id !== reviewId))
      setReload(n => n + 1)
      if (result.status === 'accepted') onAccepted?.()
    } catch {
      setActionError('未能确认处理结果，请刷新核对后重试。')
    } finally {
      setBusyId(null)
    }
  }

  useEffect(() => {
    let active = true
    let timer: ReturnType<typeof setTimeout> | undefined
    setLoading(true)
    setFailed(false)
    const fetchReviews = async () => {
      try {
        const data = await apiFetch<{ reviews: Review[]; has_more: boolean }>(
          `/api/projects/${projectId}/foreshadows/reviews?limit=${PAGE_SIZE}&offset=${page * PAGE_SIZE}&status=${status}`)
        if (!active) return
        const items = data.reviews || []
        if (status === 'confirming') {
          const current = new Set(items.map(item => item.id))
          if ([...observedConfirming.current].some(id => !current.has(id))) acceptedCallback.current?.()
          observedConfirming.current = current
          if (items.some(item => ['pending', 'queued', 'running'].includes(item.confirmation_status || ''))) {
            timer = setTimeout(fetchReviews, 5000)
          }
        }
        setReviews(items); setHasMore(Boolean(data.has_more)); setFailed(false)
      } catch { if (active) { setFailed(true); setReviews([]) } }
      finally { if (active) setLoading(false) }
    }
    void fetchReviews()
    return () => { active = false; if (timer) clearTimeout(timer) }
  }, [projectId, page, reload, refreshVersion, status])

  return (
    <section className="space-y-2 border-t border-stone-200 pt-3" aria-label="伏笔核查">
      <div className="flex items-center justify-between">
        <h4 className="text-xs font-semibold text-stone-700">伏笔核查</h4>
        <button disabled={loading} onClick={() => setReload(n => n + 1)} className="text-xs text-blue-600 disabled:opacity-40">刷新建议</button>
      </div>
      <ForeshadowReviewJobs chapterId={chapterId} editorContent={editorContent} projectId={projectId} refreshVersion={refreshVersion} onCompleted={() => setReload(n => n + 1)} />
      <ForeshadowDiscoveryList projectId={projectId} chapterId={chapterId} refreshToken={`${refreshVersion}:${reload}`} onAccepted={onAccepted} />
      <h4 className="pt-2 text-xs font-semibold text-stone-700">兑现核查建议</h4>
      <label className="flex items-center gap-2 text-xs text-stone-600">查看建议
        <select aria-label="建议处理状态" value={status} disabled={busyId !== null}
          onChange={event => { setStatus(event.target.value as typeof status); setPage(0); setActionError('') }}
          className="rounded border border-stone-200 p-1">
          <option value="pending">待审阅</option><option value="confirming">确认同步</option><option value="accepted">已确认</option><option value="dismissed">已忽略</option>
        </select>
      </label>
      <p className="text-[11px] text-stone-500">{status === 'pending'
        ? '建议尚未确认。请结合前文核对原句是否真正完成收线。'
        : status === 'confirming' ? '确认请求已保存；系统核对图谱并完成同步后才标记为已确认。'
        : '以下保留当时的原句、伏笔描述和核查说明。来源标签反映读取时的核对结果。'}</p>
      {actionError && <p role="alert" className="text-xs text-rose-600">{actionError}</p>}
      {loading ? <p className="text-xs text-stone-500" role="status">正在读取建议…</p> : failed ? (
        <div className="text-xs text-rose-600" role="alert">
          暂时无法读取核查建议。
          <button onClick={() => setReload(n => n + 1)} className="ml-2 underline">重试</button>
        </div>
      ) : reviews.map(review => (
        <article key={review.id} className="space-y-1 rounded border border-stone-200 p-2 text-xs">
          <p className="font-medium text-stone-700">{review.source_snapshot.foreshadow?.description || '伏笔核查'}</p>
          <p className="text-[11px] text-stone-500">
            {typeof review.source_snapshot.global_idx === 'number' && `全书第 ${review.source_snapshot.global_idx} 章 · `}
            {SOURCE_LABELS[review.source_state] || SOURCE_LABELS.unknown}
          </p>
          {['accepted', 'dismissed'].includes(review.status) && <p className="text-[11px] text-stone-500">
            处理账号：{review.handled_by || '未记录'} · 处理时间：{review.handled_at && Number.isFinite(Date.parse(review.handled_at))
              ? <time dateTime={review.handled_at}>{new Date(review.handled_at).toLocaleString()}</time> : '未记录'}
          </p>}
          <blockquote className="whitespace-pre-wrap border-l-2 border-stone-300 pl-2 text-stone-700">{review.evidence}</blockquote>
          <p className="whitespace-pre-wrap text-stone-500">核查说明：{review.reason}</p>
          {status === 'pending' && review.status === 'pending' && <>
          <button disabled={busyId !== null || !(review.source_state === 'current' || (review.source_state === 'projection_current' && review.source_snapshot.foreshadow?.source === 'neo4j'))}
            onClick={() => act(review.id, 'accept')}
            title="图谱来源将先核对并同步；来源变化时拒绝兑现。"
            className="mr-3 text-xs text-emerald-700 underline disabled:opacity-40">
            {busyId === review.id && busyAction === 'accept' ? '正在确认…' : '核对后确认兑现'}
          </button>
          <button disabled={busyId !== null} onClick={() => act(review.id, 'dismiss')}
            className="text-xs text-stone-500 underline disabled:opacity-40">
            {busyId === review.id && busyAction === 'dismiss' ? '正在忽略…' : '忽略此建议'}
          </button>
          </>}
          {review.status === 'confirming' && <div className="space-y-1 text-stone-600">
            <p>{review.confirmation_status === 'rejected' ? '来源已变化，本次确认未执行。请重新核查。'
              : review.confirmation_status === 'failed' ? '同步未完成，可重试核对执行结果。'
              : ['pending', 'queued', 'running'].includes(review.confirmation_status || '') ? '确认同步中…' : '同步记录暂时无法核验，请刷新。'}</p>
            {review.confirmation_status === 'failed' && <button disabled={busyId !== null} onClick={() => act(review.id, 'accept')} className="text-blue-600 underline">重试同步</button>}
            {review.confirmation_status === 'rejected' && <button disabled={busyId !== null} onClick={() => act(review.id, 'dismiss')} className="underline">放弃此确认</button>}
          </div>}
        </article>
      ))}
      {!loading && !failed && reviews.length === 0 && <p className="text-xs text-stone-500">{status === 'pending' ? '暂无待审阅建议。' : status === 'confirming' ? '暂无待同步确认。' : status === 'accepted' ? '暂无已确认建议。' : '暂无已忽略建议。'}</p>}
      {!loading && !failed && (page > 0 || hasMore) && (
        <div className="flex items-center justify-between text-xs">
          <button disabled={page === 0} onClick={() => setPage(n => n - 1)} className="disabled:opacity-40">上一页建议</button>
          <span>第 {page + 1} 页</span>
          <button disabled={!hasMore} onClick={() => setPage(n => n + 1)} className="disabled:opacity-40">下一页建议</button>
        </div>
      )}
    </section>
  )
}
