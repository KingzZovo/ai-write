'use client'

import { useState } from 'react'
import { apiFetch } from '@/lib/api'
import type { Project } from '@/stores/projectStore'

const GENRES = ['玄幻', '仙侠', '都市', '言情', '悬疑', '科幻', '历史', '其他'] as const

export function NewProjectModal({
  onClose,
  onCreated,
}: {
  onClose: () => void
  onCreated: (p: Project) => void
}) {
  const [title, setTitle] = useState('')
  const [genre, setGenre] = useState<string>(GENRES[0])
  const [premise, setPremise] = useState('')
  const [busy, setBusy] = useState(false)

  const submit = async () => {
    if (!title.trim() || busy) return
    setBusy(true)
    try {
      const project = await apiFetch<Project>('/api/projects', {
        method: 'POST',
        body: JSON.stringify({ title: title.trim(), genre, premise: premise.trim() || null }),
      })
      onCreated(project)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div className="bg-white rounded-2xl shadow-xl w-full max-w-md mx-4 p-6">
        <h3 className="text-lg font-bold text-gray-900 mb-4">新建项目</h3>
        <div className="space-y-4">
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">
              书名 <span className="text-red-500">*</span>
            </label>
            <input
              type="text"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="输入小说名称"
              className="w-full px-3 py-2 text-sm border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500"
              autoFocus
            />
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">类型</label>
            <select
              value={genre}
              onChange={(e) => setGenre(e.target.value)}
              className="w-full px-3 py-2 text-sm border border-gray-300 rounded-lg"
            >
              {GENRES.map((g) => <option key={g} value={g}>{g}</option>)}
            </select>
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">故事梗概</label>
            <textarea
              value={premise}
              onChange={(e) => setPremise(e.target.value)}
              placeholder="简要描述你的小说设定和核心创意..."
              className="w-full px-3 py-2 text-sm border border-gray-300 rounded-lg resize-none h-24"
            />
          </div>
        </div>
        <div className="flex gap-3 mt-6">
          <button onClick={onClose} className="flex-1 px-4 py-2 text-sm border border-gray-300 rounded-lg hover:bg-gray-50">
            取消
          </button>
          <button
            onClick={submit}
            disabled={!title.trim() || busy}
            className="flex-1 px-4 py-2 text-sm bg-blue-600 text-white rounded-lg disabled:opacity-50"
          >
            {busy ? '创建中...' : '创建项目'}
          </button>
        </div>
      </div>
    </div>
  )
}
