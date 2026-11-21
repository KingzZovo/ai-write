'use client'

import { useEffect, useState } from 'react'
import { useChapterDraft } from '@/components/editor/useChapterDraft'
import { readManuscript, type ManuscriptSource } from '@/lib/chapterManuscripts'
import type { ArcProgressSession, ArcProgressState } from './arcProgressSession'

export function ArcManuscriptReview({ projectId, chapterId, session, state, onOpenChapter }: {
  projectId: string; chapterId: string; session: ArcProgressSession; state: ArcProgressState
  onOpenChapter?: (chapterId: string) => void
}) {
  const [manuscript, setManuscript] = useState<ManuscriptSource | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [read, setRead] = useState(false)
  const [revision, setRevision] = useState(0)
  const { session: draft, state: draftState } = useChapterDraft(projectId, chapterId)

  useEffect(() => {
    let active = true
    const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 20_000)
    setLoading(true); setRead(false); setManuscript(null); setError(null)
    readManuscript(projectId, chapterId, controller.signal).then(value => {
      if (active) setManuscript(value)
    }).catch(cause => {
      if (active) setError(cause instanceof Error ? cause.message : '保存稿读取失败。')
    }).finally(() => { clearTimeout(timer); if (active) setLoading(false) })
    return () => { active = false; clearTimeout(timer); controller.abort() }
  }, [projectId, chapterId, revision])

  const sameDraft = !!manuscript && !!draft?.canGenerate() && draftState?.text === manuscript.content_text &&
    draftState.source?.title === manuscript.title && draftState.source?.content_text === manuscript.content_text
  const chapter = state.view?.chapters.find(item => item.id === chapterId)
  const blocked = state.busy || !!state.pending || state.loading || !!state.storageError ||
    state.view?.progress.phase === 'completed' || !chapter || chapter.state === 'removed' ||
    (state.view?.issues.length !== 0 && !chapter.confirmation)
  const confirm = async () => {
    if (!manuscript || !read || !sameDraft || blocked || !manuscript.content_text.trim()) return
    // Check again at the event boundary; a saving/generation event may have
    // arrived since the last render. Never confirm a hidden editor draft.
    const current = draft?.getSnapshot()
    if (!draft?.canGenerate() || current?.text !== manuscript.content_text || current.source?.title !== manuscript.title) return
    try { await session.submit('acknowledge', manuscript); setRead(false) }
    catch (cause) { setError(cause instanceof Error ? cause.message : '本章确认尚未完成。') }
  }
  return <section className="space-y-3 rounded-lg border bg-gray-50 p-3" aria-label="审读本章保存稿">
    <div className="flex flex-wrap items-center justify-between gap-2">
      <h3 className="font-semibold">{manuscript?.title ?? '本章保存稿'}</h3>
      <button type="button" className="min-h-10 text-blue-800" disabled={loading || state.busy}
        onClick={() => { setRevision(value => value + 1); void draft?.reload() }}>重新读取全文</button>
    </div>
    <p className="text-xs leading-relaxed text-gray-600">请按正文审读人物选择、事情的结果和前后承接。此处确认的是下面这份完整保存稿。</p>
    {loading && <p role="status">正在读取保存稿…</p>}
    {error && <p role="alert" className="text-red-700">{error}</p>}
    {manuscript && <>
      <textarea readOnly aria-label="本章完整保存稿" value={manuscript.content_text} className="min-h-80 w-full rounded border bg-white p-3 leading-7" />
      {!manuscript.content_text.trim() && <p className="text-amber-800">本章保存稿为空，请先完成并保存正文。</p>}
      {!sameDraft && <p className="text-amber-800">编辑稿尚未与这份保存稿核对一致。请先处理未保存修改或正在进行的保存，再重新读取。</p>}
      <label className="block">本章进展说明（可选）<textarea aria-label="本章进展说明" value={state.notes[chapterId] ?? ''}
        onChange={event => session.editNote(chapterId, event.target.value)} className="mt-1 min-h-20 w-full rounded border p-2" /></label>
      <label className="flex min-h-10 items-start gap-2 py-2"><input type="checkbox" checked={read} onChange={event => setRead(event.target.checked)} className="mt-1" />
        <span>我已读过这份保存稿，确认本章完成</span></label>
      <div className="flex flex-wrap gap-3">
        <button type="button" onClick={() => void confirm()} disabled={blocked || !sameDraft || !read || !manuscript.content_text.trim()}
          className="min-h-10 rounded bg-blue-700 px-3 text-white disabled:opacity-50">确认这份保存稿</button>
        {onOpenChapter && <button type="button" onClick={() => onOpenChapter(chapterId)} className="min-h-10 text-blue-800">回到本章编辑</button>}
      </div>
    </>}
  </section>
}
