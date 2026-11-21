'use client'

import { useEffect, useRef, useState } from 'react'
import { apiFetch, ApiError } from '@/lib/api'
import {
  Foreshadow, clearForeshadowDraftIfCurrent, draftKey, foreshadowAttempt, foreshadowHeaders,
  readForeshadowDraft, storeForeshadowDraft,
} from '@/lib/foreshadowWrites'
import { SourceChapterPage, SourceContext, sourcePositionLabel, validSourceContext, selectedSourceText } from '@/lib/foreshadowSources'

type Draft = { version: 1; chapterId: string; evidence: string; note: string }
type Pending = {
  attempt: ReturnType<typeof foreshadowAttempt>; method: 'POST' | 'DELETE'
  payload: { expected_link_id: string | null; note: string; chapter_id?: string; source_version?: string; evidence?: string }
  draft: Draft
}
type Props = { projectId: string; foreshadow: Foreshadow; defaultChapterId?: string; onDone: () => void; onClose: () => void }

function loadDraft(key: string, chapterId = ''): Draft {
  const stored = readForeshadowDraft(key) as Partial<Draft> | undefined
  return stored?.version === 1 && typeof stored.chapterId === 'string' && typeof stored.evidence === 'string' && typeof stored.note === 'string'
    ? stored as Draft : { version: 1, chapterId, evidence: '', note: '' }
}

function loadPending(key: string): Pending | null {
  const stored = readForeshadowDraft(key) as Pending | undefined
  return stored && ['POST', 'DELETE'].includes(stored.method) && typeof stored.attempt?.id === 'string' &&
    /^[0-9a-f]{64}$/.test(stored.attempt.version || '') && stored.payload && stored.draft?.version === 1 ? stored : null
}

export function ForeshadowSourceForm(props: Props) {
  return <SourceFormSession key={`${props.projectId}:${props.foreshadow.id}`} {...props} />
}

