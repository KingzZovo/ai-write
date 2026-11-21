'use client'

import { useEffect, useRef, useState } from 'react'
import { apiFetch, ApiError } from '@/lib/api'
import {
  acknowledgeCognitionRequest, cognitionSlot, matchesCognitionAcknowledgment,
  pendingCognitionRequests, retainCognitionRequest, cognitionSubmission,
} from '@/lib/cognitionRequests'
import type { CognitionAttempt, CognitionIntent } from '@/lib/cognitionRequests'
import type { CognitionChapter as Chapter, CognitionDetail } from '@/lib/cognitionTypes'
import { acknowledgeCognitionDraft } from '@/lib/cognitionDrafts'
import { CognitionChanges } from './CognitionChanges'
import { CognitionEditor } from './CognitionEditor'
import { CognitionHistory } from './CognitionHistory'

type Backfill = {
  id: string; status: string; chapter_count: number; cursor: number; added_count: number
  existing_count: number; attention_count: number; unavailable_count: number
}
const labels: Record<string, string> = {
  missing: '尚未提取', pending: '等待提取', queued: '已排队', running: '正在提取',
  failed: '提取未完成', obsolete: '任务已失效', invalid: '记录需核对', completed: '提取已结束', unavailable: '保存稿不可用',
}
const activeStatuses = new Set(['pending', 'queued', 'running'])
const buttonClass = 'rounded px-2 py-1 text-blue-700 hover:bg-blue-50 disabled:opacity-40'

export function CognitionPanel({ projectId }: { projectId: string }) {
  return <CognitionSession key={projectId} projectId={projectId} />
}

