'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import { apiFetch } from '@/lib/api'
import { arcActionLabels, arcProgressPath, downloadArcRecord, parseArcHistoryDetail,
  type ArcAction, type ArcHistoryDetail } from '@/lib/arcProgress'

type Row = { request_id: string; sequence: number; action: ArcAction; chapter_id: string | null; created_at: string }
type Page = { items: Row[]; next_cursor: string | null }

export function ArcProgressHistory({ projectId, volumeId, revision }: { projectId: string; volumeId: string; revision?: string }) {
  const [rows, setRows] = useState<Row[]>([]), [cursor, setCursor] = useState<string | null>(null)
  const [detail, setDetail] = useState<ArcHistoryDetail | null>(null)
  const [error, setError] = useState<string | null>(null), [readingError, setReadingError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false), [reading, setReading] = useState(false)
  const listVersion = useRef(0), detailVersion = useRef(0)
  const listController = useRef<AbortController | null>(null), detailController = useRef<AbortController | null>(null)
  const base = arcProgressPath(projectId, volumeId) + '/history'
  const load = useCallback(async (after?: string) => {
    if (after && listController.current) return
    const version = ++listVersion.current
    listController.current?.abort()
    const controller = new AbortController(); listController.current = controller
    const timer = setTimeout(() => controller.abort(), 20_000)
    setLoading(true)
    try {
      const page = await apiFetch<Page>(base + (after ? `?cursor=${encodeURIComponent(after)}` : ''), { signal: controller.signal })
      if (version !== listVersion.current) return
      if (!page || !Array.isArray(page.items) || page.items.some((row, i) => !row || typeof row.request_id !== 'string' ||
          !Number.isSafeInteger(row.sequence) || row.sequence <= 0 || !Object.hasOwn(arcActionLabels, row.action) ||
          !Number.isFinite(Date.parse(row.created_at)) || (i > 0 && row.sequence >= page.items[i-1].sequence)) ||
          (page.next_cursor !== null && page.next_cursor !== page.items.at(-1)?.request_id)) throw new Error('历史记录返回不完整。')
      setRows(previous => after ? [...previous, ...page.items.filter(row => !previous.some(old => old.request_id === row.request_id))] : page.items)
      setCursor(page.next_cursor); setError(null)
    } catch (cause) {
      if (version === listVersion.current) setError(cause instanceof Error ? cause.message : '历史读取失败。')
    } finally { clearTimeout(timer); if (version === listVersion.current) { listController.current = null; setLoading(false) } }
  }, [base])
  useEffect(() => {
    setRows([]); setCursor(null); setDetail(null); setReading(false); setReadingError(null)
    void load()
    return () => {
      listVersion.current += 1; detailVersion.current += 1
      listController.current?.abort(); detailController.current?.abort()
      listController.current = null; detailController.current = null
    }
  }, [load, revision])
  const read = async (requestId: string) => {
    const version = ++detailVersion.current
    detailController.current?.abort()
    const controller = new AbortController(); detailController.current = controller
    const timer = setTimeout(() => controller.abort(), 20_000)
    setReading(true); setReadingError(null); setDetail(null)
    try {
      const raw = await apiFetch(`${base}/${encodeURIComponent(requestId)}`, { signal: controller.signal })
      if (version === detailVersion.current) setDetail(parseArcHistoryDetail(raw, requestId))
    } catch (cause) {
      if (version === detailVersion.current) setReadingError(cause instanceof Error ? cause.message : '原回执读取失败。')
    } finally { clearTimeout(timer); if (version === detailVersion.current) { detailController.current = null; setReading(false) } }
  }
  const manuscript = detail?.manuscript
  const oldProgress = detail?.before?.legacy as Record<string, unknown> | undefined
  const saved = detail?.receipt?.progress as Record<string, unknown> | undefined
  return <section className="space-y-3 border-t pt-3" aria-label="弧进度历史">
    <div className="flex items-center justify-between gap-2"><h3 className="font-semibold">作者进度历史</h3>
      <button type="button" disabled={loading} onClick={() => void load()} className="min-h-10 text-blue-800">刷新历史</button></div>
    <p className="text-xs text-gray-600">历史保留当时的保存稿和决定；删章、删卷后仍可查阅。</p>
    {loading && <p role="status">正在读取进度历史…</p>}
    {error && <p role="alert" className="text-red-700">{error}</p>}
    {!loading && !error && !rows.length && <p>尚无通过此入口保存的进度。</p>}
    <div className="space-y-2">{rows.map(row => <button type="button" key={row.request_id} onClick={() => void read(row.request_id)}
      className="block min-h-10 w-full rounded border p-2 text-left">{arcActionLabels[row.action]} · {new Date(row.created_at).toLocaleString()}</button>)}</div>
    {cursor && <button type="button" disabled={loading} onClick={() => void load(cursor)} className="min-h-10 text-blue-800">查看更早记录</button>}
    {reading && <p role="status">正在读取本次决定与原保存稿…</p>}
    {readingError && <p role="alert" className="text-red-700">{readingError}</p>}
    {detail && <div className="space-y-2 rounded border p-3">
      {!detail.integrity_valid && <p role="alert" className="text-red-700">这份历史的完整性无法核对，原始记录仍可下载。</p>}
      {typeof manuscript?.title === 'string' && <h4 className="font-semibold">当时的《{manuscript.title}》</h4>}
      {typeof manuscript?.content_text === 'string' && <textarea aria-label="历史完整保存稿" readOnly value={manuscript.content_text} className="min-h-72 w-full rounded border p-3 leading-7" />}
      {typeof detail.before?.author_note === 'string' && detail.before.author_note && <p className="whitespace-pre-wrap">作者说明：{detail.before.author_note}</p>}
      {typeof saved?.direction === 'string' && <p className="whitespace-pre-wrap">当时的下一步方向：{saved.direction}</p>}
      {typeof saved?.closing_note === 'string' && saved.closing_note && <p className="whitespace-pre-wrap">结束说明：{saved.closing_note}</p>}
      {oldProgress && <div><p>首次核对前的旧报告（未作为正文证明）：</p>
        <pre className="max-h-60 overflow-auto whitespace-pre-wrap">{JSON.stringify(oldProgress, null, 2)}</pre></div>}
      <button type="button" onClick={() => downloadArcRecord(JSON.stringify(detail, null, 2), '完整弧进度记录.json')}
        className="min-h-10 text-blue-800">下载完整原记录</button>
    </div>}
  </section>
}
