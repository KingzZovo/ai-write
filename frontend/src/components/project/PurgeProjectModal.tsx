'use client'

import { useState } from 'react'
import { apiFetch } from '@/lib/api'

export function PurgeProjectModal({
  projectId,
  projectTitle,
  onClose,
  onDone,
}: {
  projectId: string
  projectTitle: string
  onClose: () => void
  onDone: () => void
}) {
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const matches = input.trim() === projectTitle.trim()
  const go = async () => {
    if (!matches || busy) return
    setBusy(true)
    try {
      await apiFetch(`/api/projects/${projectId}?purge=true`, { method: 'DELETE' })
      onDone()
    } finally { setBusy(false) }
  }
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div className="bg-white rounded-2xl shadow-xl w-full max-w-md mx-4 p-6">
        <h3 className="text-lg font-bold text-red-600 mb-2">⚠ 永久删除</h3>
        <p className="text-sm text-gray-700">
          项目「{projectTitle}」将被<strong>永久删除</strong>，所有卷、章节、大纲、版本都不可恢复。
        </p>
        <p className="text-sm text-gray-700 mt-3">
          为确认，请输入书名：<span className="font-semibold">{projectTitle}</span>
        </p>
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          autoFocus
          className="w-full px-3 py-2 mt-2 text-sm border border-gray-300 rounded-lg focus:ring-2 focus:ring-red-500"
        />
        <div className="flex gap-3 mt-6">
          <button onClick={onClose} className="flex-1 px-4 py-2 text-sm border border-gray-300 rounded-lg hover:bg-gray-50">
            取消
          </button>
          <button
            onClick={go}
            disabled={!matches || busy}
            className="flex-1 px-4 py-2 text-sm bg-red-600 text-white rounded-lg disabled:opacity-40"
          >
            {busy ? '删除中...' : '永久删除'}
          </button>
        </div>
      </div>
    </div>
  )
}
