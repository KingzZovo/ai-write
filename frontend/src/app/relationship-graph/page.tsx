'use client'

import dynamic from 'next/dynamic'
import { Suspense, useCallback, useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'next/navigation'
import { apiFetch } from '@/lib/api'
import {
  GRAPH_BG_DARK,
  GRAPH_TEXT_ON_DARK,
  NODE_FILL_PRIMARY,
  SENTIMENT_NEGATIVE,
  SENTIMENT_NEUTRAL,
  SENTIMENT_POSITIVE_ALT,
} from '@/lib/graph-palette'

// react-force-graph-2d touches window/canvas — must be client-only.
const ForceGraph2D = dynamic(
  () => import('react-force-graph-2d').then((m) => m.default),
  { ssr: false },
) as unknown as React.ComponentType<Record<string, unknown>>

type Character = { id: string; name: string }
type Volume = { id: string; title: string; volume_idx: number }
type Relationship = {
  id: string
  source_id: string
  target_id: string
  rel_type: string
  label: string
  sentiment: string
  since_volume_id?: string | null
  until_volume_id?: string | null
}

function GraphInner() {
  const searchParams = useSearchParams()
  const projectId = searchParams?.get('id') || ''

  const [chars, setChars] = useState<Character[]>([])
  const [volumes, setVolumes] = useState<Volume[]>([])
  const [rels, setRels] = useState<Relationship[]>([])
  const [asOfIdx, setAsOfIdx] = useState<number | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const activeVolume = useMemo(
    () => (asOfIdx === null ? null : volumes[asOfIdx] ?? null),
    [asOfIdx, volumes],
  )

  const loadBase = useCallback(async () => {
    if (!projectId) return
    setLoading(true)
    setError(null)
    try {
      const [cs, vs] = await Promise.all([
        apiFetch<{ characters: Character[] }>(
          `/api/projects/${projectId}/characters`,
        ),
        apiFetch<{ volumes: Volume[] }>(
          `/api/projects/${projectId}/volumes`,
        ).catch(() => ({ volumes: [] })),
      ])
      setChars(cs.characters)
      const sortedVols = [...vs.volumes].sort(
        (a, b) => a.volume_idx - b.volume_idx,
      )
      setVolumes(sortedVols)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [projectId])

  const loadRels = useCallback(async () => {
    if (!projectId) return
    try {
      let data: { relationships: Relationship[] }
      if (activeVolume) {
        data = await apiFetch(
          `/api/projects/${projectId}/relationships/as-of/${activeVolume.id}`,
        )
      } else {
        data = await apiFetch(
          `/api/projects/${projectId}/relationships`,
        )
      }
      setRels(data.relationships)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }, [projectId, activeVolume])

  useEffect(() => {
    loadBase()
  }, [loadBase])
  useEffect(() => {
    loadRels()
  }, [loadRels])

  const sentimentColor = (s: string) => {
    if (s === 'positive' || s === 'ally' || s === 'love') return SENTIMENT_POSITIVE_ALT
    if (s === 'negative' || s === 'enemy' || s === 'rival') return SENTIMENT_NEGATIVE
    return SENTIMENT_NEUTRAL
  }

  const graphData = useMemo(() => {
    const nodeSet = new Set<string>()
    rels.forEach((r) => {
      nodeSet.add(r.source_id)
      nodeSet.add(r.target_id)
    })
    const charById = new Map(chars.map((c) => [c.id, c]))
    return {
      nodes: Array.from(nodeSet).map((id) => ({
        id,
        name: charById.get(id)?.name ?? id.slice(0, 8),
      })),
      links: rels.map((r) => ({
        source: r.source_id,
        target: r.target_id,
        label: r.label || r.rel_type,
        color: sentimentColor(r.sentiment),
      })),
    }
  }, [chars, rels])

  if (!projectId) {
    return (
      <div className="p-8 text-sm text-gray-500">
        请在 URL 提供 <code>?id=&lt;projectId&gt;</code>
      </div>
    )
  }

  return (
    <div className="flex flex-col h-screen pt-12">
      <header className="flex items-center justify-between px-4 py-2 border-b">
        <h1 className="text-lg font-semibold">角色关系图</h1>
        <div className="text-xs text-gray-500">
          {loading ? '加载中…' : `${chars.length} 角色 · ${rels.length} 关系`}
          {activeVolume && (
            <>
              {' · '}
              快照: 第 {activeVolume.volume_idx} 卷 《{activeVolume.title}》
            </>
          )}
        </div>
      </header>
      {error && (
        <div className="p-3 bg-red-50 text-red-700 text-sm">{error}</div>
      )}
      <div className="flex-1 relative bg-slate-900">
        {typeof window !== 'undefined' && (
          <ForceGraph2D
            graphData={graphData}
            nodeLabel="name"
            nodeColor={() => NODE_FILL_PRIMARY}
            linkLabel="label"
            linkColor={(link: Record<string, unknown>) =>
              (link.color as string) || SENTIMENT_NEUTRAL
            }
            linkWidth={2}
            linkDirectionalArrowLength={4}
            linkDirectionalArrowRelPos={1}
            backgroundColor={GRAPH_BG_DARK}
            nodeCanvasObject={(
              node: Record<string, unknown>,
              ctx: CanvasRenderingContext2D,
              globalScale: number,
            ) => {
              const name = (node.name as string) || ''
              const fontSize = 12 / globalScale
              ctx.font = `${fontSize}px sans-serif`
              ctx.fillStyle = NODE_FILL_PRIMARY
              ctx.beginPath()
              ctx.arc(
                node.x as number,
                node.y as number,
                4,
                0,
                2 * Math.PI,
              )
              ctx.fill()
              ctx.fillStyle = GRAPH_TEXT_ON_DARK
              ctx.textAlign = 'center'
              ctx.textBaseline = 'middle'
              ctx.fillText(
                name,
                node.x as number,
                (node.y as number) + 8 / globalScale,
              )
            }}
          />
        )}
      </div>
      {volumes.length > 0 && (
        <footer className="border-t px-4 py-3 bg-gray-50">
          <div className="flex items-center gap-3">
            <button
              onClick={() => setAsOfIdx(null)}
              className={`px-3 py-1 text-xs rounded border ${
                asOfIdx === null
                  ? 'bg-blue-600 text-white border-blue-600'
                  : 'bg-white'
              }`}
            >
              全局
            </button>
            <input
              type="range"
              min={0}
              max={volumes.length - 1}
              value={asOfIdx ?? 0}
              onChange={(e) => setAsOfIdx(Number(e.target.value))}
              className="flex-1"
            />
            <div className="text-xs text-gray-600 min-w-[180px] text-right">
              {asOfIdx === null
                ? '拖动以查看各卷快照'
                : `第 ${volumes[asOfIdx].volume_idx} 卷：${volumes[asOfIdx].title}`}
            </div>
          </div>
        </footer>
      )}
    </div>
  )
}

export default function RelationshipGraphPage() {
  return (
    <Suspense
      fallback={
        <div className="p-8 text-sm text-gray-400">加载…</div>
      }
    >
      <GraphInner />
    </Suspense>
  )
}
