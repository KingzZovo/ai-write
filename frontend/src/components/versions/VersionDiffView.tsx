'use client'

import dynamic from 'next/dynamic'
import { useCallback, useEffect, useRef, useState } from 'react'
import { ChapterVersion, VersionSource, loadChapterVersions, loadVersionSource, restoreChapterVersion } from '@/lib/chapterVersions'

const DiffEditor = dynamic(() => import('@monaco-editor/react').then((m) => m.DiffEditor), {
  ssr: false,
  loading: () => <div className="p-6 text-sm text-gray-400">加载编辑器…</div>,
})

function VersionDiffContent({ chapterId }: { chapterId: string }) {
  const [versions, setVersions] = useState<ChapterVersion[]>([])
  const [source, setSource] = useState<VersionSource | null>(null)
  const [aId, setAId] = useState('')
  const [bId, setBId] = useState('saved')
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [force, setForce] = useState(false)
  const [needsRefresh, setNeedsRefresh] = useState(false)
  const mounted = useRef(false)
  const sequence = useRef(0)
  const controller = useRef<AbortController | null>(null)
  const writeBusy = useRef(false)

  const load = useCallback(async () => {
    const request = ++sequence.current
    controller.current?.abort()
    const abort = new AbortController()
    controller.current = abort
    setLoading(true)
    setError(null)
    try {
      const [items, current] = await Promise.all([
        loadChapterVersions(chapterId, abort.signal), loadVersionSource(chapterId, abort.signal),
      ])
      if (!mounted.current || request !== sequence.current) return
      setVersions(items)
      setSource(current)
      setAId((previous) => items.some((v) => v.id === previous) ? previous : items[0]?.id || '')
      setBId('saved')
      setForce(false)
      setNeedsRefresh(false)
    } catch (cause) {
      if (mounted.current && request === sequence.current) setError(cause instanceof Error ? cause.message : '读取版本失败。')
    } finally {
      if (mounted.current && request === sequence.current) setLoading(false)
    }
  }, [chapterId])

  useEffect(() => {
    mounted.current = true
    void load()
    return () => { mounted.current = false; sequence.current += 1; controller.current?.abort() }
  }, [load])

  const versionA = versions.find((v) => v.id === aId)
  const versionB = versions.find((v) => v.id === bId)
  const sameBody = source !== null && versionA?.content_text === source.content_text
  const shorter = Boolean(source && versionA && versionA.word_count < source.word_count)

  const restore = async () => {
    if (!versionA || !source || writeBusy.current || loading || needsRefresh || error || sameBody) return
    if (bId !== 'saved') {
      setBId('saved')
      setNotice('已切换为当前保存稿对照，请核对右侧正文后再确认恢复。')
      return
    }
    if (!window.confirm(`确认用左侧版本恢复「${source.title}」的正文？当前保存稿将完整备份。`)) return
    const selected = versionA
    const reviewedSource = source
    writeBusy.current = true
    setBusy(true)
    setError(null)
    setNotice(null)
    try {
      const result = await restoreChapterVersion(chapterId, selected, reviewedSource, force)
      if (!mounted.current) return
      setSource({ ...reviewedSource, content_text: result.content_text, word_count: result.word_count })
      setNotice(result.changed ? '正文已恢复，原保存稿已完整备份。编辑区中的未保存修改请另外核对。' : '保存稿已与所选版本一致。')
      await load()
    } catch (cause) {
      if (!mounted.current) return
      setError(cause instanceof Error ? cause.message : '恢复未获确认，请刷新保存稿核对。')
      setNeedsRefresh(true)
    } finally {
      writeBusy.current = false
      if (mounted.current) setBusy(false)
    }
  }

  return (
    <div className="flex h-screen flex-col pt-12">
      <header className="flex flex-wrap items-center gap-3 border-b bg-white px-4 py-2">
        <h1 className="text-lg font-semibold">章节版本对比</h1>
        <label className="text-xs">左侧版本
          <select aria-label="左侧版本" value={aId} disabled={loading || busy} onChange={(e) => { setAId(e.target.value); setForce(false); setNotice(null) }} className="ml-1 rounded border px-2 py-1">
            <option value="">请选择</option>
            {versions.map((v) => <option key={v.id} value={v.id}>{v.branch_name} / {v.id.slice(0, 8)}（{v.word_count}字）</option>)}
          </select>
        </label>
        <label className="text-xs">右侧对照
          <select aria-label="右侧对照" value={bId} disabled={loading || busy} onChange={(e) => setBId(e.target.value)} className="ml-1 rounded border px-2 py-1">
            <option value="saved">当前保存稿</option>
            {versions.map((v) => <option key={v.id} value={v.id}>{v.branch_name} / {v.id.slice(0, 8)}（{v.word_count}字）</option>)}
          </select>
        </label>
        <button type="button" disabled={!versionA || !source || busy || loading || needsRefresh || Boolean(error) || sameBody} onClick={() => void restore()} className="rounded bg-amber-600 px-3 py-1 text-xs text-white disabled:opacity-50">
          {busy ? '恢复中…' : sameBody ? '已与保存稿一致' : '用左侧版本恢复正文'}
        </button>
        <button type="button" disabled={busy || loading} onClick={() => void load()} className="rounded bg-gray-200 px-3 py-1 text-xs disabled:opacity-50">刷新保存稿与版本</button>
      </header>
      {shorter && !sameBody && <label className="flex items-center gap-2 border-b px-4 py-2 text-xs text-amber-800">
        <input type="checkbox" checked={force} disabled={busy || loading} onChange={(e) => setForce(e.target.checked)} />
        我已核对，允许恢复为空稿或明显较短的版本
      </label>}
      {error && <p role="alert" className="bg-red-50 p-3 text-sm text-red-700">{error}</p>}
      {notice && <p role="status" className="bg-blue-50 p-3 text-sm text-blue-800">{notice}</p>}
      <div className="min-h-0 flex-1">
        {loading ? <p className="p-6 text-sm text-gray-400">读取保存稿与版本…</p> : <DiffEditor
          height="100%" original={versionA?.content_text ?? ''} modified={bId === 'saved' ? source?.content_text ?? '' : versionB?.content_text ?? ''}
          language="markdown" options={{ readOnly: true, renderSideBySide: true, wordWrap: 'on', minimap: { enabled: false }, scrollBeyondLastLine: false }}
        />}
      </div>
    </div>
  )
}

export function VersionDiffView({ chapterId }: { chapterId: string }) {
  return <VersionDiffContent key={chapterId} chapterId={chapterId} />
}

