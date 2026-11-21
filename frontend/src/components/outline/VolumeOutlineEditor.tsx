'use client'

import { useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react'
import { VolumeOutlineHistory } from './VolumeOutlineHistory'
import { loadVolumeCandidate, volumeOutlineEditText,
  type VolumeCandidate, type VolumeOutlineSource } from '@/lib/volumeOutlines'
import { getVolumeOutlineSession } from './volumeOutlineSession'

type Props = { projectId: string; volumeId?: string; candidateTaskId?: string; onClose: () => void; onSaved?: () => void }
type Preview = { source: VolumeOutlineSource; candidate: VolumeCandidate }

function download(text: string, name: string) {
  const url = URL.createObjectURL(new Blob([text], { type: 'text/plain;charset=utf-8' }))
  const link = document.createElement('a')
  link.href = url; link.download = name
  document.body.appendChild(link); link.click(); link.remove(); URL.revokeObjectURL(url)
}

export function VolumeOutlineEditor(props: Props) {
  return <VolumeOutlineEntry key={`${props.projectId}:${props.candidateTaskId ?? props.volumeId}`} {...props} />
}

function VolumeOutlineEntry({ projectId, volumeId, candidateTaskId, onClose, onSaved }: Props) {
  const [preview, setPreview] = useState<Preview | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [revision, setRevision] = useState(0)
  useEffect(() => {
    if (!candidateTaskId) return
    let active = true
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), 20_000)
    setError(null)
    setPreview(null)
    void loadVolumeCandidate(projectId, candidateTaskId, controller.signal)
      .then((value) => { if (active && !controller.signal.aborted) setPreview(value) })
      .catch((cause) => { if (!active) return
        if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : '候选读取失败。')
        else setError('候选读取尚未完成，请保留任务文本并重新核对。') })
      .finally(() => clearTimeout(timer))
    return () => { active = false; controller.abort(); clearTimeout(timer) }
  }, [projectId, candidateTaskId, revision])
  const targetId = candidateTaskId ? preview?.source.volume_id : volumeId
  if (!targetId) return <section aria-label="卷纲核对与编辑" className="space-y-3 rounded-lg border bg-white p-4">
    <h2 className="font-semibold">核对卷纲候选</h2>
    {error ? <p role="alert">{error}</p> : <p role="status">正在读取候选及其所属分卷…</p>}
    <div className="flex flex-wrap gap-3">
      {error && <button type="button" onClick={() => setRevision((value) => value + 1)} className="min-h-10 text-blue-800">重新核对候选</button>}
      <button type="button" onClick={onClose} className="min-h-10">返回工作区</button>
    </div>
  </section>
  return <VolumeOutlineDraft key={`${projectId}:${targetId}`} projectId={projectId} volumeId={targetId}
    preview={preview} onClose={onClose} onSaved={onSaved} />
}

