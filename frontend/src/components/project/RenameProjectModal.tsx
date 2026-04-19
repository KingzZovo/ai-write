'use client'

import { useState } from 'react'
import { apiFetch } from '@/lib/api'
import type { Project } from '@/stores/projectStore'

export function RenameProjectModal({
  project,
  onClose,
  onDone,
}: {
  project: Project
  onClose: () => void
  onDone: () => void
}) {
  const [title, setTitle] = useState(project.title)
  const [busy, setBusy] = useState(false)

  const submit = async () => {
    const trimmed = title.trim()
    if (!trimmed || trimmed === project.title || busy) return
    setBusy(true)
    try {
      await apiFetch(`/api/projects/${project.id}`, {
        method: 'PUT',
        body: JSON.stringify({ title: trimmed }),
      })
      onDone()
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div className="bg-white rounded-2xl shadow-xl w-full max-w-md mx-4 p-6">
        <h3 className="text-lg font-bold text-gray-900 mb-4">重命名项目</h3>
        <input
          type="text"
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          className="w-full px-3 py-2 text-sm border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500"
          autoFocus
          onKeyDown={(e) => e.key === 'Enter' && submit()}
        />
        <div className="flex gap-3 mt-6">
          <button onClick={onClose} className="flex-1 px-4 py-2 text-sm border border-gray-300 rounded-lg hover:bg-gray-50">
            取消
          </button>
          <button
            onClick={submit}
            disabled={!title.trim() || title.trim() === project.title || busy}
            className="flex-1 px-4 py-2 text-sm bg-blue-600 text-white rounded-lg disabled:opacity-50"
          >
            {busy ? '保存中...' : '保存'}
          </button>
        </div>
      </div>
    </div>
  )
}
