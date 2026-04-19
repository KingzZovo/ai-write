'use client'

import { useState } from 'react'
import { apiFetch } from '@/lib/api'
import type { Project } from '@/stores/projectStore'

export function DeleteProjectModal({
  project,
  onClose,
  onDone,
}: {
  project: Project
  onClose: () => void
  onDone: () => void
}) {
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const matches = input.trim() === project.title.trim()

  const submit = async () => {
    if (!matches || busy) return
    setBusy(true)
    try {
      await apiFetch(`/api/projects/${project.id}`, { method: 'DELETE' })
      onDone()
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div className="bg-white rounded-2xl shadow-xl w-full max-w-md mx-4 p-6">
        <h3 className="text-lg font-bold text-red-600 mb-2">⚠ 删除项目</h3>
        <p className="text-sm text-gray-700">
          项目「{project.title}」将被移入回收站，可随时从回收站恢复。要永久删除，请进入回收站操作。
        </p>
        <p className="text-sm text-gray-700 mt-3">
          为确认删除，请输入书名：<span className="font-semibold">{project.title}</span>
        </p>
        <input
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          className="w-full px-3 py-2 mt-2 text-sm border border-gray-300 rounded-lg focus:ring-2 focus:ring-red-500"
          autoFocus
        />
        <div className="flex gap-3 mt-6">
          <button onClick={onClose} className="flex-1 px-4 py-2 text-sm border border-gray-300 rounded-lg hover:bg-gray-50">
            取消
          </button>
          <button
            onClick={submit}
            disabled={!matches || busy}
            className="flex-1 px-4 py-2 text-sm bg-red-600 text-white rounded-lg disabled:opacity-40 disabled:cursor-not-allowed"
          >
            {busy ? '删除中...' : '删除'}
          </button>
        </div>
      </div>
    </div>
  )
}