function CognitionSession({ projectId }: { projectId: string }) {
  const base = `/api/projects/${projectId}/cognition`
  const [chapters, setChapters] = useState<Chapter[]>([])
  const [backfills, setBackfills] = useState<Backfill[]>([])
  const [pending, setPending] = useState<CognitionAttempt[]>([])
  const [page, setPage] = useState(0)
  const [total, setTotal] = useState(0)
  const [hasMore, setHasMore] = useState(false)
  const [reload, setReload] = useState(0)
  const [loading, setLoading] = useState(true)
  const [readError, setReadError] = useState('')
  const [writeError, setWriteError] = useState('')
  const [message, setMessage] = useState('')
  const [busy, setBusy] = useState(false)
  const submitting = useRef(false)
  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    try { setPending(pendingCognitionRequests(projectId)) }
    catch { setWriteError('无法读取待确认的提交，请保留本页并核对任务状态。') }
    return () => { mounted.current = false }
  }, [projectId])

  useEffect(() => {
    let live = true
    let timer: ReturnType<typeof setTimeout> | undefined
    const controller = new AbortController()
    setLoading(true); setReadError('')
    const read = async () => {
      try {
        const [data, jobs] = await Promise.all([
          apiFetch<{ chapters: Chapter[]; has_more: boolean; total: number }>(
            `${base}/chapters?limit=20&offset=${page * 20}`, { signal: controller.signal }),
          apiFetch<{ backfills: Backfill[] }>(`${base}/backfills`, { signal: controller.signal }),
        ])
        if (!live) return
        setChapters(data.chapters); setHasMore(data.has_more); setTotal(data.total)
        setBackfills(jobs.backfills); setLoading(false); setReadError('')
        if (data.chapters.some(ch => activeStatuses.has(ch.status)) || jobs.backfills.some(job => job.status === 'pending')) {
          timer = setTimeout(read, 5000)
        }
      } catch {
        if (live) { setLoading(false); setReadError('暂时无法读取认知进度，请刷新重试。') }
      }
    }
    void read()
    return () => { live = false; controller.abort(); if (timer) clearTimeout(timer) }
  }, [base, page, reload])

  const submit = async (intent: CognitionIntent, retained?: CognitionAttempt) => {
    if (submitting.current || !mounted.current) return
    submitting.current = true; setBusy(true); setWriteError(''); setMessage('')
    let attempt: CognitionAttempt | undefined
    try {
      attempt = retained || retainCognitionRequest(projectId, intent)
      setPending(pendingCognitionRequests(projectId))
      const original = attempt.intent
      const { path, payload } = cognitionSubmission(base, attempt)
      const result = await apiFetch<unknown>(path, { method: 'POST', body: JSON.stringify(payload) })
      if (!matchesCognitionAcknowledgment(attempt, result)) throw new Error('missing acknowledgment')
      acknowledgeCognitionRequest(projectId, attempt)
      try { acknowledgeCognitionDraft(projectId, attempt) } catch { /* Keep unreadable local drafts for review. */ }
      if (!mounted.current) return
      setMessage(original.operation === 'backfill'
        ? '已受理历史补提取，请查看安排进度；各章提取结果仍需分别核对。'
        : original.operation === 'edit' ? '已保存作者校订；提交期间的新输入仍会保留为草稿。'
        : original.operation === 'restore_auto' ? '已确认恢复自动提取，请核对章节状态。未保存草稿仍会保留。'
        : '已确认这次提交，请以下方刷新后的章节状态为准。')
      setReload(n => n + 1)
    } catch (err) {
      const rejected = err instanceof ApiError && [404, 409, 422].includes(err.status)
      if (attempt && rejected) acknowledgeCognitionRequest(projectId, attempt)
      if (mounted.current) {
        setWriteError(rejected ? (attempt?.intent.operation === 'edit' && err instanceof ApiError && err.status === 422
          ? `校订未保存：${err.message}` : '保存稿、章序或提取状态已变化，请刷新核对后再提交。校订草稿会保留。')
          : attempt ? '尚未确认提交，本页保留了原请求。请点击“确认上次提交”，避免重新开一轮。'
          : '无法准备提取请求，请保留本页并核对任务状态。')
        if (rejected) setReload(n => n + 1)
      }
    } finally {
      submitting.current = false
      if (mounted.current) {
        setBusy(false)
        try { setPending(pendingCognitionRequests(projectId)) } catch { /* Keep the visible request/error. */ }
      }
    }
  }

  const backfillPending = pending.find(item => item.intent.operation === 'backfill')
  return <section aria-label="人物认知" className="space-y-4 text-sm">
    <div className="space-y-2">
      <p className="text-gray-600">按章节核对谁在何时获知了什么。这里处理已完成的保存稿；读者知道，不等于人物知道。</p>
      <div className="flex flex-wrap gap-2">
        <button className={buttonClass} disabled={busy || loading || !!readError || !!backfillPending || backfills.some(job => job.status === 'pending')}
          onClick={() => submit({ operation: 'backfill' })}>补提取历史完成章</button>
        <button className={buttonClass} onClick={() => setReload(n => n + 1)}>刷新认知进度</button>
      </div>
      <p className="text-xs text-gray-500">历史补提取分批检查本次清单中的全部完成章，只安排缺失项，保留作者校订。失败项请单独处理。期间修改、删除或新增章节后，可再次安排。</p>
    </div>
    {pending.length > 0 && <div className="space-y-2 rounded border border-amber-200 bg-amber-50 p-3" aria-label="待确认的认知提交">
      <p>这些提交尚未收到确认。确认时会核对原请求。</p>
      {pending.map((attempt, index) => <div key={attempt.id} className="flex flex-wrap items-center justify-between gap-2">
        <span>{attempt.intent.operation === 'backfill' ? '历史补提取'
          : `${chapters.find(ch => ch.chapter_id === cognitionSlot(attempt.intent))?.chapter_title || `章节请求 ${index + 1}`} · ${
            attempt.intent.operation === 'edit' ? '作者校订' : attempt.intent.operation === 'restore_auto' ? '恢复自动提取' : '提取保存稿'}`}</span>
        <button disabled={busy} className={buttonClass} onClick={() => submit(attempt.intent, attempt)}>确认上次提交</button>
      </div>)}
    </div>}
    {message && <p role="status" className="text-green-800">{message}</p>}
    {writeError && <p role="alert" className="text-red-700">{writeError}</p>}
    {loading && <p role="status">正在读取认知记录…</p>}
    {readError && <p role="alert" className="text-red-700">{readError}</p>}
    {((!loading && !readError) || chapters.length > 0) && <>
      {backfills.length > 0 && <div className="space-y-2 rounded bg-gray-50 p-3" aria-label="历史补提取安排进度">
        {backfills.map(job => <div key={job.id} className="space-y-1 text-xs">
          <p className="font-medium">{job.status === 'pending' ? '正在安排' : job.status === 'scheduled' ? '安排已结束（不代表提取完成）' : '安排未完成'}：{job.cursor} / {job.chapter_count} 章</p>
          <p>新增安排 {job.added_count} 章 · 已有任务或结果 {job.existing_count} 章 · 需单独处理 {job.attention_count} 章 · 保存稿不可用 {job.unavailable_count} 章</p>
        </div>)}
      </div>}
      <p className="text-xs text-gray-500">共 {total} 章 · 第 {page + 1} 页</p>
      {chapters.map(chapter => <CognitionChapter key={chapter.chapter_id} projectId={projectId} chapter={chapter}
        disabled={busy || loading || !!readError} pending={pending.some(item => cognitionSlot(item.intent) === chapter.chapter_id)}
        onSubmit={submit}
        onRetry={() => chapter.source_hash && chapter.version && submit({ operation: 'retry', chapter_id: chapter.chapter_id,
          source_hash: chapter.source_hash, version: chapter.version })} />)}
      {chapters.length === 0 && <p className="text-gray-500">本页暂无章节。</p>}
    </>}
    {(page > 0 || hasMore) && <div className="flex justify-between">
      <button className={buttonClass} disabled={loading || page === 0} onClick={() => setPage(p => p - 1)}>上一页章节</button>
      <button className={buttonClass} disabled={loading || !hasMore} onClick={() => setPage(p => p + 1)}>下一页章节</button>
    </div>}
  </section>
}