function SourceFormSession({ projectId, foreshadow, defaultChapterId, onDone, onClose }: Props) {
  const key = draftKey(projectId, `source:${foreshadow.id}`)
  const pendingKey = `${key}:pending`, attemptKey = `${key}:attempt`
  const [draft, setDraft] = useState(() => loadDraft(key, defaultChapterId))
  const [pending, setPending] = useState(() => loadPending(pendingKey))
  const [context, setContext] = useState<SourceContext | null>(null)
  const [page, setPage] = useState(0)
  const [reload, setReload] = useState(0)
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [needsRefresh, setNeedsRefresh] = useState(false)
  const [readError, setReadError] = useState('')
  const [saveError, setSaveError] = useState('')
  const active = useRef(true), inFlight = useRef(false), requestVersion = useRef(0)
  const prose = useRef<HTMLTextAreaElement>(null)
  useEffect(() => { active.current = true; return () => { active.current = false; requestVersion.current += 1 } }, [])

  useEffect(() => {
    const version = ++requestVersion.current
    let cancelled = false
    setLoading(true); setReadError(''); setContext(null)
    const params = new URLSearchParams({ limit: '20', offset: String(page * 20) })
    if (draft.chapterId) params.set('chapter_id', draft.chapterId)
    void apiFetch<SourceContext>(`/api/projects/${projectId}/foreshadows/${foreshadow.id}/source?${params}`)
      .then(data => {
        if (cancelled || version !== requestVersion.current) return
        if (!validSourceContext(data, foreshadow.id, draft.chapterId)) throw new Error('invalid source response')
        setContext(data); setNeedsRefresh(false)
      }).catch(error => {
        if (!cancelled && version === requestVersion.current) setReadError(
          `${error instanceof ApiError ? error.message : '来源读取失败。'} 引用和说明仍保留；可重新选择章节，或不选正文查看关联历史。`)
      }).finally(() => { if (!cancelled && version === requestVersion.current) setLoading(false) })
    return () => { cancelled = true }
  }, [projectId, foreshadow.id, draft.chapterId, page, reload])

  const change = (patch: Partial<Draft>) => {
    const next = { ...draft, ...patch }
    storeForeshadowDraft(key, next); setDraft(next)
  }
  const choose = (chapterId: string) => {
    if (chapterId === draft.chapterId) return
    requestVersion.current += 1
    setContext(null); setPage(0)
    change({ chapterId, evidence: '' })
  }
  const clearPending = (sent: Pending) => {
    clearForeshadowDraftIfCurrent(pendingKey, sent)
    clearForeshadowDraftIfCurrent(attemptKey, sent.attempt)
    if (active.current) setPending(null)
  }
  const send = async (sent: Pending) => {
    if (inFlight.current) return
    inFlight.current = true
    setSaving(true); setSaveError('')
    try {
      const result = await apiFetch<{ id: string; operation: 'link' | 'unlink'; is_latest: boolean }>(
        `/api/projects/${projectId}/foreshadows/${foreshadow.id}/source`, {
          method: sent.method, headers: foreshadowHeaders(sent.attempt), body: JSON.stringify(sent.payload),
        })
      if (result?.id !== sent.attempt.id || result.operation !== (sent.method === 'POST' ? 'link' : 'unlink') || typeof result.is_latest !== 'boolean') {
        throw new Error('尚未取得这次关联操作的确认，请保留输入并重试。')
      }
      clearPending(sent)
      if (!result.is_latest) {
        if (active.current) { setNeedsRefresh(true); setSaveError('这次操作曾已完成，随后关联又有变化。输入已保留，请重新读取并核对。') }
        return
      }
      clearForeshadowDraftIfCurrent(key, sent.draft)
      if (active.current) onDone()
    } catch (error) {
      if (error instanceof ApiError && [401, 403, 404, 409, 422].includes(error.status)) {
        clearPending(sent)
        if (active.current) setNeedsRefresh(!error.code?.startsWith('quote_'))
      }
      if (active.current) setSaveError(error instanceof Error ? error.message : '关联尚未确认，输入已保留。')
    } finally {
      inFlight.current = false
      if (active.current) setSaving(false)
    }
  }
  const submit = (method: 'POST' | 'DELETE') => {
    if (inFlight.current || pending || !context?.record_present || !context.record_version || loading || needsRefresh) return
    if (method === 'POST' && (!context.preview || context.preview.chapter_id !== draft.chapterId || !draft.evidence.trim())) return
    const payload: Pending['payload'] = { expected_link_id: context.latest_link?.id || null, note: draft.note }
    if (method === 'POST') Object.assign(payload, { chapter_id: draft.chapterId, source_version: context.preview!.source_version, evidence: draft.evidence })
    const attempt = foreshadowAttempt(attemptKey, JSON.stringify({ method, payload, version: context.record_version }), context.record_version)
    const sent: Pending = { attempt, payload, method, draft }
    storeForeshadowDraft(key, draft); storeForeshadowDraft(pendingKey, sent); setPending(sent)
    void send(sent)
  }
  const locked = saving || Boolean(pending)
  const quoteSelection = () => {
    const editor = prose.current
    if (editor && context?.preview && editor.selectionStart !== editor.selectionEnd) {
      const quote = selectedSourceText(context.preview.content, editor.value, editor.selectionStart, editor.selectionEnd)
      if (quote === null) setSaveError('选区与保存稿不一致，请重新选取原句。')
      else { change({ evidence: quote }); setSaveError('') }
    }
  }

  return <section aria-label="关联伏笔正文" className="space-y-3 rounded-lg border border-blue-200 bg-blue-50/30 p-3 text-xs">
    <div className="flex items-center justify-between gap-2">
      <h4 className="font-semibold text-stone-700">关联正文原句</h4>
      <button onClick={onClose} disabled={saving} className="text-stone-500 disabled:opacity-40">收起（保留输入）</button>
    </div>
    <p className="whitespace-pre-wrap text-stone-700">伏笔：{context?.description || foreshadow.description}</p>
    {context && !context.record_present && <p className="text-amber-700">伏笔记录已删除，保留的关联历史可继续查看。</p>}
    <p className="text-stone-500">请选择实际埋设线索的章节，对照已保存正文引用原句。关联由作者确认；是否铺垫充分、是否已兑现，仍需结合情节判断。</p>
    <ChapterChooser projectId={projectId} selectedId={draft.chapterId} disabled={locked} onChoose={choose} />
    <button disabled={saving || loading} onClick={() => { requestVersion.current += 1; setReload(value => value + 1) }} className="text-blue-600 disabled:opacity-40">重新读取并核对保存稿</button>
    {loading && <p role="status">正在读取保存稿及关联记录…</p>}
    {readError && <p role="alert" className="text-rose-600">{readError}</p>}
    {context?.preview && <>
      <p className="text-stone-600">{sourcePositionLabel(context.preview.source_snapshot)} · {context.preview.title}（已保存正文）</p>
      <textarea ref={prose} aria-label="关联章节的已保存正文" readOnly value={context.preview.content} className="h-60 w-full resize-y rounded border border-stone-200 bg-white p-2 leading-relaxed" />
      <button disabled={locked} onClick={quoteSelection} className="text-blue-600 disabled:opacity-40">引用所选文字</button>
    </>}
    <label className="block text-stone-600">引用原句
      <textarea aria-label="引用原句" readOnly={locked} value={draft.evidence} onChange={event => change({ evidence: event.target.value })} className="mt-1 h-20 w-full rounded border border-stone-200 p-2" />
    </label>
    <p className="text-stone-500">原句需与保存稿逐字一致；重复出现时，请多引用一些前后文。</p>
    <label className="block text-stone-600">关联说明（可选）
      <textarea aria-label="关联说明" readOnly={locked} value={draft.note} onChange={event => change({ note: event.target.value })} className="mt-1 h-16 w-full rounded border border-stone-200 p-2" />
    </label>
    {saveError && <p role="alert" className="text-rose-600">{saveError}</p>}
    {pending ? <>
      <p className="text-stone-600">上次操作尚待确认，请先用原请求重试。</p>
      <button disabled={saving} onClick={() => void send(pending)} className="rounded bg-blue-600 px-3 py-1.5 text-white disabled:opacity-40">{saving ? '保存中…' : '重试确认关联操作'}</button>
    </> : <div className="flex flex-wrap gap-3">
      <button disabled={saving || loading || needsRefresh || !context?.record_present || !context.preview || !draft.evidence.trim()} onClick={() => submit('POST')} className="rounded bg-blue-600 px-3 py-1.5 text-white disabled:opacity-40">确认原句并关联</button>
      {(context?.latest_link?.operation === 'link' || (!context?.latest_link && foreshadow.source_kind === 'prose')) && <button disabled={saving || loading || needsRefresh || !context?.record_present} onClick={() => submit('DELETE')} className="text-rose-600 disabled:opacity-40">解除正文关联</button>}
    </div>}
    {context && <div className="space-y-2 border-t border-stone-200 pt-2">
      <p className="font-medium text-stone-700">作者关联历史</p>
      {context.history.length === 0 && <p className="text-stone-500">尚无人工关联记录；已有候选登记证据仍可在伏笔卡片核对。</p>}
      {context.history.map(link => <article key={link.id} className="space-y-1 rounded border border-stone-200 bg-white p-2">
        <p className="text-stone-600">{link.operation === 'unlink' ? '解除关联' : `关联至${sourcePositionLabel(link.source_snapshot)}`} · {link.created_by || '未记录账号'}</p>
        <p className="text-stone-400">{link.created_at}</p>
        {link.record_snapshot.description && <p className="whitespace-pre-wrap">当时的登记：{link.record_snapshot.description}</p>}
        {link.evidence && <blockquote className="whitespace-pre-wrap border-l-2 border-stone-200 pl-2">{link.evidence}</blockquote>}
        {link.note && <p className="whitespace-pre-wrap text-stone-600">{link.note}</p>}
      </article>)}
      {(page > 0 || context.has_more) && <div className="flex justify-between">
        <button disabled={locked || loading || page === 0} onClick={() => setPage(value => value - 1)}>上一页关联记录</button>
        <span>第 {page + 1} 页</span>
        <button disabled={locked || loading || !context.has_more} onClick={() => setPage(value => value + 1)}>下一页关联记录</button>
      </div>}
    </div>}
  </section>
}

