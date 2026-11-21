'use client'

import { useEffect, useRef, useState } from 'react'
import { apiFetch, ApiError } from '@/lib/api'
import { draftKey } from '@/lib/foreshadowWrites'
import { reviewAttempt, acknowledgeReviewAttempt } from '@/lib/foreshadowReviewRequests'

type Job = { id: string; chapter_title: string; status: string; attempts: number; discovery_count?: number | null; rescan_discovery?: boolean }
const LABELS: Record<string, string> = {
  pending: '等待核查', queued: '已排队', running: '正在核查', completed: '核查已结束',
  failed: '核查未完成', obsolete: '已有更新版本',
}

type Props = {
  projectId: string; refreshVersion: number; onCompleted: () => void; chapterId?: string; editorContent?: string
}

export function ForeshadowReviewJobs(props: Props) {
  return <ReviewJobsSession key={props.projectId} {...props} />
}

function ReviewJobsSession({ projectId, refreshVersion, onCompleted, chapterId, editorContent }: Props) {
  const [jobs, setJobs] = useState<Job[]>([])
  const [page, setPage] = useState(0)
  const [hasMore, setHasMore] = useState(false)
  const [reload, setReload] = useState(0)
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const previous = useRef<Record<string, string>>({})
  const completed = useRef(onCompleted)
  completed.current = onCompleted
  const active = useRef(true)
  const retrying = useRef(false)
  useEffect(() => { active.current = true; return () => { active.current = false } }, [])

  useEffect(() => {
    let active = true
    let timer: ReturnType<typeof setTimeout> | undefined
    previous.current = {}
    setLoading(true)
    const fetchJobs = async () => {
      try {
        const data = await apiFetch<{ jobs: Job[]; has_more: boolean }>(
          `/api/projects/${projectId}/foreshadows/review-jobs?limit=20&offset=${page * 20}`,
        )
        if (!active) return
        const items = data.jobs || []
        if (items.some(j => j.status === 'completed' && previous.current[j.id] !== 'completed')) completed.current()
        previous.current = Object.fromEntries(items.map(j => [j.id, j.status]))
        setJobs(items); setHasMore(Boolean(data.has_more)); setError(''); setLoading(false)
        if (items.some(j => ['pending', 'queued', 'running'].includes(j.status))) timer = setTimeout(fetchJobs, 5000)
      } catch { if (active) { setError('暂时无法读取核查任务，请刷新重试。'); setLoading(false) } }
    }
    void fetchJobs()
    return () => { active = false; if (timer) clearTimeout(timer) }
  }, [projectId, page, reload, refreshVersion])

  const retry = async (id: string) => {
    if (retrying.current || !active.current) return
    retrying.current = true
    setBusy(id); setError('')
    try {
      const result = await apiFetch<{ id: string; status: string }>(`/api/projects/${projectId}/foreshadows/review-jobs/${id}/retry`, { method: 'POST' })
      if (result.id !== id || result.status !== 'pending') throw new Error('missing acknowledgment')
      if (active.current) setReload(n => n + 1)
    } catch { if (active.current) setError('未能确认重试，正文可能已更新。请刷新核对任务状态。') }
    finally { retrying.current = false; if (active.current) setBusy(null) }
  }

  return <section aria-label="伏笔核查任务" className="space-y-1 rounded bg-stone-50 p-2 text-xs">
    <div className="flex justify-between"><span className="font-medium">章节核查进度</span>
      <button onClick={() => setReload(n => n + 1)} className="text-blue-600">刷新任务</button></div>
    {chapterId && editorContent !== undefined && <CreateReview key={`${projectId}:${chapterId}`} projectId={projectId}
      chapterId={chapterId} editorContent={editorContent} onCreated={() => { setPage(0); setReload(n => n + 1) }} />}
    {loading && <p role="status">正在读取核查任务…</p>}
    {error && <p role="alert" className="text-rose-600">{error}</p>}
    {jobs.map(job => <div key={job.id} className="flex items-center gap-2">
      <span className="min-w-0 flex-1 truncate">{job.chapter_title}</span>
      <span>{LABELS[job.status] || '状态待核对'}</span>
      {job.rescan_discovery && <span className="text-stone-500">重新识别</span>}
      {job.status === 'completed' && <span className="text-stone-500">{typeof job.discovery_count === 'number'
        ? `本稿识别 ${job.discovery_count} 条线索候选（含已处理）` : '旧任务仅含兑现核查'}</span>}
      {job.status === 'failed' && <button disabled={busy !== null} onClick={() => retry(job.id)} className="text-blue-600 disabled:opacity-40">
        {busy === job.id ? '正在提交…' : '重试核查'}</button>}
    </div>)}
    {!loading && !error && jobs.length === 0 && <p className="text-stone-500">暂无核查任务。</p>}
    {(page > 0 || hasMore) && <div className="flex justify-between">
      <button disabled={page === 0} onClick={() => setPage(p => p - 1)}>上一页任务</button>
      <button disabled={!hasMore} onClick={() => setPage(p => p + 1)}>下一页任务</button>
    </div>}
  </section>
}


