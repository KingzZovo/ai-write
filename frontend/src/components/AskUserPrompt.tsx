'use client'

import { useCallback, useEffect, useId, useRef, useState } from 'react'
import { apiFetch } from '@/lib/api'

interface Pause {
  id: string
  question: string
  chapter_id: string | null
  timeout_at: string
}

const POLL_ACTIVE_MS = 3000
const POLL_IDLE_MS = 10000

export function AskUserPrompt({ projectId }: { projectId: string }) {
  const [pending, setPending] = useState<Pause[]>([])
  const [answer, setAnswer] = useState('')
  const [active, setActive] = useState<Pause | null>(null)
  // IDs the user has just answered / cancelled. We skip them client-side until
  // the backend's /pending no longer returns them, avoiding a flicker where the
  // just-dismissed dialog re-appears between POST and the next poll.
  const dismissedRef = useRef<Set<string>>(new Set())
  const titleId = useId()

  const refresh = useCallback(async () => {
    if (typeof document !== 'undefined' && document.hidden) return
    try {
      const r = await apiFetch<{ pending: Pause[] }>(
        `/api/ask-user/pending?project_id=${projectId}`
      )
      // Drop dismissed IDs the server no longer reports (status propagated)
      const serverIds = new Set(r.pending.map(p => p.id))
      for (const id of Array.from(dismissedRef.current)) {
        if (!serverIds.has(id)) dismissedRef.current.delete(id)
      }
      const visible = r.pending.filter(p => !dismissedRef.current.has(p.id))
      setPending(visible)
      setActive(prev => {
        if (visible.length === 0) return null
        // Keep current active if still pending; else advance to head
        if (prev && visible.some(p => p.id === prev.id)) return prev
        return visible[0]
      })
    } catch {
      /* silent — polling will retry */
    }
  }, [projectId])

  // Poll faster while a pause is showing, slower when idle.
  useEffect(() => {
    refresh()
    const intervalMs = active ? POLL_ACTIVE_MS : POLL_IDLE_MS
    const iv = setInterval(refresh, intervalMs)
    return () => clearInterval(iv)
  }, [refresh, active])

  const submit = useCallback(async () => {
    if (!active) return
    const text = answer.trim()
    if (!text) return
    const id = active.id
    dismissedRef.current.add(id)
    try {
      await apiFetch(`/api/ask-user/${id}/answer`, {
        method: 'POST',
        body: JSON.stringify({ answer: text }),
      })
    } catch {
      dismissedRef.current.delete(id) // allow retry on failure
      return
    }
    setAnswer('')
    setActive(null)
    refresh()
  }, [active, answer, refresh])

  const cancel = useCallback(async () => {
    if (!active) return
    const id = active.id
    dismissedRef.current.add(id)
    try {
      await apiFetch(`/api/ask-user/${id}/cancel`, { method: 'POST' })
    } catch {
      dismissedRef.current.delete(id)
      return
    }
    setActive(null)
    refresh()
  }, [active, refresh])

  // Esc cancels the pause (standard modal UX).
  useEffect(() => {
    if (!active) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault()
        cancel()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [active, cancel])

  if (!active) return null

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby={titleId}
      className="fixed inset-0 bg-black/40 z-50 flex items-center justify-center"
    >
      <div className="bg-white rounded-lg p-6 max-w-lg w-full mx-4 shadow-xl">
        <div className="flex items-center gap-2 mb-3">
          <span className="text-lg" aria-hidden="true">💬</span>
          <h3 id={titleId} className="font-semibold">AI 需要你的决定</h3>
        </div>
        <p className="text-sm text-gray-700 mb-4 whitespace-pre-wrap">{active.question}</p>
        <textarea
          value={answer}
          onChange={e => setAnswer(e.target.value)}
          onKeyDown={e => {
            if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) submit()
          }}
          className="w-full h-24 p-2 border rounded text-sm mb-3"
          placeholder="回答... (⌘/Ctrl + Enter 提交，Esc 取消)"
          aria-label="你的回答"
          autoFocus
        />
        <div className="flex gap-2">
          <button
            type="button"
            onClick={submit}
            disabled={!answer.trim()}
            className="flex-1 px-4 py-2 bg-blue-600 text-white rounded disabled:opacity-50"
          >
            提交
          </button>
          <button
            type="button"
            onClick={cancel}
            className="px-4 py-2 bg-gray-200 text-gray-700 rounded"
          >
            取消
          </button>
        </div>
        {pending.length > 1 && (
          <p className="text-xs text-gray-400 mt-2">
            还有 {pending.length - 1} 个问题待回答
          </p>
        )}
      </div>
    </div>
  )
}
