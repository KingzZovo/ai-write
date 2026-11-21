'use client'

import { useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react'
import { apiFetch } from '@/lib/api'
import { arcPlanLabels, arcPlanPath, parseArcPlanPage, type ArcPlanDetail, type ArcPlanResult, type ArcPlanStatus } from '@/lib/arcPlanning'
import { downloadArcRecord } from '@/lib/arcProgress'
import { getArcPlanningSession } from './arcPlanningSession'
import { PlanningOriginal, PlanningAttemptInputs } from './ArcPlanningInputs'

const button = 'min-h-10 rounded border px-3 py-2 disabled:opacity-50'
const emptySnapshot = () => null
const materialLabels = { idea: '这段故事想写什么', background: '已有背景', core_setup: '本弧核心设定', opening_scene: '开场情境' }
const fieldLabels: Record<string, string> = {
  core_conflict: '主要处境与问题', emotional_arc: '人物态度与情感变化', volume_start_state: '起点与未完事项',
  volume_end_state: '本弧计划结果', transition_to_next: '收束与后续衔接', foreshadow_progression: '线索安排',
  summary: '剧情详解', key_events: '可展开的过程', start_state: '承接状态', end_state: '本章结果',
  handoff_to_next: '交给下一章的事项', character_choices: '人物选择与依据', narrative_focus: '场面详略',
  entity_transfers: '物件与资源交接', unresolved_threads: '未完事项', knowledge_changes: '人物知情变化',
}
const text = (value: unknown) => typeof value === 'string' ? value : JSON.stringify(value, null, 2)
const record = (value: unknown): value is Record<string, unknown> => !!value && typeof value === 'object' && !Array.isArray(value)

function PlanFields({ data, exclude = [] }: { data: Record<string, unknown>; exclude?: string[] }) {
  return <dl className="space-y-3">{Object.entries(data).filter(([key]) => !exclude.includes(key)).map(([key, value]) =>
    <div key={key}><dt className="font-medium">{fieldLabels[key] ?? `补充材料：${key}`}</dt>
      <dd className="mt-1 whitespace-pre-wrap break-words text-gray-700">{text(value)}</dd></div>)}</dl>
}

function UnfinishedOutputs({ detail }: { detail: ArcPlanDetail }) {
  if (!record(detail.journal) || !Array.isArray(detail.journal.stages)) return null
  return <>{detail.journal.stages.flatMap((stage, stageIndex) => {
    if (!record(stage) || !Array.isArray(stage.attempts)) return []
    return stage.attempts.flatMap((attempt, attemptIndex) => {
      if (!record(attempt) || attempt.state === 'accepted' || typeof attempt.raw_text !== 'string' || !attempt.raw_text) return []
      return [<details key={`${stageIndex}:${attemptIndex}`} className="rounded border border-amber-200 p-3">
        <summary className="cursor-pointer font-medium">第 {stageIndex + 1} 阶段、第 {attemptIndex + 1} 次未完成的原输出</summary>
        <p className="my-2 text-amber-800">{typeof attempt.error === 'string' ? attempt.error : '等待核对'}</p>
        <pre className="whitespace-pre-wrap break-words font-sans">{attempt.raw_text}</pre></details>]
    })
  })}</>
}

function PlanReview({ detail }: { detail: ArcPlanDetail }) {
  if (!detail.integrity_valid) return <p role="alert" className="text-amber-800">原记录无法完整核对。请下载保留材料，当前记录不会用于继续生成。</p>
  const outline = detail.accepted_outline
  return <div className="space-y-3">
    <details className="rounded border p-3"><summary className="cursor-pointer font-medium">本次原作者材料</summary>
      <dl className="mt-3 space-y-3">{Object.entries(materialLabels).map(([key, label]) => <div key={key}><dt className="font-medium">{label}</dt>
        <dd className="whitespace-pre-wrap break-words">{detail.request[key as keyof typeof materialLabels] || '未填写'}</dd></div>)}</dl>
    </details>
    {detail.book_material_text && <details className="rounded border p-3"><summary className="cursor-pointer font-medium">本次传入的全书创作材料</summary>
      <p className="mt-3 whitespace-pre-wrap break-words">{detail.book_material_text}</p></details>}
    {detail.cognition_material_text ? <PlanningOriginal label="本次采用的完整人物认知原件" text={detail.cognition_material_text} /> : null}
    <PlanningAttemptInputs journal={detail.journal} />
    {outline && <>
      {typeof outline.title === 'string' && <details open className="rounded border p-3"><summary className="cursor-pointer font-medium">{outline.title}：整体安排</summary>
        <div className="mt-3"><PlanFields data={outline} exclude={['title', 'chapter_count', 'chapter_summaries']} /></div></details>}
      {outline.chapter_summaries.map(chapter => <details key={String(chapter.chapter_idx)} className="rounded border p-3">
        <summary className="cursor-pointer font-medium">第 {String(chapter.chapter_idx)} 章 · {String(chapter.title)}</summary>
        <div className="mt-3"><PlanFields data={chapter} exclude={['chapter_idx', 'title']} /></div></details>)}
    </>}
    <UnfinishedOutputs detail={detail} />
  </div>
}

export function ArcPlanningPanel({ projectId, onCreated }: { projectId: string; onCreated?: (result: ArcPlanResult) => void | Promise<void> }) {
  const session = useMemo(() => getArcPlanningSession(projectId), [projectId])
  const state = useSyncExternalStore(session.subscribe, session.getSnapshot, emptySnapshot)
  const [formOpen, setFormOpen] = useState(false), [historyOpen, setHistoryOpen] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null), [opening, setOpening] = useState(false)
  useEffect(() => { session.attach() }, [session])
  const selected = state?.selected, status = state?.status?.status
  useEffect(() => {
    if (!selected || (status !== 'pending' && status !== 'running') || state?.pending) return
    const timer = setInterval(() => { if (!session.getSnapshot().busy && !session.getSnapshot().loading) void session.reload() }, 5000)
    return () => clearInterval(timer)
  }, [session, selected, status, state?.pending])
  if (!state) return <p role="status">正在准备弧规划…</p>
  const busy = state.busy || state.loading, blocked = busy || !!state.pending || !!state.storageError
  const perform = async (operation: () => Promise<unknown>) => {
    setActionError(null)
    try { await operation() } catch (cause) { setActionError(cause instanceof Error ? cause.message : '操作未能完成，原输入仍保留。') }
  }
  return <section className="space-y-3 rounded border p-3" aria-label="故事弧规划与恢复">
    <h2 className="font-semibold">规划新的故事弧</h2>
    <p className="text-gray-600">按原材料分批展开剧情，完成后创建分卷及全部章节草稿。途中退出后可继续原任务。</p>
    <div className="flex flex-wrap gap-2">
      <button type="button" className={button} onClick={() => setFormOpen(open => !open)} aria-expanded={formOpen}>{formOpen ? '收起创作材料' : '填写创作材料'}</button>
      <button type="button" className={button} onClick={() => setHistoryOpen(open => !open)} aria-expanded={historyOpen}>{historyOpen ? '收起规划历史' : '查看规划历史'}</button>
    </div>
    {(state.error || actionError) && <p role="alert" className="text-red-700">{state.error || actionError}</p>}
    {state.storageError && <div className="space-y-2 rounded bg-amber-50 p-3"><p role="alert">{state.storageError}</p>
      <div className="flex flex-wrap gap-2"><button type="button" className={button} disabled={state.busy} onClick={session.recoverStorage}>重新核对暂存</button>
        <button type="button" className={button} onClick={() => downloadArcRecord(state.unreadable ?? JSON.stringify({ draft: state.draft, pending: state.pending }, null, 2), '原弧规划输入.json')}>下载原输入</button></div></div>}
    {state.pending && <div className="space-y-2 rounded bg-amber-50 p-3" aria-label="待确认的弧规划请求">
      <p>{state.pending.rejected ? '本次请求未被接受，完整输入仍保留。' : '原请求的受理结果尚未核对，请查询原任务或重试同一请求。'}</p>
      <div className="flex flex-wrap gap-2"><button type="button" className={button} disabled={busy} onClick={() => void session.reload(true)}>查询原请求</button>
        <button type="button" className={button} disabled={busy || !!state.storageError} onClick={() => void perform(session.retry)}>重试原请求</button>
        {state.pending.rejected && <button type="button" className={button} disabled={busy} onClick={session.releaseRejected}>保留材料，重新核对</button>}
        <button type="button" className={button} onClick={() => downloadArcRecord(JSON.stringify(state.pending!.input, null, 2), '原弧规划请求.json')}>下载原请求</button></div>
    </div>}
    {formOpen && <form className="space-y-3" onSubmit={event => { event.preventDefault(); void perform(session.submit) }}>
      {Object.entries(materialLabels).map(([key, label]) => <label key={key} className="block">{label}{key === 'idea' ? '（必填）' : '（可选）'}
        <textarea aria-label={label} value={state.draft[key as keyof typeof materialLabels]} disabled={blocked}
          onChange={event => session.edit(key as keyof typeof materialLabels, event.target.value)}
          className="mt-1 min-h-24 w-full rounded border p-2 disabled:bg-gray-50" /></label>)}
      <label className="block">本弧章数（4–40）<input aria-label="本弧章数" type="number" min={4} max={40} step={1} value={state.draft.target_chapters}
        disabled={blocked} onChange={event => session.edit('target_chapters', event.target.value)} className="ml-2 min-h-10 w-24 rounded border p-2" /></label>
      <button type="button" disabled={blocked || state.sourceLoading} onClick={() => void session.reloadSource()} className={button}>读取当前分卷与前弧来源</button>
      {state.sourceLoading && <p role="status">正在核对前弧与分卷顺序…</p>}
      {state.source && <p>计划追加为第 {state.source.target_volume_idx} 卷。{state.source.mode === 'next' ? '本次使用已结束前弧的当前规划与作者进度。' : '本次为本书首次故事弧规划。'}</p>}
      {state.source?.previous_material && <details className="rounded border p-3"><summary className="cursor-pointer">查看传入的前弧材料</summary>
        <pre className="mt-2 whitespace-pre-wrap break-words font-sans">{text(state.source.previous_material)}</pre></details>}
      {state.source?.book_material_text && <details className="rounded border p-3"><summary className="cursor-pointer">查看传入的全书创作材料</summary>
        <p className="mt-2 whitespace-pre-wrap break-words">{state.source.book_material_text}</p></details>}
      {state.source?.cognition_material_text ? <PlanningOriginal label="查看当前完整人物认知原件" text={state.source.cognition_material_text} /> : null}
      <p className="text-gray-600">请审读人物为何行动、事情怎样完成、物件与金额如何交接，以及每章是否兑现安排。生成的规划仍需作者判断。</p>
      <button type="submit" className={button} disabled={blocked || state.sourceLoading || !state.source || !state.draft.idea.trim() ||
        (!!state.status && ['pending', 'running'].includes(state.status.status))}>开始规划并创建章草稿</button>
    </form>}
    {historyOpen && <PlanningHistory key={projectId} projectId={projectId} disabled={blocked} selected={state.selected}
      onSelect={id => void session.select(id)} />}
    {state.status && <div className="space-y-3 border-t pt-3">
      <p role="status" className="font-medium">{arcPlanLabels[state.status.status]} · 已完成 {state.status.accepted_chapters} / {state.status.target_chapters} 章规划</p>
      {state.status.error_message && <p className="text-amber-800">{state.status.error_message}</p>}
      {state.status.status === 'completed' && <p>全部章草稿已保存。请先审读剧情安排，再开始正文创作。</p>}
      <div className="flex flex-wrap gap-2"><button type="button" className={button} disabled={busy} onClick={() => void session.reload(true)}>刷新并查看完整规划</button>
        {['pending', 'running', 'needs_review'].includes(state.status.status) && <button type="button" className={button}
          disabled={blocked || (state.detail?.revision === state.status.revision && !state.detail.can_resume)} onClick={() => void session.command('resume')}>恢复原任务</button>}
        {['pending', 'running', 'needs_review'].includes(state.status.status) && <button type="button" className={button}
          disabled={blocked} onClick={() => void session.command('cancel')}>停止本次规划，保留内容</button>}
        {state.detail && <button type="button" className={button} onClick={() => downloadArcRecord(JSON.stringify(state.detail, null, 2), '完整弧规划记录.json')}>下载完整记录</button>}
        {state.detail?.integrity_valid && state.detail.status === 'completed' && state.detail.result && onCreated &&
          <button type="button" className={button} disabled={opening} onClick={() => {
            const result = state.detail!.result!
            setOpening(true); void perform(async () => { try { await onCreated(result) } finally { setOpening(false) } })
          }}>更新目录并查看这个分卷</button>}
      </div>
      {state.detail && <PlanReview detail={state.detail} />}
    </div>}
    {state.loading && <p role="status">正在读取原任务…</p>}
  </section>
}