function CreateReview({ projectId, chapterId, editorContent, onCreated }: {
  projectId: string; chapterId: string; editorContent: string; onCreated: () => void
}) {
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')
  const version = useRef(0)
  const busyVersion = useRef<number | null>(null)
  useEffect(() => {
    version.current += 1
    setMessage(''); setError(''); setBusy(false)
    return () => { version.current += 1 }
  }, [editorContent])
  const submit = async (rescan = false) => {
    const requestVersion = version.current
    if (busyVersion.current === requestVersion) return
    busyVersion.current = requestVersion
    setBusy(true); setError(''); setMessage('')
    const key = draftKey(projectId, `review:${chapterId}:${rescan ? 'rescan' : 'normal'}`)
    let attempt: ReturnType<typeof reviewAttempt> | undefined
    try {
      const payload = { chapter_id: chapterId, expected_content: editorContent, rescan_discovery: rescan }
      attempt = reviewAttempt(key, JSON.stringify(payload))
      const result = await apiFetch<{ id: string; chapter_id: string; status: string; request_id?: string; rescan_discovery?: boolean }>(
        `/api/projects/${projectId}/foreshadows/review-jobs`, {
          method: 'POST', body: JSON.stringify({ ...payload, request_id: attempt.id }),
        })
      if (typeof result.id !== 'string' || !result.id || result.chapter_id !== chapterId || result.request_id !== attempt.id ||
          (rescan && result.rescan_discovery !== true) ||
          !['pending', 'queued', 'running', 'completed', 'failed', 'obsolete', 'unavailable'].includes(result.status)) throw new Error('missing acknowledgment')
      acknowledgeReviewAttempt(key, attempt)
      if (requestVersion !== version.current) return
      setMessage(result.status === 'completed' ? '该请求的核查已结束，可查看对应的识别历史。'
        : result.status === 'failed' ? '该请求已提交，但核查未完成；请在任务列表重试原任务。'
        : result.status === 'obsolete' ? '该请求对应的保存稿已有更新，请核对当前章节。'
        : result.status === 'unavailable' ? '原请求已受理，原章节或任务现已不可用，请核对章节。'
        : rescan ? '已提交重新识别线索，请在任务列表查看进度。'
        : '已提交当前保存稿的伏笔核查，请在任务列表查看进度。')
      onCreated()
    } catch (err) {
      // These API rejections happen before commit. Keep other drafts' unknown
      // outcomes, but do not accumulate full text for definitively refused work.
      if (attempt && err instanceof ApiError && [409, 422].includes(err.status)) acknowledgeReviewAttempt(key, attempt)
      if (requestVersion === version.current) setError(err instanceof ApiError && err.status === 409
        ? err.message : err instanceof ApiError && err.status === 422 ? '核查请求未通过校验，请刷新页面后重新提交。'
        : attempt ? '未能确认核查提交，已保留原请求；再次点击同一按钮可核对并重试。'
        : '无法准备核查请求，请刷新页面后重试。')
    } finally {
      if (busyVersion.current === requestVersion) busyVersion.current = null
      if (requestVersion === version.current) setBusy(false)
    }
  }
  return <div className="space-y-1 py-1">
    <div className="flex flex-wrap gap-3">
      <button disabled={busy || !editorContent.trim()} onClick={() => submit()} className="text-blue-600 disabled:opacity-40">
        {busy ? '正在提交核查…' : '核查当前已保存章节'}</button>
      <button disabled={busy || !editorContent.trim()} onClick={() => submit(true)} className="text-blue-600 disabled:opacity-40">重新识别线索</button>
    </div>
    <p className="text-stone-500">请先保存正文；核查提供线索候选和兑现建议，需逐条核对后确认。</p>
    <p className="text-stone-500">普通核查复用同稿最近的识别结果；更换提示词或模型后，可重新识别线索并保留每轮记录。完全相同的线索沿用原处理记录。</p>
    {message && <p role="status">{message}</p>}
    {error && <p role="alert" className="text-rose-600">{error}</p>}
  </div>
}
