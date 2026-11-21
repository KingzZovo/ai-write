'use client'

import { useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react'
import { apiFetch } from '@/lib/api'
import { arcActionLabels, arcProgressPath, downloadArcRecord, type ArcAction } from '@/lib/arcProgress'
import { getArcProgressSession } from './arcProgressSession'
import { ArcManuscriptReview } from './ArcManuscriptReview'
import { ArcProgressHistory } from './ArcProgressHistory'
import { ArcPlanningPanel } from './ArcPlanningPanel'

type Volume = { id: string; title: string }
type Props = { projectId: string; volumes: Volume[]; currentVolumeId?: string; onOpenChapter?: (chapterId: string) => void; onCreated?: () => void | Promise<void> }
type ArchivePage = { items: { volume_id: string; title: string; deleted: boolean }[]; next_cursor: string | null }
const emptySnapshot = () => null
const button = 'min-h-10 rounded border px-3 py-2 disabled:opacity-50'

export function ArcProgressPanel(props: Props) { return <ArcProjectPanel key={props.projectId} {...props} /> }

function ArcProjectPanel({ projectId, volumes, currentVolumeId, onOpenChapter, onCreated }: Props) {
  const [selected, setSelected] = useState(currentVolumeId ?? volumes[0]?.id ?? '')
  const [archives, setArchives] = useState<ArchivePage['items']>([])
  const [cursor, setCursor] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null), [loading, setLoading] = useState(false)
  const controllerRef = useRef<AbortController | null>(null)
  const version = useRef(0)
  useEffect(() => () => { version.current += 1; controllerRef.current?.abort() }, [])
  const loadArchives = async (after?: string) => {
    if (controllerRef.current) return
    const request = ++version.current, controller = new AbortController()
    controllerRef.current = controller
    const timer = setTimeout(() => controller.abort(), 20_000)
    setLoading(true)
    try {
      const page = await apiFetch<ArchivePage>(arcProgressPath(projectId) + (after ? `?cursor=${encodeURIComponent(after)}` : ''), { signal: controller.signal })
      if (request !== version.current) return
      if (!page || !Array.isArray(page.items) || page.items.some(row => !row || typeof row.volume_id !== 'string' ||
          typeof row.title !== 'string' || typeof row.deleted !== 'boolean') ||
          (page.next_cursor !== null && page.next_cursor !== page.items.at(-1)?.volume_id)) throw new Error('历史弧目录返回不完整。')
      setArchives(previous => after ? [...previous, ...page.items.filter(row => !previous.some(old => old.volume_id === row.volume_id))] : page.items)
      setCursor(page.next_cursor); setError(null)
      if (!selected && page.items.length) setSelected(page.items[0].volume_id)
    } catch (cause) {
      if (request === version.current) setError(cause instanceof Error ? cause.message : '历史弧目录读取失败。')
    } finally { clearTimeout(timer); if (request === version.current) { controllerRef.current = null; setLoading(false) } }
  }
  const options = [...volumes, ...archives.filter(row => !volumes.some(volume => volume.id === row.volume_id))
    .map(row => ({ id: row.volume_id, title: row.title + (row.deleted ? '（已删除，查看历史）' : '') }))]
  const volumeId = selected || currentVolumeId || options[0]?.id || ''
  return <div className="space-y-4 text-sm">
    <p className="text-gray-600">弧是一段连续故事，使用分卷保存。由作者逐章审读确认完成，再决定后续方向。</p>
    <ArcPlanningPanel projectId={projectId} onCreated={async result => {
      await onCreated?.()
      setSelected(result.volume_id)
    }} />
    <label className="block">选择分卷<select aria-label="弧进度分卷" value={volumeId} onChange={event => setSelected(event.target.value)}
      className="mt-1 min-h-10 w-full rounded border bg-white p-2">
      {!volumeId && <option value="">请选择分卷</option>}
      {volumeId && !options.some(option => option.id === volumeId) && <option value={volumeId}>原分卷（查看历史）</option>}
      {options.map(volume => <option key={volume.id} value={volume.id}>{volume.title}</option>)}
    </select></label>
    <div className="flex flex-wrap gap-2"><button type="button" disabled={loading} onClick={() => void loadArchives()} className={button}>读取历史弧目录</button>
      {cursor && <button type="button" disabled={loading} onClick={() => void loadArchives(cursor)} className={button}>更多历史分卷</button>}</div>
    {loading && <p role="status">正在读取历史弧目录…</p>}
    {error && <p role="alert" className="text-red-700">{error}</p>}
    {volumeId ? <ArcVolume key={`${projectId}:${volumeId}`} projectId={projectId} volumeId={volumeId} onOpenChapter={onOpenChapter} />
      : <p>本书还没有可选择的分卷；已有的进度可从历史弧目录读取。</p>}
  </div>
}