function PlanningHistory({ projectId, disabled, selected, onSelect }: { projectId: string; disabled: boolean; selected: string | null; onSelect: (id: string) => void }) {
  const [items, setItems] = useState<ArcPlanStatus[]>([]), [cursor, setCursor] = useState<string | null>(null)
  const [loading, setLoading] = useState(false), [error, setError] = useState<string | null>(null)
  const controller = useRef<AbortController | null>(null), version = useRef(0)
  const load = async (after?: string) => {
    if (controller.current) return
    const current = ++version.current, request = new AbortController()
    controller.current = request
    const timer = setTimeout(() => request.abort(), 20_000)
    setLoading(true)
    try {
      const page = parseArcPlanPage(await apiFetch(arcPlanPath(projectId) + (after ? `?cursor=${encodeURIComponent(after)}` : ''), { signal: request.signal }), projectId)
      if (current !== version.current) return
      setItems(previous => after ? [...previous, ...page.items.filter(row => !previous.some(old => old.request_id === row.request_id))] : page.items)
      setCursor(page.next_cursor); setError(null)
    } catch (cause) { if (current === version.current) setError(cause instanceof Error ? cause.message : '规划历史读取失败。') }
    finally { clearTimeout(timer); if (current === version.current) { controller.current = null; setLoading(false) } }
  }
  useEffect(() => { void load(); return () => { version.current++; controller.current?.abort(); controller.current = null } }, [projectId]) // eslint-disable-line react-hooks/exhaustive-deps
  return <div className="space-y-2 rounded bg-gray-50 p-3">
    {items.map(row => <button key={row.request_id} type="button" className={`${button} block w-full text-left`}
      disabled={disabled} aria-pressed={selected === row.request_id} onClick={() => onSelect(row.request_id)}>
      {row.result?.arc.title || `第 ${row.target_volume_idx} 卷 · ${row.target_chapters} 章`} · {arcPlanLabels[row.status]}
      <span className="mt-1 block text-xs text-gray-500">{row.created_at}</span></button>)}
    {loading && <p role="status">正在读取规划历史…</p>}
    {error && <p role="alert">{error}</p>}
    {!loading && !items.length && !error && <p>本书还没有已受理的弧规划请求。</p>}
    <div className="flex gap-2"><button type="button" disabled={loading} className={button} onClick={() => void load()}>刷新规划历史</button>
      {cursor && <button type="button" disabled={loading} className={button} onClick={() => void load(cursor)}>更多规划记录</button>}</div>
  </div>
}
