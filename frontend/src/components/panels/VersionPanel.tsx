'use client'

import { useState, useEffect, useCallback, useRef } from 'react'
import { History } from 'lucide-react'
import { apiFetch } from '@/lib/api'
import { ChapterVersion, loadChapterVersions, loadVersionSource, parseChapterVersion, versionSourceLabel } from '@/lib/chapterVersions'

function VersionHistory({ chapterId }: { chapterId: string }) {
  const [versions, setVersions] = useState<ChapterVersion[]>([])
  const [loading, setLoading] = useState(true)
  const [selectedVersionId, setSelectedVersionId] = useState<string | null>(null)
  const [diff, setDiff] = useState<string | null>(null)
  const [diffLoading, setDiffLoading] = useState(false)
  const [diffError, setDiffError] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [showBranchForm, setShowBranchForm] = useState(false)
  const [branchName, setBranchName] = useState('')
  const [busy, setBusy] = useState(false)
  const mounted = useRef(false)
  const loadSequence = useRef(0)
  const diffSequence = useRef(0)
  const writeBusy = useRef(false)
  const controller = useRef<AbortController | null>(null)

  const fetchVersions = useCallback(async () => {
    const request = ++loadSequence.current
    ++diffSequence.current
    controller.current?.abort()
    const abort = new AbortController()
    controller.current = abort
    setLoading(true)
    setError(null)
    setDiff(null)
    setDiffError(null)
    setDiffLoading(false)
    try {
      const data = await loadChapterVersions(chapterId, abort.signal)
      if (!mounted.current || request !== loadSequence.current) return
      setVersions(data)
      setSelectedVersionId((id) => data.some((v) => v.id === id) ? id : null)
    } catch (cause) {
      if (mounted.current && request === loadSequence.current) setError(cause instanceof Error ? cause.message : '读取版本失败。')
    } finally {
      if (mounted.current && request === loadSequence.current) setLoading(false)
    }
  }, [chapterId])

  useEffect(() => {
    mounted.current = true
    void fetchVersions()
    return () => { mounted.current = false; ++loadSequence.current; ++diffSequence.current; controller.current?.abort() }
  }, [fetchVersions])

  const activeVersions = versions.filter((v) => v.is_active)
  const activeVersion = activeVersions.length === 1 ? activeVersions[0] : undefined
  const branchSource = selectedVersionId || activeVersion?.id

  const selectVersion = async (id: string) => {
    const request = ++diffSequence.current
    setDiff(null)
    setDiffError(null)
    setDiffLoading(false)
    if (selectedVersionId === id) { setSelectedVersionId(null); return }
    setSelectedVersionId(id)
    if (!activeVersion || activeVersion.id === id) return
    setDiffLoading(true)
    try {
      const data = await apiFetch<{ version_a: string; version_b: string; diff: string }>(
        `/api/chapters/${chapterId}/versions/diff?a=${activeVersion.id}&b=${id}`,
      )
      if (!data || data.version_a !== activeVersion.id || data.version_b !== id || typeof data.diff !== 'string') throw new Error('差异响应不匹配，请重新选择。')
      if (mounted.current && request === diffSequence.current) setDiff(data.diff)
    } catch (cause) {
      if (mounted.current && request === diffSequence.current) setDiffError(cause instanceof Error ? cause.message : '读取差异失败。')
    } finally {
      if (mounted.current && request === diffSequence.current) setDiffLoading(false)
    }
  }

  const runAction = async (action: () => Promise<string>, onConfirmed?: () => void) => {
    if (writeBusy.current || loading) return
    writeBusy.current = true
    setBusy(true)
    setError(null)
    setNotice(null)
    try {
      const message = await action()
      if (!mounted.current) return
      onConfirmed?.()
      setNotice(message)
      await fetchVersions()
    } catch (cause) {
      if (mounted.current) setError(cause instanceof Error ? cause.message : '操作未获确认，请保留输入并刷新核对。')
    } finally {
      writeBusy.current = false
      if (mounted.current) setBusy(false)
    }
  }

  const createBranch = () => {
    const name = branchName.trim(), sourceId = branchSource
    if (!name || !sourceId) return
    void runAction(async () => {
      const result = parseChapterVersion(await apiFetch<unknown>(`/api/chapters/${chapterId}/versions/branch`, {
        method: 'POST', body: JSON.stringify({ source_version_id: sourceId, branch_name: name }),
      }), chapterId)
      if (result.parent_id !== sourceId || result.branch_name !== name) throw new Error('分支创建结果未获完整确认，请保留输入并刷新核对。')
      return '分支已创建，章节正文保持原稿。'
    }, () => { setBranchName(''); setShowBranchForm(false) })
  }

  const saveSnapshot = () => void runAction(async () => {
    const source = await loadVersionSource(chapterId)
    const result = parseChapterVersion(await apiFetch<unknown>(`/api/chapters/${chapterId}/versions`, {
      method: 'POST', body: JSON.stringify({ content: source.content_text, branch_name: 'main', metadata: { source: 'user_edit' } }),
    }), chapterId)
    if (result.content_text !== source.content_text) throw new Error('快照保存结果未获完整确认，请刷新核对。')
    return '已保存正文快照。'
  })

  const markBaseline = (id: string) => void runAction(async () => {
    const result = await apiFetch<{ status: string; active_version_id: string; applied_to_chapter: boolean }>(`/api/chapters/${chapterId}/versions/${id}/activate`, { method: 'POST' })
    if (result?.status !== 'ok' || result.active_version_id !== id || result.applied_to_chapter !== false) throw new Error('版本选择未获完整确认，请刷新核对。')
    return '已选为对比基准。恢复正文请打开完整对比页。'
  })

  const formatTime = (value: string) => {
    const date = new Date(value)
    return Number.isNaN(date.getTime()) ? '时间未知' : date.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
  }

  return <div className="space-y-3">
    <div className="flex items-center justify-between">
      <h3 className="text-sm font-semibold text-gray-900">版本历史</h3>
      <button type="button" disabled={busy || loading} onClick={() => void fetchVersions()} className="text-xs text-blue-700 disabled:opacity-50">刷新版本</button>
    </div>
    <div className="flex gap-3 text-xs">
      <button type="button" disabled={busy || loading || Boolean(error)} onClick={saveSnapshot} className="text-blue-700 disabled:opacity-50">保存正文快照</button>
      <button type="button" disabled={busy || loading || !versions.length} onClick={() => setShowBranchForm((open) => !open)} className="text-blue-700 disabled:opacity-50">创建分支</button>
    </div>
    <p className="text-xs text-gray-500">快照读取已保存正文；对比基准只用于比较。</p>
    {error && <p role="alert" className="rounded bg-red-50 p-2 text-xs text-red-700">{error}</p>}
    {notice && <p role="status" className="rounded bg-blue-50 p-2 text-xs text-blue-800">{notice}</p>}
    {showBranchForm && <div className="space-y-2 rounded-lg bg-gray-50 p-2.5">
      <label className="block text-xs">分支名称
        <input aria-label="分支名称" value={branchName} maxLength={100} disabled={busy} onChange={(e) => setBranchName(e.target.value)} className="mt-1 w-full rounded border px-2 py-1" />
      </label>
      <p className="text-xs text-gray-500">{branchSource ? `从版本 ${branchSource.slice(0, 8)} 创建。` : '请先选择一个版本。'}</p>
      <div className="flex gap-3 text-xs">
        <button type="button" disabled={busy || loading || !branchName.trim() || !branchSource} onClick={createBranch} className="text-blue-700 disabled:opacity-50">{busy ? '处理中…' : '确认创建分支'}</button>
        <button type="button" disabled={busy} onClick={() => setShowBranchForm(false)}>收起</button>
      </div>
    </div>}
    {activeVersions.length > 1 && <p className="text-xs text-amber-800">历史记录有多个选中标记，请明确选择一个对比基准。</p>}
    {loading ? <p className="text-xs text-gray-400">加载版本中…</p> : versions.length === 0 ? error ? null : <div className="flex flex-col items-center gap-1 py-4 text-center">
      <History className="h-5 w-5 text-gray-300" aria-hidden /><p className="text-xs text-gray-400">暂无版本记录。</p>
    </div> : <div className="space-y-2">{versions.map((version) => <div key={version.id} className={`rounded-lg border p-2.5 text-xs ${selectedVersionId === version.id ? 'border-blue-400 bg-blue-50' : 'border-gray-200 bg-white'}`}>
      <button type="button" disabled={busy} onClick={() => void selectVersion(version.id)} aria-pressed={selectedVersionId === version.id} className="w-full text-left">
        <span className="font-medium">{version.branch_name}</span>
        {version.is_active && <span className="ml-2 text-blue-700">已选</span>}
        <span className="mt-1 flex justify-between text-gray-500"><span>{version.word_count.toLocaleString()} 字 · {versionSourceLabel(version.source)}</span><span>{formatTime(version.created_at)}</span></span>
      </button>
      <a href={`/version-diff?chapter_id=${encodeURIComponent(chapterId)}`} target="_blank" rel="noreferrer" className="mt-2 inline-block text-blue-700 underline">完整对比与恢复正文</a>
      {selectedVersionId === version.id && !version.is_active && <button type="button" disabled={busy || Boolean(error)} onClick={() => markBaseline(version.id)} className="mt-2 block w-full rounded bg-blue-600 px-2 py-1 text-white disabled:opacity-50">{busy ? '处理中…' : '选为对比基准'}</button>}
    </div>)}</div>}
    {selectedVersionId && activeVersion && selectedVersionId !== activeVersion.id && <div>
      <h4 className="mb-1 text-xs font-medium text-gray-600">与对比基准的差异</h4>
      {diffLoading ? <p className="text-xs text-gray-400">读取差异中…</p> : diffError ? <p role="alert" className="text-xs text-red-700">{diffError}</p> : diff === null ? null : diff ? <pre className="max-h-60 overflow-auto whitespace-pre-wrap rounded border bg-gray-50 p-2 text-xs">{diff}</pre> : <p className="text-xs text-gray-500">两个版本正文相同。</p>}
    </div>}
  </div>
}

export function VersionPanel({ chapterId }: { chapterId: string }) {
  return <VersionHistory key={chapterId} chapterId={chapterId} />
}
