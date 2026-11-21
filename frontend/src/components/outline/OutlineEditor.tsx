/**
 * PR-OUTLINE-CENTER-EDIT (2026-05-04)
 *
 * Click on a 全书大纲 / 分卷大纲 / 章节大纲 entry in the left tree → render this
 * editor in the centre area, parity with chapter content (large textarea +
 * 3 s debounced auto-save + Cmd/Ctrl-S).
 *
 * Save endpoints:
 *   - book   → PUT /api/projects/{pid}/outlines/{outlineId}      (content_json.raw_text)
 *   - volume → PUT /api/projects/{pid}/outlines/{outlineId}      (content_json.raw_text)
 *   - chapter → PUT /api/projects/{pid}/chapters/{chapterId}     (outline_json.raw_text)
 *
 * Existing structured fields on the JSON (volume_idx, chapter_summaries, …)
 * are preserved by merging into raw_text replacement. The server only syncs
 * changed chapter summaries to untouched draft stubs, preserving detailed plans.
 */
'use client'

import React, { useCallback, useEffect, useRef, useState } from 'react'
import { apiFetch } from '@/lib/api'

export type OutlineEditTarget =
  | { type: 'book'; outlineId: string; initialJson: Record<string, unknown> | null; title: string }
  | { type: 'volume'; outlineId: string; volumeId: string; initialJson: Record<string, unknown> | null; title: string; volumeIdx?: number }
  | { type: 'chapter'; chapterId: string; initialJson: Record<string, unknown> | null; title: string }

function targetKey(t: OutlineEditTarget): string {
  if (t.type === 'chapter') return `chapter:${t.chapterId}`
  return `${t.type}:${t.outlineId}`
}

function extractText(json: Record<string, unknown> | null | undefined): string {
  if (!json) return ''
  if (typeof json === 'string') return json as unknown as string
  const rt = (json as Record<string, unknown>)['raw_text']
  if (typeof rt === 'string') return rt
  // Fallback: pretty JSON so user can still see/edit
  try {
    return JSON.stringify(json, (key, value) =>
      key === '_chapter_id' || key === '_order_unavailable' ? undefined : value, 2)
  } catch { return '' }
}

function hasUnavailablePlan(value: unknown): boolean {
  if (!value || typeof value !== 'object') return false
  if (Array.isArray(value)) return value.some(hasUnavailablePlan)
  const record = value as Record<string, unknown>
  if (record._author_text_revision) return false
  return record._order_unavailable === true
    || ('_chapter_id' in record && !record._chapter_id)
    || Object.values(record).some(hasUnavailablePlan)
}

interface Props {
  projectId: string
  target: OutlineEditTarget
  onClose: () => void
  /** Notifies parent after save so it can refresh in-memory caches. */
  onSaved?: (target: OutlineEditTarget, updatedJson: Record<string, unknown>) => void
}

interface Draft { text: string; baseline: Record<string, unknown> }
function readDraft(key: string, initial: Record<string, unknown> | null): Draft {
  const fallback = { text: extractText(initial), baseline: initial || {} }
  try {
    const draft = JSON.parse(sessionStorage.getItem(key) || 'null')
    if (draft && typeof draft.text === 'string' && draft.baseline && typeof draft.baseline === 'object'
      && !Array.isArray(draft.baseline) && draft.text !== fallback.text) return draft
  } catch { /* Storage may be disabled; normal editing still works. */ }
  return fallback
}
function storeDraft(key: string, draft: Draft) {
  try {
    if (draft.text === extractText(draft.baseline)) sessionStorage.removeItem(key)
    else sessionStorage.setItem(key, JSON.stringify(draft))
  } catch { /* Best effort local recovery; server save errors remain visible. */ }
}
function editedJson(baseline: Record<string, unknown>, text: string): Record<string, unknown> {
  if (typeof baseline.raw_text === 'string' || Object.keys(baseline).length === 0) {
    return { ...baseline, raw_text: text }
  }
  const parsed: unknown = JSON.parse(text)
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('细纲必须是 JSON 对象')
  return parsed as Record<string, unknown>
}

export function OutlineEditor(props: Props) {
  // Each target owns its requests and timers; late responses cannot mutate a
  // different target. Unsaved drafts survive target changes in this tab.
  const key = `${props.projectId}:${targetKey(props.target)}`
  return <OutlineEditorSession key={key} {...props} draftKey={`aiwrite:outline-draft:${key}`} />
}