function ChapterChooser({ projectId, selectedId, disabled, onChoose }: {
  projectId: string; selectedId: string; disabled: boolean; onChoose: (id: string) => void
}) {
  const [input, setInput] = useState(''), [query, setQuery] = useState('')
  const [page, setPage] = useState(0), [reload, setReload] = useState(0)
  const [result, setResult] = useState<SourceChapterPage | null>(null)
  const [loading, setLoading] = useState(false), [error, setError] = useState('')
  useEffect(() => {
    let cancelled = false
    setLoading(true); setError(''); setResult(null)
    const params = new URLSearchParams({ q: query, limit: '30', offset: String(page * 30) })
    void apiFetch<SourceChapterPage>(`/api/projects/${projectId}/foreshadows/source-chapters?${params}`)
      .then(data => {
        if (cancelled) return
        if (!Array.isArray(data?.chapters) || typeof data.has_more !== 'boolean' ||
            data.chapters.some(chapter => !chapter || typeof chapter.id !== 'string' || typeof chapter.title !== 'string' ||
              !Number.isInteger(chapter.volume_idx) || !Number.isInteger(chapter.chapter_idx))) throw new Error('invalid chapters response')
        setResult(data)
      }).catch(() => { if (!cancelled) setError('章节列表读取失败，请重试搜索。') })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [projectId, query, page, reload])
  return <div className="space-y-2">
    <form onSubmit={event => { event.preventDefault(); setQuery(input.trim()); setPage(0); setReload(value => value + 1) }} className="flex gap-2">
      <input aria-label="搜索来源章节" placeholder="搜索章名、卷名或章号" value={input} maxLength={100} disabled={disabled} onChange={event => setInput(event.target.value)} className="min-w-0 flex-1 rounded border border-stone-200 px-2 py-1" />
      <button disabled={disabled || loading} className="text-blue-600 disabled:opacity-40">搜索章节</button>
    </form>
    <label className="block text-stone-600">来源章节
      <select aria-label="来源章节" value={selectedId} disabled={disabled || loading} onChange={event => onChoose(event.target.value)} className="mt-1 w-full rounded border border-stone-200 p-1.5">
        <option value="">不选正文，仅查看关联历史</option>
        {selectedId && !result?.chapters.some(chapter => chapter.id === selectedId) && <option value={selectedId}>已选章节（见保存稿）</option>}
        {result?.chapters.map(chapter => <option key={chapter.id} value={chapter.id}>{sourcePositionLabel(chapter)} · {chapter.title}</option>)}
      </select>
    </label>
    {error && <p role="alert" className="text-rose-600">{error}</p>}
    {!loading && result?.chapters.length === 0 && <p className="text-stone-500">没有匹配的章节。</p>}
    {(page > 0 || result?.has_more) && <div className="flex justify-between">
      <button disabled={disabled || loading || page === 0} onClick={() => setPage(value => value - 1)}>上一页章节</button>
      <span>第 {page + 1} 页</span>
      <button disabled={disabled || loading || !result?.has_more} onClick={() => setPage(value => value + 1)}>下一页章节</button>
    </div>}
  </div>
}
