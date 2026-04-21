'use client'

import { useState, useEffect, useCallback } from 'react'
import { apiFetch } from '@/lib/api'

interface CollectionStats {
  name: string
  count: number
  dim: number
  distance: string
  sample_payloads: Array<Record<string, unknown>>
  error?: string
}

interface Point {
  id: string | number
  payload: Record<string, unknown>
}

interface SearchHit {
  score: number
  id: string | number
  payload: Record<string, unknown>
}

interface RebuildProgress {
  status: string
  done?: number
  total?: number
  current_chapter?: number
  failed?: string[]
}

export default function VectorPage() {
  const [collections, setCollections] = useState<CollectionStats[]>([])
  const [selected, setSelected] = useState<string>('chapter_summaries')
  const [points, setPoints] = useState<Point[]>([])
  const [searchQuery, setSearchQuery] = useState('')
  const [searchResults, setSearchResults] = useState<SearchHit[]>([])
  const [searching, setSearching] = useState(false)
  const [projectIdForRebuild, setProjectIdForRebuild] = useState('')
  const [rebuildProgress, setRebuildProgress] = useState<RebuildProgress | null>(null)

  const loadCollections = useCallback(async () => {
    try {
      const r = await apiFetch<{ collections: CollectionStats[] }>('/api/vector-store/collections')
      setCollections(r.collections)
    } catch { /* */ }
  }, [])

  const loadPoints = useCallback(async (name: string) => {
    try {
      const r = await apiFetch<{ points: Point[] }>(`/api/vector-store/${name}/points?limit=50`)
      setPoints(r.points)
    } catch {
      setPoints([])
    }
  }, [])

  useEffect(() => { loadCollections() }, [loadCollections])
  useEffect(() => { if (selected) loadPoints(selected) }, [selected, loadPoints])

  const handleDelete = async (id: string | number) => {
    if (!confirm('删除此向量点？')) return
    await apiFetch(`/api/vector-store/${selected}/points`, {
      method: 'DELETE',
      body: JSON.stringify({ point_ids: [id] }),
    })
    loadPoints(selected)
    loadCollections()
  }

  const handleSearch = async () => {
    if (!searchQuery.trim()) return
    setSearching(true)
    try {
      const r = await apiFetch<{ results: SearchHit[] }>(
        `/api/vector-store/${selected}/search`,
        { method: 'POST', body: JSON.stringify({ query_text: searchQuery, top_k: 10 }) }
      )
      setSearchResults(r.results)
    } catch (err) {
      alert(err instanceof Error ? err.message : '搜索失败')
    } finally {
      setSearching(false)
    }
  }

  const triggerRebuild = async () => {
    if (!projectIdForRebuild.trim()) { alert('请输入 project_id'); return }
    await apiFetch(`/api/vector-store/projects/${projectIdForRebuild}/rebuild-rag`, {
      method: 'POST',
      body: JSON.stringify({ force: false }),
    })
    setRebuildProgress({ status: 'running', done: 0, total: 0 })
    pollProgress()
  }

  const pollProgress = useCallback(() => {
    const iv = setInterval(async () => {
      try {
        const r = await apiFetch<RebuildProgress>(
          `/api/vector-store/rebuild-progress?project_id=${projectIdForRebuild}`
        )
        setRebuildProgress(r)
        if (r.status === 'completed' || r.status === 'partial' || r.status === 'idle') {
          clearInterval(iv)
          loadCollections()
        }
      } catch {
        clearInterval(iv)
      }
    }, 2000)
  }, [projectIdForRebuild, loadCollections])

  return (
    <div className="pt-14 px-4 md:px-8 max-w-7xl mx-auto pb-12">
      <h1 className="text-2xl font-bold mb-6">向量存储</h1>

      <div className="grid grid-cols-12 gap-4">
        {/* Left: collection list + rebuild */}
        <aside className="col-span-3 space-y-2">
          {collections.map(c => (
            <button
              key={c.name} onClick={() => setSelected(c.name)}
              className={`w-full text-left p-3 rounded border ${
                selected === c.name ? 'border-blue-500 bg-blue-50' : 'border-gray-200 bg-white'
              }`}
            >
              <div className="font-medium text-sm">{c.name}</div>
              <div className="text-xs text-gray-500">{c.count} 条 · {c.dim}d · {c.distance}</div>
              {c.error && <div className="text-xs text-red-500 mt-1">{c.error}</div>}
            </button>
          ))}

          <div className="pt-4 border-t border-gray-200 mt-4">
            <h3 className="text-sm font-semibold mb-2">RAG 回溯</h3>
            <p className="text-xs text-gray-500 mb-2">对指定项目的所有章节，重新生成摘要并写入 chapter_summaries。</p>
            <input
              value={projectIdForRebuild}
              onChange={e => setProjectIdForRebuild(e.target.value)}
              placeholder="project_id"
              className="w-full px-2 py-1 text-xs border rounded mb-2"
            />
            <button
              onClick={triggerRebuild}
              className="w-full px-3 py-1.5 text-xs bg-blue-600 text-white rounded"
            >
              触发回溯
            </button>
            {rebuildProgress && (
              <div className="text-xs mt-2 text-gray-600">
                状态: {rebuildProgress.status}
                {rebuildProgress.total ? ` · ${rebuildProgress.done || 0}/${rebuildProgress.total}` : ''}
                {rebuildProgress.failed && rebuildProgress.failed.length > 0 && (
                  <span className="text-red-600"> · 失败 {rebuildProgress.failed.length}</span>
                )}
              </div>
            )}
          </div>
        </aside>

        {/* Middle: points table */}
        <section className="col-span-6 bg-white rounded border border-gray-200 p-4">
          <div className="flex items-center justify-between mb-3">
            <h2 className="font-semibold">{selected}</h2>
            <span className="text-xs text-gray-500">最近 50 条</span>
          </div>
          <div className="space-y-2 max-h-[70vh] overflow-y-auto">
            {points.length === 0 ? (
              <p className="text-sm text-gray-400 text-center py-8">无数据</p>
            ) : points.map(p => (
              <div key={String(p.id)} className="p-2 bg-gray-50 rounded text-xs">
                <div className="flex justify-between mb-1">
                  <code className="text-gray-500">{String(p.id)}</code>
                  <button onClick={() => handleDelete(p.id)}
                    className="text-red-500 text-[10px]">删除</button>
                </div>
                <pre className="whitespace-pre-wrap text-gray-700 break-all">{JSON.stringify(p.payload, null, 2)}</pre>
              </div>
            ))}
          </div>
        </section>

        {/* Right: search tester */}
        <section className="col-span-3 bg-white rounded border border-gray-200 p-4">
          <h2 className="font-semibold mb-3">检索测试</h2>
          <input
            value={searchQuery}
            onChange={e => setSearchQuery(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter') handleSearch() }}
            placeholder="输入关键词/语句"
            className="w-full px-2 py-1 text-sm border rounded mb-2"
          />
          <button
            onClick={handleSearch}
            disabled={searching}
            className="w-full px-3 py-1.5 text-sm bg-blue-600 text-white rounded mb-3 disabled:opacity-50"
          >
            {searching ? '搜索中...' : '搜索'}
          </button>
          <div className="space-y-2 max-h-[65vh] overflow-y-auto">
            {searchResults.map(h => (
              <div key={String(h.id)} className="p-2 bg-gray-50 rounded text-xs">
                <div className="text-green-600 mb-1">score: {h.score.toFixed(3)}</div>
                <pre className="whitespace-pre-wrap break-all">{JSON.stringify(h.payload, null, 2)}</pre>
              </div>
            ))}
          </div>
        </section>
      </div>
    </div>
  )
}