function CognitionChapter({ projectId, chapter, disabled, pending, onRetry, onSubmit }: {
  projectId: string; chapter: Chapter; disabled: boolean; pending: boolean; onRetry: () => void
  onSubmit: (intent: CognitionIntent) => Promise<void>
}) {
  const [expanded, setExpanded] = useState(false)
  const [editing, setEditing] = useState(false)
  const [history, setHistory] = useState(false)
  return <article className="space-y-2 rounded border border-gray-200 p-3">
    <div className="flex flex-wrap justify-between gap-2">
      <h3 className="font-medium">{chapter.global_idx ? `第 ${chapter.global_idx} 章 · ` : ''}{chapter.chapter_title}</h3>
      <span>{chapter.status === 'completed' && chapter.source_mode === 'author' ? '作者校订已生效' : labels[chapter.status] || '状态待核对'}</span>
    </div>
    <p className="text-xs text-gray-600">{chapter.message}</p>
    <p className="text-xs text-gray-500">{chapter.source_mode === 'author' ? '当前使用作者校订' : `本轮已执行 ${chapter.attempts} 次`}{chapter.changes_count !== null ? ` · ${chapter.changes_count} 条认知变化` : ''}</p>
    <div className="flex flex-wrap gap-2">
      {chapter.can_retry && !pending && <button disabled={disabled} className={buttonClass} onClick={onRetry}>
        {chapter.status === 'missing' ? '提取保存稿' : '重试本章提取'}</button>}
      {pending && <span className="text-xs text-amber-800">本章有待确认的提交，请在上方确认。</span>}
      {chapter.status === 'completed' && <button className={buttonClass} aria-expanded={expanded}
        onClick={() => setExpanded(value => !value)}>{expanded ? '收起原文依据' : '查看认知与原文依据'}</button>}
      {chapter.can_edit && <button className={buttonClass} aria-expanded={editing}
        onClick={() => setEditing(value => !value)}>{editing ? '收起校订（保留草稿）' : '校订认知条目'}</button>}
      <button className={buttonClass} aria-expanded={history} onClick={() => setHistory(value => !value)}>{history ? '收起校订历史' : '查看校订历史'}</button>
    </div>
    {expanded && chapter.status === 'completed' && <CognitionEvidence key={`${chapter.chapter_id}:${chapter.version}`}
      projectId={projectId} chapterId={chapter.chapter_id} expectedVersion={chapter.version} />}
    {editing && <CognitionEditor projectId={projectId} chapterId={chapter.chapter_id} currentVersion={chapter.version} disabled={disabled}
      pending={pending} onSubmit={onSubmit} />}
    {history && <CognitionHistory projectId={projectId} chapterId={chapter.chapter_id} currentVersion={chapter.version} />}
  </article>
}

function CognitionEvidence({ projectId, chapterId, expectedVersion }: { projectId: string; chapterId: string; expectedVersion: string | null }) {
  const [detail, setDetail] = useState<CognitionDetail | null>(null)
  const [error, setError] = useState('')
  useEffect(() => {
    const controller = new AbortController()
    let live = true
    void apiFetch<CognitionDetail>(`/api/projects/${projectId}/cognition/chapters/${chapterId}`, { signal: controller.signal })
      .then(data => {
        if (!live) return
        if (data.chapter_id !== chapterId || data.status !== 'completed' || data.version !== expectedVersion || !Array.isArray(data.changes)) {
          setError('保存稿或结果已变化，请刷新认知进度后再查看。'); return
        }
        setDetail(data)
      }).catch(() => { if (live) setError('无法读取原文依据，请收起后重试。') })
    return () => { live = false; controller.abort() }
  }, [projectId, chapterId, expectedVersion])
  if (error) return <p role="alert" className="text-red-700">{error}</p>
  if (!detail?.changes) return <p role="status">正在读取原文依据…</p>
  return <div className="space-y-3 border-t border-gray-100 pt-2">
    <p className="text-xs text-gray-500">以下为本章当时的知情记录。请结合上下文核对猜测、误解和真正获知的区别。</p>
    <CognitionChanges changes={detail.changes} emptyLabel={detail.source_mode === 'author'
      ? '作者确认本章不记录认知变化；这不表示人物什么都不知道。' : undefined} />
  </div>
}
