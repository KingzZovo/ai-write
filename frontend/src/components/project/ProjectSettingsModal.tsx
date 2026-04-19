'use client'

import { useState } from 'react'
import { apiFetch } from '@/lib/api'
import type { Project } from '@/stores/projectStore'

interface Settings {
  target_total_words?: number | null
  target_chapter_words?: number | null
  [key: string]: unknown
}

export function ProjectSettingsModal({
  project,
  onClose,
  onDone,
}: {
  project: Project
  onClose: () => void
  onDone: () => void
}) {
  const initial = (project.settings_json as Settings | null | undefined) || {}
  const [totalStr, setTotalStr] = useState(
    initial.target_total_words ? String(initial.target_total_words) : ''
  )
  const [chapterStr, setChapterStr] = useState(
    initial.target_chapter_words ? String(initial.target_chapter_words) : ''
  )
  const [busy, setBusy] = useState(false)

  const parseNum = (s: string): number | null => {
    const trimmed = s.trim()
    if (!trimmed) return null
    const n = parseInt(trimmed, 10)
    if (Number.isNaN(n) || n <= 0) return null
    return n
  }

  const save = async () => {
    if (busy) return
    setBusy(true)
    try {
      const next: Settings = {
        ...initial,
        target_total_words: parseNum(totalStr),
        target_chapter_words: parseNum(chapterStr),
      }
      await apiFetch(`/api/projects/${project.id}`, {
        method: 'PUT',
        body: JSON.stringify({ settings_json: next }),
      })
      onDone()
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div className="bg-white rounded-2xl shadow-xl w-full max-w-md mx-4 p-6">
        <h3 className="text-lg font-bold text-gray-900 mb-4">项目设置 — {project.title}</h3>
        <div className="space-y-4">
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">全书目标字数</label>
            <input
              type="number"
              min={1}
              value={totalStr}
              onChange={(e) => setTotalStr(e.target.value)}
              placeholder="留空为不限"
              className="w-full px-3 py-2 text-sm border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500"
            />
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">单章默认字数</label>
            <input
              type="number"
              min={1}
              value={chapterStr}
              onChange={(e) => setChapterStr(e.target.value)}
              placeholder="如 3000，留空不限"
              className="w-full px-3 py-2 text-sm border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500"
            />
          </div>
        </div>
        <div className="flex gap-3 mt-6">
          <button
            onClick={onClose}
            className="flex-1 px-4 py-2 text-sm border border-gray-300 rounded-lg hover:bg-gray-50"
          >
            取消
          </button>
          <button
            onClick={save}
            disabled={busy}
            className="flex-1 px-4 py-2 text-sm bg-blue-600 text-white rounded-lg disabled:opacity-50"
          >
            {busy ? '保存中...' : '保存'}
          </button>
        </div>
      </div>
    </div>
  )
}
