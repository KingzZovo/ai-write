'use client'

import { Suspense, useCallback, useEffect, useState } from 'react'
import { useSearchParams } from 'next/navigation'
import { apiFetch } from '@/lib/api'

type Entry = {
  id: string
  project_id: string
  actor_type: string
  actor_id: string | null
  target_type: string
  target_id: string | null
  action: string
  before_json: Record<string, unknown> | unknown[] | null
  after_json: Record<string, unknown> | unknown[] | null
  reason: string | null
  created_at: string
}

type Response = { entries: Entry[]; total: number; has_more: boolean }

const ACTOR_OPTIONS = ['', 'user', 'agent', 'critic', 'system']
const TARGET_OPTIONS = ['', 'character', 'world_rule', 'relationship']
const ACTION_OPTIONS = ['', 'create', 'update', 'delete']

function ChangelogInner() {
  const searchParams = useSearchParams()
  const projectId = searchParams?.get('id') || ''
  const [entries, setEntries] = useState<Entry[]>([])
  const [actorType, setActorType] = useState('')
  const [targetType, setTargetType] = useState('')
  const [action, setAction] = useState('')
  const [offset, setOffset] = useState(0)
  const [hasMore, setHasMore] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [expanded, setExpanded] = useState<string | null>(null)

  const limit = 50

  const load = useCallback(
    async (nextOffset: number, append: boolean) => {
      if (!projectId) return
      setLoading(true)
      setError(null)
      const q = new URLSearchParams()
      q.set('limit', String(limit))
      q.set('offset', String(nextOffset))
      if (actorType) q.set('actor_type', actorType)
      if (targetType) q.set('target_type', targetType)
      if (action) q.set('action', action)
      try {
        const data = await apiFetch<Response>(
          `/api/projects/${projectId}/changelog?${q.toString()}`,
        )
        setEntries((prev) =>
          append ? [...prev, ...data.entries] : data.entries,
        )
        setHasMore(data.has_more)
        setOffset(nextOffset)
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e))
      } finally {
        setLoading(false)
      }
    },
    [projectId, actorType, targetType, action],
  )

  useEffect(() => {
    load(0, false)
  }, [load])

  const badge = (kind: string, value: string) => {
    const colorMap: Record<string, string> = {
      create: 'bg-green-100 text-green-700',
      update: 'bg-blue-100 text-blue-700',
      delete: 'bg-red-100 text-red-700',
      user: 'bg-gray-100 text-gray-700',
      agent: 'bg-purple-100 text-purple-700',
      critic: 'bg-amber-100 text-amber-700',
      system: 'bg-slate-100 text-slate-700',
      character: 'bg-indigo-100 text-indigo-700',
      world_rule: 'bg-teal-100 text-teal-700',
      relationship: 'bg-pink-100 text-pink-700',
    }
    return (
      <span
        className={`px-2 py-0.5 text-xs rounded ${
          colorMap[value] || 'bg-gray-100 text-gray-600'
        }`}
        title={kind}
      >
        {value}
      </span>
    )
  }

  if (!projectId) {
    return (
      <div className="p-8 text-sm text-gray-500">
        请在 URL 提供 <code>?id=&lt;projectId&gt;</code>
      </div>
    )
  }

  return (
    <div className="flex flex-col h-screen pt-12">
      <header className="px-4 py-3 border-b bg-white">
        <h1 className="text-lg font-semibold mb-3">设定变更时间轴</h1>
        <div className="flex flex-wrap gap-3 text-xs">
          <label>
            操作者
            <select
              value={actorType}
              onChange={(e) => setActorType(e.target.value)}
              className="ml-1 border rounded px-2 py-1"
            >
              {ACTOR_OPTIONS.map((o) => (
                <option key={o} value={o}>
                  {o || '全部'}
                </option>
              ))}
            </select>
          </label>
          <label>
            对象类型
            <select
              value={targetType}
              onChange={(e) => setTargetType(e.target.value)}
              className="ml-1 border rounded px-2 py-1"
            >
              {TARGET_OPTIONS.map((o) => (
                <option key={o} value={o}>
                  {o || '全部'}
                </option>
              ))}
            </select>
          </label>
          <label>
            动作
            <select
              value={action}
              onChange={(e) => setAction(e.target.value)}
              className="ml-1 border rounded px-2 py-1"
            >
              {ACTION_OPTIONS.map((o) => (
                <option key={o} value={o}>
                  {o || '全部'}
                </option>
              ))}
            </select>
          </label>
        </div>
      </header>
      {error && (
        <div className="p-3 bg-red-50 text-red-700 text-sm">{error}</div>
      )}
      <main className="flex-1 overflow-y-auto">
        {entries.length === 0 && !loading && (
          <div className="p-6 text-sm text-gray-400">暂无变更记录</div>
        )}
        <ul className="divide-y">
          {entries.map((e) => {
            const isOpen = expanded === e.id
            return (
              <li key={e.id} className="p-4 hover:bg-gray-50">
                <div className="flex items-center gap-2 flex-wrap text-xs">
                  <span className="text-gray-500 min-w-[150px]">
                    {new Date(e.created_at).toLocaleString()}
                  </span>
                  {badge('actor', e.actor_type)}
                  {badge('target', e.target_type)}
                  {badge('action', e.action)}
                  {e.reason && (
                    <span className="text-gray-500">· {e.reason}</span>
                  )}
                  <span className="text-gray-400 font-mono">
                    {e.target_id?.slice(0, 8) ?? '—'}
                  </span>
                  <button
                    onClick={() => setExpanded(isOpen ? null : e.id)}
                    className="ml-auto text-blue-600 underline"
                  >
                    {isOpen ? '收起' : '查看详情'}
                  </button>
                </div>
                {isOpen && (
                  <div className="mt-3 grid grid-cols-2 gap-3 text-xs">
                    <div>
                      <div className="font-semibold mb-1">before</div>
                      <pre className="bg-gray-50 p-2 rounded overflow-auto max-h-64">
{JSON.stringify(e.before_json ?? {}, null, 2)}
                      </pre>
                    </div>
                    <div>
                      <div className="font-semibold mb-1">after</div>
                      <pre className="bg-gray-50 p-2 rounded overflow-auto max-h-64">
{JSON.stringify(e.after_json ?? {}, null, 2)}
                      </pre>
                    </div>
                  </div>
                )}
              </li>
            )
          })}
        </ul>
      </main>
      <footer className="border-t px-4 py-2 flex items-center gap-2 text-xs bg-gray-50">
        <span className="text-gray-500">
          显示 {entries.length} 条 · offset {offset}
        </span>
        <div className="flex-1" />
        <button
          onClick={() => load(0, false)}
          disabled={loading}
          className="px-3 py-1 bg-gray-200 rounded disabled:opacity-50"
        >
          刷新
        </button>
        <button
          onClick={() => load(offset + limit, true)}
          disabled={!hasMore || loading}
          className="px-3 py-1 bg-blue-600 text-white rounded disabled:opacity-50"
        >
          {loading ? '加载中…' : hasMore ? '加载更多' : '已到底'}
        </button>
      </footer>
    </div>
  )
}

export default function ChangelogPage() {
  return (
    <Suspense
      fallback={
        <div className="p-8 text-sm text-gray-400">加载…</div>
      }
    >
      <ChangelogInner />
    </Suspense>
  )
}
