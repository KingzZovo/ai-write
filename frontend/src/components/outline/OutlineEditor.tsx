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
 * are preserved by merging into raw_text replacement, so cascade syncs in
 * outlines.py PR-OL9 keep working.
 */
'use client'

import React, { useCallback, useEffect, useRef, useState } from 'react'
import { apiFetch } from '@/lib/api'

export type OutlineEditTarget =
  | { type: 'book'; outlineId: string; initialJson: Record<string, unknown> | null; title: string }
  | { type: 'volume'; outlineId: string; initialJson: Record<string, unknown> | null; title: string; volumeIdx?: number }
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
  try { return JSON.stringify(json, null, 2) } catch { return '' }
}

interface Props {
  projectId: string
  target: OutlineEditTarget
  onClose: () => void
  /** Notifies parent after save so it can refresh in-memory caches. */
  onSaved?: (target: OutlineEditTarget, updatedJson: Record<string, unknown>) => void
}

export function OutlineEditor({ projectId, target, onClose, onSaved }: Props) {
  const initialText = extractText(target.initialJson)
  const [content, setContent] = useState<string>(initialText)
  const [savedContent, setSavedContent] = useState<string>(initialText)
  const [savingState, setSavingState] = useState<'idle' | 'saving' | 'saved' | 'error'>('idle')
  const [expanding, setExpanding] = useState<boolean>(false)
  const [expandError, setExpandError] = useState<string>('')
  const [structuredJson, setStructuredJson] = useState<Record<string, unknown> | null>(
    target.initialJson || null,
  )
  const saveTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const key = targetKey(target)

  // Re-initialise content when the target changes (e.g. user clicked a different outline).
  useEffect(() => {
    const t = extractText(target.initialJson)
    setContent(t)
    setSavedContent(t)
    setSavingState('idle')
    setStructuredJson(target.initialJson || null)
    setExpandError('')
    if (saveTimerRef.current) {
      clearTimeout(saveTimerRef.current)
      saveTimerRef.current = null
    }
    // Intentionally only react to the stable target key; initialJson identity
    // changes (e.g. parent refresh) shouldn't blow away the user's in-flight edit.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key])

  const doSave = useCallback(async (text: string) => {
    setSavingState('saving')
    try {
      const merged = { ...(target.initialJson || {}), raw_text: text }
      let url: string
      let body: Record<string, unknown>
      if (target.type === 'chapter') {
        url = `/api/projects/${projectId}/chapters/${target.chapterId}`
        body = { outline_json: merged }
      } else {
        url = `/api/projects/${projectId}/outlines/${target.outlineId}`
        body = { content_json: merged }
      }
      await apiFetch(url, { method: 'PUT', body: JSON.stringify(body) })
      setSavedContent(text)
      setSavingState('saved')
      onSaved?.(target, merged)
      // Auto-fade the "已保存" badge.
      setTimeout(() => {
        setSavingState((s) => (s === 'saved' ? 'idle' : s))
      }, 1800)
    } catch (err) {
      console.error('OutlineEditor save failed:', err)
      setSavingState('error')
    }
  }, [projectId, target, onSaved])

  const onChange = useCallback((value: string) => {
    setContent(value)
    if (saveTimerRef.current) clearTimeout(saveTimerRef.current)
    if (value !== savedContent) {
      saveTimerRef.current = setTimeout(() => { void doSave(value) }, 3000)
    }
  }, [savedContent, doSave])

  // PR-OUTLINE-DEEPDIVE Phase 2: trigger LLM expansion for chapter outline.
  const expandOutline = useCallback(async () => {
    if (target.type !== 'chapter') return
    setExpanding(true)
    setExpandError('')
    try {
      const res = await apiFetch(
        `/api/projects/${projectId}/chapters/${target.chapterId}/outline/expand`,
        { method: 'POST' },
      )
      const data = (res || {}) as { outline_json?: Record<string, unknown> }
      const newJson = data.outline_json || {}
      setStructuredJson(newJson)
      const newText = extractText(newJson)
      setContent(newText)
      setSavedContent(newText)
      onSaved?.(target, newJson)
    } catch (err: unknown) {
      console.error('OutlineEditor expand failed:', err)
      const msg = err instanceof Error ? err.message : String(err)
      setExpandError(msg || '扩写失败')
    } finally {
      setExpanding(false)
    }
  }, [projectId, target, onSaved])

  // Cleanup pending save timer on unmount.
  useEffect(() => () => {
    if (saveTimerRef.current) clearTimeout(saveTimerRef.current)
  }, [])

  // Cmd/Ctrl-S to force-save.
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 's') {
        e.preventDefault()
        if (content !== savedContent) void doSave(content)
      }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [content, savedContent, doSave])

  const dirty = content !== savedContent
  const stateText =
    savingState === 'saving' ? '保存中…'
    : savingState === 'saved' ? '已保存'
    : savingState === 'error' ? '保存失败（请重试）'
    : dirty ? '编辑中…'
    : '已同步'
  const stateColor =
    savingState === 'error' ? 'text-red-600'
    : savingState === 'saved' ? 'text-green-600'
    : dirty ? 'text-amber-600'
    : 'text-gray-400'

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
                disabled={expanding || savingState === 'saving'}
                title="调 LLM 为本章生成含伏笔 / 状态变化 / 下章钩子的详细大纲"
                className="px-3 py-1 text-xs border border-blue-300 text-blue-700 rounded-lg hover:bg-blue-50 disabled:opacity-50 disabled:cursor-not-allowed"
              >{expanding ? 'AI 扩写中…' : 'AI 扩写本章大纲'}</button>
            )}
            <button
              type="button"
              onClick={() => { if (dirty) void doSave(content) }}
              disabled={!dirty || savingState === 'saving'}
              className="px-3 py-1 text-xs border border-gray-300 text-gray-700 rounded-lg hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed"
            >立即保存</button>
            <button
              type="button"
              onClick={onClose}
              className="px-3 py-1 text-xs text-gray-500 hover:text-gray-900"
            >关闭</button>
          </div>
        </div>
      </div>
      <div className="max-w-3xl mx-auto py-4 px-6">
        {expandError && (
          <div className="mb-3 p-3 text-sm text-red-700 bg-red-50 border border-red-200 rounded-lg">
            扩写失败：{expandError}
          </div>
        )}
        <textarea
          value={content}
          onChange={(e) => onChange(e.target.value)}
          placeholder="此大纲为空。开始编辑以填充内容…（每 3 秒自动保存，Cmd/Ctrl+S 立即保存）"
          className="w-full min-h-[500px] p-4 text-base leading-relaxed border border-gray-200 rounded-lg outline-none resize-y focus:border-blue-300 focus:ring-1 focus:ring-blue-200"
        />
      </div>
    </div>
  )
}

export default OutlineEditor
