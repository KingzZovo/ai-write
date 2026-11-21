'use client'

import { useState } from 'react'

export function BulkDeleteModal({
  count,
  onClose,
  onConfirm,
}: {
  count: number
  onClose: () => void
  onConfirm: () => Promise<void> | void
}) {
  const [busy, setBusy] = useState(false)
  const go = async () => {
    if (busy) return
    setBusy(true)
    try { await onConfirm() } finally { setBusy(false) }
  }
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div className="bg-white rounded-2xl shadow-xl w-full max-w-md mx-4 p-6">
        <h3 className="text-lg font-bold text-red-600 mb-2">⚠ 批量删除</h3>
        <p className="text-sm text-gray-700">
          选中的 {count} 个项目将被移入回收站，可随时恢复。
        </p>
        <div className="flex gap-3 mt-6">
          <button onClick={onClose} className="flex-1 px-4 py-2 text-sm border border-gray-300 rounded-lg hover:bg-gray-50">
            取消
          </button>
          <button
            onClick={go}
            disabled={busy}
            className="flex-1 px-4 py-2 text-sm bg-red-600 text-white rounded-lg disabled:opacity-50"
          >
            {busy ? '删除中...' : '删除'}
          </button>
        </div>
      </div>
    </div>
  )
}
