'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import { apiFetch } from '@/lib/api'
import { parseVolumeOutlineSource, volumeOutlineText, type VolumeOutlineSource } from '@/lib/volumeOutlines'

type Row = { request_id: string; created_at: string }
type Page = { items: Row[]; next_cursor: string | null }
type Detail = { before: VolumeOutlineSource; after: VolumeOutlineSource; raw: unknown }

export function VolumeOutlineHistory({ projectId, volumeId, revision }: { projectId: string; volumeId: string; revision?: string }) {
  const [rows, setRows] = useState<Row[]>([])
  const [cursor, setCursor] = useState<string | null>(null)
  const [detail, setDetail] = useState<Detail | null>(null)
  const [listError, setListError] = useState<string | null>(null)
  const [detailError, setDetailError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [reading, setReading] = useState(false)
  const listSequence = useRef(0), detailSequence = useRef(0)
  const listController = useRef<AbortController | null>(null), detailController = useRef<AbortController | null>(null)
  const base = `/api/projects/${encodeURIComponent(projectId)}/volume-outlines/${encodeURIComponent(volumeId)}/history`

  const load = useCallback(async (after?: string) => {
    if (after && listController.current) return
    const current = ++listSequence.current
    listController.current?.abort()
    const pending = new AbortController(); listController.current = pending
    const timer = setTimeout(() => pending.abort(), 20_000)
    setLoading(true)
    try {
      const page = await apiFetch<Page>(base + (after ? `?cursor=${encodeURIComponent(after)}` : ''), { signal: pending.signal })
      if (current !== listSequence.current) return
      if (!page || !Array.isArray(page.items) || page.items.some((row) => !row || typeof row.request_id !== 'string' ||
          !row.request_id || typeof row.created_at !== 'string' || !Number.isFinite(Date.parse(row.created_at))) ||
          (page.next_cursor !== null && (typeof page.next_cursor !== 'string' || page.next_cursor !== page.items.at(-1)?.request_id)) ||
          new Set(page.items.map((row) => row.request_id)).size !== page.items.length ||
          (after && page.items.some((row) => row.request_id === after))) throw new Error('历史记录返回不完整。')
      setRows((previous) => after ? [...previous, ...page.items.filter((row) => !previous.some((old) => old.request_id === row.request_id))] : page.items)
      setCursor(page.next_cursor); setListError(null)
    } catch (cause) {
      if (current === listSequence.current) setListError(pending.signal.aborted ? '历史读取超时，请重试。' : cause instanceof Error ? cause.message : '历史记录读取失败。')
    } finally {
      clearTimeout(timer)
      if (current === listSequence.current) { listController.current = null; setLoading(false) }
    }
  }, [base])

  useEffect(() => {
    setReading(false)
    setDetailError(null)
    void load()
    return () => {
      listSequence.current += 1; detailSequence.current += 1
      listController.current?.abort(); detailController.current?.abort()
      listController.current = null; detailController.current = null
    }
  }, [load, revision])

  const read = async (requestId: string) => {
    const current = ++detailSequence.current
    detailController.current?.abort()
    const pending = new AbortController(); detailController.current = pending
    const timer = setTimeout(() => pending.abort(), 20_000)
    setReading(true); setDetail(null)
    try {
      const raw = await apiFetch<{ request_id: string; before: unknown; receipt: { source: unknown } }>(`${base}/${encodeURIComponent(requestId)}`, { signal: pending.signal })
      if (current !== detailSequence.current) return
      if (raw.request_id !== requestId) throw new Error('返回了另一份保存记录。')
      setDetail({ before: parseVolumeOutlineSource(raw.before, projectId, volumeId),
        after: parseVolumeOutlineSource(raw.receipt.source, projectId, volumeId), raw })
      setDetailError(null)
    } catch (cause) {
      if (current === detailSequence.current) setDetailError(pending.signal.aborted ? '保存历史读取超时，请重试。' : cause instanceof Error ? cause.message : '保存历史读取失败。')
    } finally { clearTimeout(timer); if (current === detailSequence.current) { detailController.current = null; setReading(false) } }
  }

  const download = () => {
    if (!detail) return
    const url = URL.createObjectURL(new Blob([JSON.stringify(detail.raw, null, 2)], { type: 'application/json;charset=utf-8' }))
    const link = document.createElement('a'); link.href = url; link.download = '完整卷纲保存历史.json'
    document.body.appendChild(link); link.click(); link.remove(); URL.revokeObjectURL(url)
  }
  return <div className="space-y-3 border-t pt-3 text-sm">
    <p>卷纲保存历史，每页 20 条；可查看修改前后全文及下载完整记录。</p>
    {loading && <p role="status">正在读取保存历史…</p>}
    {listError && <div className="text-red-700"><p role="alert">{listError}</p>
      <button type="button" disabled={loading} onClick={() => void load()} className="min-h-10 text-blue-800">重新读取历史</button></div>}
    {!loading && !listError && !rows.length && <p>尚无通过此入口保存的历史记录。</p>}
    <div className="flex flex-wrap gap-2">{rows.map((row) => <button type="button" key={row.request_id} onClick={() => void read(row.request_id)}
      className="min-h-10 rounded border px-3">{new Date(row.created_at).toLocaleString()}</button>)}</div>
    {cursor && <button type="button" disabled={loading} onClick={() => void load(cursor)} className="min-h-10 text-blue-800 disabled:opacity-50">查看更早记录</button>}
    {reading && <p role="status">正在读取本次修改前后全文…</p>}
    {detailError && <p role="alert" className="text-red-700">{detailError}</p>}
    {detail && <div className="grid gap-3 sm:grid-cols-2">
      <label>修改前<textarea aria-label="历史修改前卷纲" value={volumeOutlineText(detail.before)} readOnly className="mt-1 min-h-64 w-full rounded border p-3" /></label>
      <label>修改后<textarea aria-label="历史修改后卷纲" value={volumeOutlineText(detail.after)} readOnly className="mt-1 min-h-64 w-full rounded border p-3" /></label>
      <button type="button" className="min-h-10 text-blue-800" onClick={download}>下载完整保存记录</button>
    </div>}
  </div>
}
