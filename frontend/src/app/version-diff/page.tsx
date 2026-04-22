'use client'

import dynamic from 'next/dynamic'
import { Suspense, useCallback, useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'next/navigation'
import { apiFetch } from '@/lib/api'

const DiffEditor = dynamic(
  () => import('@monaco-editor/react').then((m) => m.DiffEditor),
  {
    ssr: false,
    loading: () => (
      <div className="p-6 text-sm text-gray-400">加载编辑器…</div>
    ),
  },
)

type VersionNode = {
  id: string
  chapter_id: string
  parent_id: string | null
  branch_name: string
  content_text: string
  word_count: number
  created_at: string
  is_active: boolean
  metadata: Record<string, unknown>
}

function DiffInner() {
  const searchParams = useSearchParams()
  const chapterId = searchParams?.get('chapter_id') || ''
  const [versions, setVersions] = useState<VersionNode[]>([])
  const [aId, setAId] = useState<string>('')
  const [bId, setBId] = useState<string>('')
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [rollingBack, setRollingBack] = useState(false)

  const load = useCallback(async () => {
    if (!chapterId) return
    setLoading(true)
    setError(null)
    try {
      const data = await apiFetch<VersionNode[]>(
        `/api/chapters/${chapterId}/versions`,
      )
      setVersions(data)
      const active = data.find((v) => v.is_active)
      if (active) setBId(active.id)
      if (data[0] && data[0].id !== active?.id) setAId(data[0].id)
      else if (data[1]) setAId(data[1].id)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [chapterId])

  useEffect(() => {
    load()
  }, [load])

  const versionA = useMemo(
    () => versions.find((v) => v.id === aId) || null,
    [versions, aId],
  )
  const versionB = useMemo(
    () => versions.find((v) => v.id === bId) || null,
    [versions, bId],
  )

  const rollback = async () => {
    if (!versionA) return
    if (!window.confirm(`确认回滚到版本 ${versionA.id.slice(0, 8)}？将写回章节正文。`))
      return
    setRollingBack(true)
    setError(null)
    try {
      await apiFetch(
        `/api/chapters/${chapterId}/versions/${versionA.id}/rollback`,
        { method: 'POST' },
      )
      await load()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setRollingBack(false)
    }
  }

  if (!chapterId) {
    return (
      <div className="p-8 text-sm text-gray-500">
        请在 URL 提供 <code>?chapter_id=&lt;chapterId&gt;</code>
      </div>
    )
  }

  return (
    <div className="flex flex-col h-screen pt-12">
      <header className="flex items-center gap-3 px-4 py-2 border-b bg-white">
        <h1 className="text-lg font-semibold whitespace-nowrap">章节版本对比</h1>
        <label className="text-xs">
          A（旧）
          <select
            value={aId}
            onChange={(e) => setAId(e.target.value)}
            className="ml-1 text-xs border rounded px-2 py-1"
          >
            <option value="">-</option>
            {versions.map((v) => (
              <option key={v.id} value={v.id}>
                {v.branch_name} / {v.id.slice(0, 8)} ({v.word_count}w)
                {v.is_active ? ' ★' : ''}
              </option>
            ))}
          </select>
        </label>
        <label className="text-xs">
          B（新）
          <select
            value={bId}
            onChange={(e) => setBId(e.target.value)}
            className="ml-1 text-xs border rounded px-2 py-1"
          >
            <option value="">-</option>
            {versions.map((v) => (
              <option key={v.id} value={v.id}>
                {v.branch_name} / {v.id.slice(0, 8)} ({v.word_count}w)
                {v.is_active ? ' ★' : ''}
              </option>
            ))}
          </select>
        </label>
        <div className="flex-1" />
        <button
          onClick={rollback}
          disabled={!versionA || rollingBack}
          className="px-3 py-1 bg-amber-500 text-white text-xs rounded disabled:opacity-50"
        >
          {rollingBack ? '回滚中…' : `回滚到 A${versionA ? '（' + versionA.id.slice(0, 8) + '）' : ''}`}
        </button>
        <button
          onClick={load}
          className="px-3 py-1 bg-gray-200 text-xs rounded"
        >
          刷新
        </button>
      </header>
      {error && (
        <div className="p-3 bg-red-50 text-red-700 text-sm">{error}</div>
      )}
      <div className="flex-1">
        {loading ? (
          <div className="p-6 text-sm text-gray-400">加载中…</div>
        ) : (
          <DiffEditor
            height="100%"
            original={versionA?.content_text ?? ''}
            modified={versionB?.content_text ?? ''}
            language="markdown"
            options={{
              readOnly: true,
              renderSideBySide: true,
              wordWrap: 'on',
              minimap: { enabled: false },
              scrollBeyondLastLine: false,
            }}
          />
        )}
      </div>
    </div>
  )
}

export default function VersionDiffPage() {
  return (
    <Suspense
      fallback={
        <div className="p-8 text-sm text-gray-400">加载…</div>
      }
    >
      <DiffInner />
    </Suspense>
  )
}
