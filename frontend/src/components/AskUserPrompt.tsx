'use client'

import { useState, useEffect, useCallback } from 'react'
import { apiFetch } from '@/lib/api'

interface Pause {
  id: string
  question: string
  chapter_id: string | null
  timeout_at: string
}

export function AskUserPrompt({ projectId }: { projectId: string }) {
  const [pending, setPending] = useState<Pause[]>([])
  const [answer, setAnswer] = useState('')
  const [active, setActive] = useState<Pause | null>(null)

  const refresh = useCallback(async () => {
    try {
      const r = await apiFetch<{ pending: Pause[] }>(
        `/api/ask-user/pending?project_id=${projectId}`
      )
      setPending(r.pending)
      if (r.pending.length > 0) {
        setActive(prev => prev || r.pending[0])
      } else {
        setActive(null)
      }
    } catch { /* silent */ }
  }, [projectId])

  useEffect(() => {
    refresh()
    const iv = setInterval(refresh, 3000)
    return () => clearInterval(iv)
  }, [refresh])

  if (!active) return null

  const submit = async () => {
    if (!answer.trim()) return
    await apiFetch(`/api/ask-user/${active.id}/answer`, {
      method: 'POST',
      body: JSON.stringify({ answer }),
    })
    setAnswer('')
    setActive(null)
    refresh()
  }

  const cancel = async () => {
    await apiFetch(`/api/ask-user/${active.id}/cancel`, { method: 'POST' })
    setActive(null)
    refresh()
  }

  return (
    <div className="fixed inset-0 bg-black/40 z-50 flex items-center justify-center">
      <div className="bg-white rounded-lg p-6 max-w-lg w-full mx-4 shadow-xl">
        <div className="flex items-center gap-2 mb-3">
          <span className="text-lg">💬</span>
          <h3 className="font-semibold">AI 需要你的决定</h3>
        </div>
        <p className="text-sm text-gray-700 mb-4 whitespace-pre-wrap">{active.question}</p>
        <textarea
          value={answer}
          onChange={e => setAnswer(e.target.value)}
          onKeyDown={e => {
            if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) submit()
          }}
          className="w-full h-24 p-2 border rounded text-sm mb-3"
          placeholder="回答... (⌘/Ctrl + Enter 提交)"
          autoFocus
        />
        <div className="flex gap-2">
          <button
            onClick={submit}
            disabled={!answer.trim()}
            className="flex-1 px-4 py-2 bg-blue-600 text-white rounded disabled:opacity-50"
          >
            提交
          </button>
          <button onClick={cancel}
            className="px-4 py-2 bg-gray-200 text-gray-700 rounded">
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
