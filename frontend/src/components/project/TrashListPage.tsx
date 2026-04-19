'use client'

import { useCallback, useEffect, useState } from 'react'
import { useRouter } from 'next/navigation'
import { apiFetch } from '@/lib/api'
import type { Project } from '@/stores/projectStore'
import { PurgeProjectModal } from './PurgeProjectModal'

interface TrashedProject extends Project {
  deleted_at?: string | null
}

export function TrashListPage() {
  const router = useRouter()
  const [items, setItems] = useState<TrashedProject[]>([])
  const [loading, setLoading] = useState(true)
  const [purgeTarget, setPurgeTarget] = useState<TrashedProject | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const data = await apiFetch<{ projects: TrashedProject[] }>('/api/projects?trashed=true')
      setItems(data.projects)
    } finally { setLoading(false) }
  }, [])

  useEffect(() => { load() }, [load])

  const restore = async (id: string) => {
    await apiFetch(`/api/projects/${id}/restore`, { method: 'POST' })
    await load()
  }

  return (
    <div className="min-h-screen pt-12 bg-gray-50">
      <div className="max-w-4xl mx-auto px-6 py-8">
        <div className="flex items-center gap-3 mb-6">
          <button onClick={() => router.push('/')} className="text-sm text-gray-600 hover:text-gray-900">← 返回</button>
          <h1 className="text-2xl font-bold text-gray-900">回收站</h1>
        </div>

        {loading ? (
          <div className="py-20 text-center text-gray-400">加载中...</div>
        ) : items.length === 0 ? (
          <div className="py-20 text-center text-gray-500">回收站为空</div>
        ) : (
          <table className="w-full bg-white rounded-xl overflow-hidden">
            <thead className="bg-gray-50 text-left text-xs text-gray-500 uppercase">
              <tr>
                <th className="px-4 py-2">书名</th>
                <th className="px-4 py-2">类型</th>
                <th className="px-4 py-2">删除时间</th>
                <th className="px-4 py-2 text-right">操作</th>
              </tr>
            </thead>
            <tbody className="text-sm">
              {items.map((p) => (
                <tr key={p.id} className="border-t border-gray-100">
                  <td className="px-4 py-3 font-medium text-gray-800">{p.title}</td>
                  <td className="px-4 py-3 text-gray-600">{p.genre || '—'}</td>
                  <td className="px-4 py-3 text-gray-500">
                    {p.deleted_at ? new Date(p.deleted_at).toLocaleString('zh-CN') : '—'}
                  </td>
                  <td className="px-4 py-3 text-right space-x-2">
                    <button onClick={() => restore(p.id)} className="text-blue-600 hover:underline">恢复</button>
                    <button onClick={() => setPurgeTarget(p)} className="text-red-600 hover:underline">永久删除</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      {purgeTarget && (
        <PurgeProjectModal
          projectId={purgeTarget.id}
          projectTitle={purgeTarget.title}
          onClose={() => setPurgeTarget(null)}
          onDone={async () => { setPurgeTarget(null); await load() }}
        />
      )}
    </div>
  )
}
