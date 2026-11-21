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
          <button
            onClick={() => router.push('/')}
            className="px-2 py-1 -ml-2 text-sm text-gray-600 hover:text-gray-900 hover:bg-gray-200/60 rounded-lg transition-colors"
          >
            ← 返回
          </button>
          <h1 className="text-2xl font-semibold tracking-tight text-gray-900">回收站</h1>
        </div>

        {loading ? (
          <div className="rounded-xl border border-gray-200 bg-white shadow-card p-4 space-y-3">
            {Array.from({ length: 3 }).map((_, i) => (
              <div key={i} className="flex items-center gap-4">
                <div className="skeleton h-4 w-1/3 rounded" />
                <div className="skeleton h-4 w-16 rounded" />
                <div className="skeleton h-4 flex-1 rounded" />
              </div>
            ))}
          </div>
        ) : items.length === 0 ? (
          <div className="py-24 flex flex-col items-center text-center animate-fade-in">
            <div className="w-14 h-14 mb-4 rounded-2xl bg-gray-100 text-gray-400 flex items-center justify-center">
              <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round">
                <polyline points="3 6 5 6 21 6" />
                <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
              </svg>
            </div>
            <p className="text-sm text-gray-500">回收站为空</p>
          </div>
        ) : (
          <div className="rounded-xl border border-gray-200 bg-white shadow-card overflow-hidden">
            <table className="w-full">
              <thead className="bg-gray-50 text-left text-xs font-medium text-gray-500">
                <tr>
                  <th className="px-4 py-2.5">书名</th>
                  <th className="px-4 py-2.5">类型</th>
                  <th className="px-4 py-2.5">删除时间</th>
                  <th className="px-4 py-2.5 text-right">操作</th>
                </tr>
              </thead>
              <tbody className="text-sm">
                {items.map((p) => (
                  <tr key={p.id} className="border-t border-gray-100 hover:bg-gray-50/60 transition-colors">
                    <td className="px-4 py-3 font-medium text-gray-800">{p.title}</td>
                    <td className="px-4 py-3 text-gray-600">{p.genre || '—'}</td>
                    <td className="px-4 py-3 text-gray-500">
                      {p.deleted_at ? new Date(p.deleted_at).toLocaleString('zh-CN') : '—'}
                    </td>
                    <td className="px-4 py-3 text-right space-x-3">
                      <button onClick={() => restore(p.id)} className="font-medium text-brand-600 hover:text-brand-700 transition-colors">恢复</button>
                      <button onClick={() => setPurgeTarget(p)} className="font-medium text-danger-600 hover:text-danger-700 transition-colors">永久删除</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
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
