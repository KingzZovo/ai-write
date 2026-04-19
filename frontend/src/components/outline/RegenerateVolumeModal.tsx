'use client'

import { useState } from 'react'
import { apiSSE } from '@/lib/api'

export function RegenerateVolumeModal({
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
  const [progress, setProgress] = useState('')

  const go = () => {
    if (busy) return
    setBusy(true)
    setProgress('准备中...')
    apiSSE(
      `/api/projects/${projectId}/volumes/${volumeId}/regenerate`,
      {},
      (text) => setProgress((p) => (p + text).slice(-600)),
      () => { setBusy(false); onDone() },
      (evt) => {
        if (evt.status === 'done') {
          setProgress(`已生成 ${evt.chapters_created} 章`)
        }
        if (typeof evt.error === 'string') {
          setProgress(`错误：${evt.error}`)
        }
      },
    )
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div className="bg-white rounded-2xl shadow-xl w-full max-w-md mx-4 p-6">
        <h3 className="text-lg font-bold text-red-600 mb-2">⚠ 重新生成卷大纲</h3>
        <p className="text-sm text-gray-700">
          卷「{volumeTitle}」下 {chapterCount} 章内容和本卷大纲将被删除，然后 AI 将根据全书大纲重新生成。此操作不可撤销。
        </p>
        {progress && (
          <pre className="mt-3 text-xs text-gray-700 bg-gray-50 p-3 rounded border max-h-48 overflow-y-auto whitespace-pre-wrap">
            {progress}
          </pre>
        )}
        <div className="flex gap-3 mt-6">
          <button
            onClick={onClose}
            disabled={busy}
            className="flex-1 px-4 py-2 text-sm border border-gray-300 rounded-lg hover:bg-gray-50 disabled:opacity-50"
          >
            {busy ? '运行中...' : '取消'}
          </button>
          {!busy && (
            <button
              onClick={go}
              className="flex-1 px-4 py-2 text-sm bg-red-600 text-white rounded-lg"
            >
              确认重生
            </button>
          )}
        </div>
      </div>
    </div>
  )
}