function ArcVolume({ projectId, volumeId, onOpenChapter }: { projectId: string; volumeId: string; onOpenChapter?: (chapterId: string) => void }) {
  const session = useMemo(() => getArcProgressSession(projectId, volumeId), [projectId, volumeId])
  const state = useSyncExternalStore(session.subscribe, session.getSnapshot, emptySnapshot)
  const [reviewId, setReviewId] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [finishOpen, setFinishOpen] = useState(false), [historyOpen, setHistoryOpen] = useState(false)
  useEffect(() => { session.attach() }, [session])
  if (!state) return <p role="status">正在准备弧进度…</p>
  const view = state.view, busy = state.busy || state.loading || !!state.pending || !!state.storageError
  const mayFinish = !!view && view.issues.every(issue => issue.code === 'completion_changed')
  const operate = async (action: ArcAction, chapterId: string | null = null) => {
    setActionError(null)
    try { await session.submit(action, null, chapterId); setFinishOpen(false) }
    catch (cause) { setActionError(cause instanceof Error ? cause.message : '操作尚未确认。') }
  }
  return <div className="space-y-4">
    <div className="flex flex-wrap items-center justify-between gap-2"><h2 className="font-semibold">{view?.title || '本弧进度'}</h2>
      <button type="button" disabled={state.busy || state.loading} onClick={() => void session.reload()} className={button}>刷新进度</button></div>
    {state.loading && <p role="status">正在核对当前保存稿与进度…</p>}
    {(state.error || actionError) && <p role="alert" className="text-red-700">{state.error || actionError}</p>}
    {state.storageError && <div className="space-y-2 text-amber-800"><p role="alert">{state.storageError}</p>
      <button type="button" disabled={state.busy} onClick={session.recoverStorage} className={button}>重新核对暂存</button>
      <button type="button" onClick={() => downloadArcRecord(state.unreadableDraft ?? JSON.stringify({ notes: state.notes, direction: state.direction,
        closingNote: state.closingNote, pending: state.pending }, null, 2), '待核对弧进度输入.json')} className={button}>下载原输入</button></div>}
    {state.pending && <section className="space-y-2 rounded border border-amber-300 bg-amber-50 p-3" aria-label="待确认的弧进度操作">
      <p>{state.pending.rejected ? '本次操作未被接受，输入仍保留。' : '本次操作的保存结果尚未确认，请核对原回执。'}</p>
      <div className="flex flex-wrap gap-2">
        <button type="button" disabled={state.busy} onClick={() => void session.checkReceipt()} className={button}>查询原回执</button>
        <button type="button" disabled={state.busy || state.unreadableDraft !== null} onClick={() => { void session.retry().catch(() => {}) }} className={button}>重试原请求</button>
        {state.pending.rejected && <button type="button" disabled={state.busy} onClick={session.dismissRejected} className={button}>保留输入，重新核对</button>}
        <button type="button" onClick={() => downloadArcRecord(state.pending!.body, '原弧进度请求.json')} className={button}>下载原请求</button>
      </div>
    </section>}
    {state.receipt && !state.pending && <p role="status" className="text-green-800">已确认保存：{arcActionLabels[state.receipt.action]}。当前进度以重新读取的结果为准。</p>}
    {view && <>
      <p>作者已确认 {view.confirmed_count} / {view.chapter_count} 章。
        {view.progress.phase === 'completed' ? (view.progress.closed_early ? ' 本弧由作者提前结束。' : ' 本弧已结束。') : ' 本弧可继续核对与创作。'}</p>
      {view.legacy.unverified && <p className="text-amber-800">旧报告记录了 {view.legacy.reported_count} 章，尚未核对保存稿，未计入已确认章节。</p>}
      {view.issues.length > 0 && <ul className="list-disc space-y-1 pl-5 text-amber-800">{view.issues.map((issue, i) => <li key={`${issue.code}:${issue.chapter_id ?? i}`}>{issue.message}</li>)}</ul>}
      {view.issues.some(issue => ['plan_changed', 'order_changed', 'legacy_unverified', 'legacy_structure_unverified'].includes(issue.code)) && <button type="button" disabled={busy || view.issues.some(issue => ['manuscript_changed', 'chapter_removed'].includes(issue.code))}
        onClick={() => void operate('reconcile')} className={button}>已核对，采用当前弧安排</button>}
      <div className="space-y-2">{view.chapters.map(chapter => <div key={chapter.id} className="rounded border p-3">
        <p className="font-medium">{chapter.chapter_idx ? `第${chapter.chapter_idx}章 ` : ''}{chapter.title}</p>
        <p className="text-xs text-gray-600">{{ pending: '待审读确认', confirmed: '作者已确认', changed: '保存稿已变化，待重核', removed: '章节已移出或删除' }[chapter.state]}</p>
        <div className="mt-2 flex flex-wrap gap-2">
          {chapter.state !== 'removed' && <button type="button" onClick={() => setReviewId(chapter.id)} className={button}>审读保存稿</button>}
          {chapter.confirmation && <button type="button" disabled={busy} onClick={() => void operate('retract', chapter.id)} className={button}>撤销此章确认</button>}
        </div>
      </div>)}</div>
      {reviewId && view.chapters.some(chapter => chapter.id === reviewId && chapter.state !== 'removed') && <ArcManuscriptReview key={`${projectId}:${reviewId}`}
        projectId={projectId} chapterId={reviewId} session={session} state={state} onOpenChapter={onOpenChapter} />}
      <section className="space-y-2 rounded border p-3">
        <label className="block">下一步方向<textarea aria-label="本弧下一步方向" value={state.direction} onChange={event => session.edit('direction', event.target.value)}
          className="mt-1 min-h-24 w-full rounded border p-2" /></label>
        {view.progress.direction_chapter_id && <p className="text-xs text-gray-600">已保存方向对应：{view.chapters.find(c => c.id === view.progress.direction_chapter_id)?.title ?? '原章节已移出'}</p>}
        <div className="flex flex-wrap gap-2"><button type="button" disabled={busy || view.issues.length > 0 || !view.next_chapter_id || !state.direction.trim()}
          onClick={() => void operate('direction')} className={button}>给当前待写章保存方向</button>
          {view.next_chapter_id && onOpenChapter && <button type="button" disabled={busy} onClick={() => onOpenChapter(view.next_chapter_id!)}
            className={button}>去写下一章</button>}
          {view.progress.phase === 'completed' && <button type="button" disabled={busy} onClick={() => void operate('reopen')} className={button}>继续本弧</button>}
          {(view.progress.phase !== 'completed' || view.issues.some(issue => issue.code === 'completion_changed')) &&
            <button type="button" disabled={busy || !mayFinish} onClick={() => setFinishOpen(open => !open)} className={button}>结束本弧…</button>}
        </div>
        {finishOpen && <div className="space-y-2 border-t pt-2"><p>结束后仍保留 {view.chapter_count - view.confirmed_count} 章未确认记录。</p>
          <label className="block">结束说明（可选）<textarea aria-label="结束本弧说明" value={state.closingNote} onChange={event => session.edit('closingNote', event.target.value)} className="mt-1 w-full rounded border p-2" /></label>
          <button type="button" disabled={busy || !mayFinish} onClick={() => void operate('finish')} className={button}>确认结束，保留未完成章节</button></div>}
      </section>
    </>}
    <button type="button" onClick={() => setHistoryOpen(open => !open)} aria-expanded={historyOpen} className={button}>{historyOpen ? '收起进度历史' : '查看进度历史'}</button>
    {historyOpen && <ArcProgressHistory key={`${projectId}:${volumeId}`} projectId={projectId} volumeId={volumeId}
      revision={view?.source.latest?.request_id ?? state.receipt?.request_id} />}
  </div>
}
