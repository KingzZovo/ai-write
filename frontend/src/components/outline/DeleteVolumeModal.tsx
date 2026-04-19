'use client'

import { useState } from 'react'
import { apiFetch } from '@/lib/api'

export function DeleteVolumeModal({
  projectId,
  volumeId,
  volumeTitle,
  chapterCount,
  onClose,
  onDone,
}: {
  projectId: string
  volumeId: string
  volumeTitle: string
  chapterCount: number
  onClose: () => void
  onDone: () => void
}) {
  const [busy, setBusy] = useState(false)
  const go = async () => {
    if (busy) return
    setBusy(true)
    try {
      await apiFetch(`/api/projects/${projectId}/volumes/${volumeId}`, { method: 'DELETE' })
      onDone()
    } finally { setBusy(false) }
  }
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div className="bg-white rounded-2xl shadow-xl w-full max-w-md mx-4 p-6">
        <h3 className="text-lg font-bold text-red-600 mb-2">⚠ 删除卷</h3>
        <p className="text-sm text-gray-700">
          卷「{volumeTitle}」及其下 {chapterCount} 章内容将被彻底删除，不可恢复。
        </p>
        <div className="flex gap-3 mt-6">
          <button onClick={onClose} className="flex-1 px-4 py-2 text-sm border border-gray-300 rounded-lg hover:bg-gray-50">取消</button>
          <button onClick={go} disabled={busy} className="flex-1 px-4 py-2 text-sm bg-red-600 text-white rounded-lg disabled:opacity-50">
            {busy ? '删除中...' : '删除'}
          </button>
        </div>
      </div>
    </div>
  )
}