function OutlineEditorSession({ projectId, target, onClose, onSaved, draftKey }: Props & { draftKey: string }) {
  const [initial] = useState(() => readDraft(draftKey, target.initialJson))
  const [content, setContent] = useState(initial.text)
  const [savedContent, setSavedContent] = useState(extractText(initial.baseline))
  const [savingState, setSavingState] = useState<'idle' | 'saving' | 'saved' | 'error'>('idle')
  const [expanding, setExpanding] = useState(false)
  const [reloading, setReloading] = useState(false)
  const [expandError, setExpandError] = useState('')
  const [saveNotice, setSaveNotice] = useState('')
  const baselineRef = useRef(initial.baseline)
  const textRef = useRef(initial.text)
  const busyRef = useRef(false)
  const activeRef = useRef(true)
  const saveTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const saveRef = useRef<() => Promise<boolean>>(async () => false)
  const clearTimer = () => {
    if (saveTimerRef.current) clearTimeout(saveTimerRef.current)
    saveTimerRef.current = null
  }
  const baseUrl = target.type === 'chapter'
    ? `/api/projects/${projectId}/chapters/${target.chapterId}`
    : `/api/projects/${projectId}/outlines/${target.outlineId}`

  const doSave = useCallback(async (): Promise<boolean> => {
    clearTimer()
    if (busyRef.current || !activeRef.current) return false
    const text = textRef.current
    const baseline = baselineRef.current
    if (text === extractText(baseline)) return true
    busyRef.current = true
    setSavingState('saving')
    setSaveNotice('')
    let succeeded = false
    try {
      const merged = editedJson(baseline, text)
      const body = target.type === 'chapter'
        ? { outline_json: merged, expected_outline_json: baseline }
        : { content_json: merged, expected_content_json: baseline }
      const result = await apiFetch<{ content_json?: Record<string, unknown>; outline_json?: Record<string, unknown>;
        synced_chapters?: number; preserved_chapters?: number; unmaterialized_chapters?: number }>(baseUrl,
        { method: 'PUT', body: JSON.stringify(body) })
      if (!activeRef.current) return false
      const saved = target.type === 'chapter' ? result.outline_json : result.content_json
      if (!saved || typeof saved !== 'object' || Array.isArray(saved)) throw new Error('保存未返回有效大纲')
      baselineRef.current = saved
      setSavedContent(extractText(saved))
      // A response only replaces the visible text if the author has not typed
      // again while it was in flight. Otherwise save the newer draft next.
      if (textRef.current === text) {
        textRef.current = extractText(saved)
        setContent(textRef.current)
      }
      storeDraft(draftKey, { text: textRef.current, baseline: saved })
      if (target.type === 'volume') {
        const notes = ['卷纲已保存，已有正文和独立编辑的细纲已保留。']
        if (result.synced_chapters) notes.push(`已同步 ${result.synced_chapters} 章未编辑的摘要。`)
        if (result.preserved_chapters) notes.push(`${result.preserved_chapters} 章已有内容，请按新卷纲核对细纲。`)
        if (result.unmaterialized_chapters) notes.push(`${result.unmaterialized_chapters} 个章号尚无对应章节，请核对章节列表。`)
        setSaveNotice(notes.join(''))
      }
      setSavingState('saved')
      onSaved?.(target, saved)
      succeeded = true
      return true
    } catch {
      clearTimer()
      if (activeRef.current) {
        setSavingState('error')
        setSaveNotice('保存失败，当前编辑已保留。请检查格式；若服务端已修改，请先复制当前文字，再重新加载核对。')
      }
      return false
    } finally {
      busyRef.current = false
      if (succeeded && activeRef.current && textRef.current !== extractText(baselineRef.current)) {
        clearTimer()
        saveTimerRef.current = setTimeout(() => { void saveRef.current() }, 3000)
      }
    }
  }, [baseUrl, draftKey, target, onSaved])
  saveRef.current = doSave

  const onChange = (value: string) => {
    textRef.current = value
    setContent(value)
    storeDraft(draftKey, { text: value, baseline: baselineRef.current })
    clearTimer()
    if (value !== extractText(baselineRef.current)) {
      saveTimerRef.current = setTimeout(() => { void saveRef.current() }, 3000)
    }
  }

  const expandOutline = async () => {
    if (target.type !== 'chapter' || busyRef.current || textRef.current !== extractText(baselineRef.current)) return
    clearTimer()
    busyRef.current = true
    setExpanding(true)
    setExpandError('')
    try {
      const data = await apiFetch<{ outline_json: Record<string, unknown> }>(`${baseUrl}/outline/expand`, { method: 'POST' })
      if (!activeRef.current) return
      if (!data.outline_json || typeof data.outline_json !== 'object' || Array.isArray(data.outline_json)) throw new Error('扩写没有返回有效细纲')
      baselineRef.current = data.outline_json
      textRef.current = extractText(data.outline_json)
      setContent(textRef.current)
      setSavedContent(textRef.current)
      setSavingState('idle')
      storeDraft(draftKey, { text: textRef.current, baseline: data.outline_json })
      onSaved?.(target, data.outline_json)
    } catch {
      if (activeRef.current) setExpandError('扩写未完成，已有细纲已保留。请刷新核对后重试。')
    } finally {
      busyRef.current = false
      if (activeRef.current) setExpanding(false)
    }
  }

  const reload = async () => {
    if (busyRef.current) return
    if (textRef.current !== extractText(baselineRef.current)
      && !window.confirm('重新加载会放弃当前本地修改。请先复制需要保留的文字，是否继续？')) return
    clearTimer()
    busyRef.current = true
    setReloading(true)
    setSavingState('saving')
    try {
      const data = await apiFetch<{ content_json?: Record<string, unknown>; outline_json?: Record<string, unknown> }>(baseUrl)
      if (!activeRef.current) return
      const saved = target.type === 'chapter' ? data.outline_json : data.content_json
      if (!saved || typeof saved !== 'object' || Array.isArray(saved)) throw new Error('invalid outline')
      baselineRef.current = saved
      textRef.current = extractText(saved)
      setContent(textRef.current)
      setSavedContent(textRef.current)
      setSavingState('idle')
      setSaveNotice('已加载服务端最新版本。')
      storeDraft(draftKey, { text: textRef.current, baseline: saved })
      onSaved?.(target, saved)
    } catch {
      if (activeRef.current) { setSavingState('error'); setSaveNotice('重新加载失败，当前文字已保留。') }
    } finally { busyRef.current = false; if (activeRef.current) setReloading(false) }
  }

  useEffect(() => {
    activeRef.current = true
    const handler = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 's') {
        event.preventDefault()
        void saveRef.current()
      }
    }
    const beforeUnload = (event: BeforeUnloadEvent) => {
      if (textRef.current !== extractText(baselineRef.current)) {
        event.preventDefault()
        event.returnValue = ''
      }
    }
    window.addEventListener('keydown', handler)
    window.addEventListener('beforeunload', beforeUnload)
    return () => {
      activeRef.current = false
      clearTimer()
      window.removeEventListener('keydown', handler)
      window.removeEventListener('beforeunload', beforeUnload)
    }
  }, [])

  const dirty = content !== savedContent
  const stateText = savingState === 'saving' ? '保存中…'
    : savingState === 'error' ? '保存失败（请核对后重试）'
    : dirty ? '未保存' : savingState === 'saved' ? '已保存' : '已同步'
  const stateColor = savingState === 'error' ? 'text-red-600' : dirty ? 'text-amber-600' : 'text-gray-500'

  const levelLabel =
    target.type === 'book' ? '全书大纲'
    : target.type === 'volume' ? '分卷大纲'
    : '章节大纲'

  return (
    <div className="flex-1 overflow-y-auto">
      <div className="max-w-3xl mx-auto pt-4 px-6">
        <div className="flex items-center justify-between mb-2">
          <div className="min-w-0">
            <div className="text-xs text-gray-400 mb-0.5">{levelLabel}</div>
            <h3 className="text-lg font-semibold text-gray-800 truncate">{target.title}</h3>
          </div>
          <div className="flex items-center gap-3 shrink-0">
            <span className={`text-xs ${stateColor}`}>{stateText}</span>
            {target.type === 'chapter' && (
              <button
                type="button"
                onClick={() => { void expandOutline() }}
                disabled={dirty || expanding || savingState === 'saving'}
                title="请先保存当前修改，再按上游大纲扩写本章"
                className="px-3 py-1 text-xs border border-blue-300 text-blue-700 rounded-lg hover:bg-blue-50 disabled:opacity-50 disabled:cursor-not-allowed"
              >{expanding ? 'AI 扩写中…' : 'AI 扩写本章大纲'}</button>
            )}
            <button
              type="button"
              onClick={() => { if (dirty) void doSave() }}
              disabled={!dirty || expanding || savingState === 'saving'}
              className="px-3 py-1 text-xs border border-gray-300 text-gray-700 rounded-lg hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed"
            >立即保存</button>
            <button type="button" onClick={() => { void reload() }} disabled={expanding || savingState === 'saving'}
              className="px-3 py-1 text-xs text-gray-500">重新加载</button>
            <button
              type="button"
              disabled={expanding || savingState === 'saving'}
              onClick={async () => {
                if (busyRef.current) return
                if (textRef.current !== extractText(baselineRef.current)) {
                  const saved = await saveRef.current()
                  if (!saved || !activeRef.current || textRef.current !== extractText(baselineRef.current)) return
                }
                onClose()
              }}
              className="px-3 py-1 text-xs text-gray-500 hover:text-gray-900"
            >关闭</button>
          </div>
        </div>
      </div>
      <div className="max-w-3xl mx-auto py-4 px-6">
        {hasUnavailablePlan(baselineRef.current) && (
          <p role="note" className="mb-3 text-sm text-amber-700">
            部分旧计划尚未对应到当前章节，原文已保留，暂不用于生成。请核对这些条目的章号和剧情安排。
          </p>
        )}
        {saveNotice && <p role="status" className="mb-3 text-sm text-amber-700">{saveNotice}</p>}
        {expandError && (
          <div className="mb-3 p-3 text-sm text-red-700 bg-red-50 border border-red-200 rounded-lg">
            扩写失败：{expandError}
          </div>
        )}
        <textarea
          value={content}
          disabled={expanding || reloading}
          onChange={(e) => onChange(e.target.value)}
          placeholder="此大纲为空。开始编辑以填充内容…（每 3 秒自动保存，Cmd/Ctrl+S 立即保存）"
          className="w-full min-h-[500px] p-4 text-base leading-relaxed border border-gray-200 rounded-lg outline-none resize-y focus:border-blue-300 focus:ring-1 focus:ring-blue-200"
        />
      </div>
    </div>
  )
}

export default OutlineEditor