function VolumeOutlineDraft({ projectId, volumeId, preview, onClose, onSaved }: {
  projectId: string; volumeId: string; preview: Preview | null; onClose: () => void; onSaved?: () => void
}) {
  const session = useMemo(() => getVolumeOutlineSession(projectId, volumeId), [projectId, volumeId])
  const state = useSyncExternalStore(session.subscribe, session.getSnapshot, session.getSnapshot)
  const notified = useRef<string | null>(null)
  const [showHistory, setShowHistory] = useState(false)
  useEffect(() => { session.attach(preview?.source) }, [session, preview?.source])
  useEffect(() => {
    if (state.receipt && state.receipt.request_id !== notified.current) {
      notified.current = state.receipt.request_id
      onSaved?.()
    }
  }, [state.receipt, onSaved])
  const source = state.source
  const dirty = Boolean(source && state.text !== volumeOutlineEditText(source, state.format))
  const saving = state.busy
  const saveDisabled = !source || !state.text.trim() || saving || Boolean(state.pending || state.remote || state.storageError)
    || (!dirty && !state.candidate && source?.outline?.is_confirmed === 1)
  const run = (operation: () => Promise<unknown>) => { void operation().catch(() => {}) }
  return <section aria-label="卷纲核对与编辑" className="mx-auto w-full max-w-4xl space-y-4 rounded-lg border border-gray-200 bg-white p-4 sm:p-6">
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div><p className="text-xs text-gray-500">分卷大纲</p>
        <h2 className="text-lg font-semibold">{source ? `第 ${source.volume_idx} 卷 · ${source.title}` : '正在读取当前卷纲'}</h2>
      </div>
      <button type="button" onClick={onClose} className="min-h-10 text-sm text-blue-800">返回工作区</button>
    </div>
    <p className="text-sm text-gray-600">候选先放入编辑稿。核对剧情安排后，点击保存才会采用为本卷大纲；已有正文和独立编辑的章节细纲会保留。</p>
    {state.loading && <p role="status" className="text-sm">正在核对当前保存稿…</p>}
    {state.error && <p role="alert" className="text-sm text-red-700">{state.error}</p>}
    {state.storageError && <div className="space-y-1 rounded bg-amber-50 p-3 text-sm">
      <p role="alert">{state.storageError}</p>
      <button type="button" disabled={saving} onClick={session.recoverStorage} className="min-h-10 text-blue-800">重新读取卷纲暂存</button>
    </div>}
    {state.pending && <div className="space-y-2 rounded border border-amber-200 bg-amber-50 p-3 text-sm">
      <p>这份保存请求尚待核对。重试会沿用原卷纲、原编辑稿和原请求编号，后续输入仍会保留。</p>
      <div className="flex flex-wrap gap-3">
        <button type="button" disabled={saving} onClick={() => run(session.retry)} className="min-h-10 text-blue-800 disabled:opacity-50">
          {saving ? '正在保存…' : '核对并重试原保存'}
        </button>
        {state.pending.rejected && <button type="button" disabled={saving} onClick={session.dismissRejected} className="min-h-10">
          保留编辑稿，清除已拒绝请求
        </button>}
      </div>
    </div>}
    {source && <details>
      <summary className="cursor-pointer text-sm text-gray-700">查看当前保存稿</summary>
      <textarea aria-label="当前卷纲保存稿" readOnly value={volumeOutlineEditText(source, state.format)}
        className="mt-2 min-h-48 w-full resize-y rounded border bg-gray-50 p-3 text-sm leading-relaxed" />
    </details>}
    {preview && <div className="space-y-2 rounded border border-blue-200 bg-blue-50 p-3">
      <details open>
        <summary className="cursor-pointer text-sm font-semibold text-blue-900">本次卷纲候选全文</summary>
        <textarea aria-label="待采用卷纲候选" value={preview.candidate.text} readOnly
          className="mt-2 min-h-48 w-full resize-y rounded border bg-white p-3 text-sm leading-relaxed" />
      </details>
      <button type="button" disabled={!source || saving || Boolean(state.pending)} onClick={() => session.useCandidate(preview.candidate)}
        className="min-h-10 text-sm text-blue-800 disabled:opacity-50">将这份候选放入编辑稿</button>
    </div>}
    {state.remote && <div className="space-y-2 rounded border border-amber-300 bg-amber-50 p-3 text-sm">
      <p>保存稿或分卷已在其他位置变化。请对照下面的最新保存稿；当前编辑内容会保留。</p>
      <textarea aria-label="最新卷纲保存稿对照" value={volumeOutlineEditText(state.remote, state.format)} readOnly
        className="min-h-48 w-full resize-y rounded border bg-white p-3 leading-relaxed" />
      <button type="button" disabled={saving || Boolean(state.pending && !state.pending.rejected)} onClick={session.useRemoteSource}
        className="min-h-10 text-blue-800 disabled:opacity-50">已核对，保留编辑稿继续修改</button>
    </div>}
    <div className="flex flex-wrap items-center gap-3 text-sm">
      <label>编辑方式 <select aria-label="卷纲编辑方式" value={state.format}
        disabled={!source || saving || dirty || Boolean(state.pending || state.candidate)}
        onChange={(event) => session.changeFormat(event.target.value as 'text' | 'structure')} className="min-h-10 rounded border px-2">
        <option value="text">文字卷纲</option><option value="structure">结构化章节安排</option>
      </select></label>
      {(dirty || state.candidate) && <span className="text-xs text-gray-500">保存当前改稿后可切换编辑方式。</span>}
    </div>
    {state.format === 'structure' && <p className="text-sm text-amber-800">
      保存完整结构会同步仍未编辑的章节摘要。请同时核对文字改稿与结构安排；已有作者文字修订时，后续生成仍以文字改稿为准。
    </p>}
    <label className="block space-y-2 text-sm font-medium">
      <span>卷纲编辑稿{state.candidate ? '（以所选候选为基础）' : ''}</span>
      <textarea aria-label="卷纲编辑稿" value={state.text} disabled={!source}
        onChange={(event) => session.edit(event.target.value)}
        className="min-h-80 w-full resize-y rounded border border-gray-300 p-3 text-base font-normal leading-relaxed focus:border-blue-500 focus:outline-none" />
    </label>
    {state.previousDraft !== null && <details>
      <summary className="cursor-pointer text-sm text-gray-700">查看替换前的编辑稿</summary>
      <textarea aria-label="替换前卷纲编辑稿" value={state.previousDraft} readOnly className="mt-2 min-h-48 w-full rounded border p-3 text-sm" />
      <button type="button" disabled={saving || Boolean(state.pending)} onClick={session.restorePreviousDraft}
        className="min-h-10 text-sm text-blue-800 disabled:opacity-50">恢复这份编辑稿</button>
    </details>}
    <div className="flex flex-wrap items-center gap-3">
      <button type="button" disabled={saveDisabled} onClick={() => run(session.save)}
        className="min-h-10 rounded bg-blue-700 px-4 text-sm font-medium text-white disabled:opacity-50">保存为本卷大纲</button>
      <button type="button" disabled={state.loading || saving} onClick={() => run(session.reload)} className="min-h-10 text-sm text-blue-800 disabled:opacity-50">重新核对保存稿</button>
      <button type="button" onClick={() => download(state.text, `卷纲编辑稿-${volumeId}.txt`)} className="min-h-10 text-sm text-blue-800">下载完整编辑稿</button>
      <button type="button" onClick={() => setShowHistory((value) => !value)} className="min-h-10 text-sm text-blue-800">查看保存历史</button>
    </div>
    {state.receipt && !state.pending && <p role="status" className="text-sm text-emerald-800">
      本次卷纲保存已确认。本卷 {state.receipt.preserved_chapters} 章正文和独立编辑的细纲已保留。
      {state.receipt.synced_chapters > 0 ? `已同步 ${state.receipt.synced_chapters} 章未编辑的摘要。` : ''}
      {state.receipt.unmaterialized_chapters > 0 ? `${state.receipt.unmaterialized_chapters} 项章节安排尚未找到对应章节，请核对目录。` : ''}
      {dirty ? '当前还有后续修改未保存。' : ''}
    </p>}
    {showHistory && <VolumeOutlineHistory projectId={projectId} volumeId={volumeId} revision={state.receipt?.request_id} />}
  </section>
}
