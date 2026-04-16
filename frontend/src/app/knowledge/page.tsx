'use client'

import React, { useEffect, useState, useCallback } from 'react'
import { useKnowledgeStore } from '@/stores/knowledgeStore'
import type { BookSource, ReferenceBook, CrawlTask } from '@/stores/knowledgeStore'
import { apiFetch } from '@/lib/api'

type TabKey = 'sources' | 'books' | 'crawl' | 'explore'

const TABS: { key: TabKey; label: string }[] = [
  { key: 'sources', label: '书源管理' },
  { key: 'books', label: '参考书库' },
  { key: 'crawl', label: '抓取任务' },
  { key: 'explore', label: '排行榜' },
]

export default function KnowledgePage() {
  const [activeTab, setActiveTab] = useState<TabKey>('sources')

  return (
    <div>
      <div className="flex gap-1 border-b border-gray-200 mb-6">
        {TABS.map((tab) => (
          <button
            key={tab.key}
            onClick={() => setActiveTab(tab.key)}
            className={`px-4 py-2.5 text-sm font-medium border-b-2 transition-colors ${
              activeTab === tab.key
                ? 'border-blue-600 text-blue-600'
                : 'border-transparent text-gray-500 hover:text-gray-700 hover:border-gray-300'
            }`}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {activeTab === 'sources' && <SourcesTab />}
      {activeTab === 'books' && <BooksTab />}
      {activeTab === 'crawl' && <CrawlTab />}
      {activeTab === 'explore' && <ExploreTab />}
    </div>
  )
}

/* ─── Sources Tab ──────────────────────────────────────────── */

function SourcesTab() {
  const [sources, setSources] = useState<any[]>([])
  const [loading, setLoading] = useState(true)
  const [totalPages, setTotalPages] = useState(1)
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [search, setSearch] = useState('')
  const [groups, setGroups] = useState<string[]>([])
  const [selectedGroup, setSelectedGroup] = useState('')
  const [showImport, setShowImport] = useState(false)
  const [importJson, setImportJson] = useState('')
  const [importError, setImportError] = useState<string | null>(null)
  const [uploading, setUploading] = useState(false)
  const [testingId, setTestingId] = useState<string | null>(null)
  const [testResult, setTestResult] = useState<Record<string, string>>({})
  // Batch operations
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [batchTesting, setBatchTesting] = useState(false)
  const [batchTestResult, setBatchTestResult] = useState<any>(null)

  const fetchSources = useCallback(async (p = page, s = search, g = selectedGroup) => {
    setLoading(true)
    try {
      const params = new URLSearchParams({ page: String(p), page_size: '30' })
      if (s) params.set('search', s)
      if (g) params.set('group', g)
      const data = await apiFetch<any>(`/api/knowledge/sources?${params}`)
      setSources(data.sources || [])
      setTotal(data.total || 0)
      setTotalPages(data.total_pages || 1)
      if (data.groups) setGroups(data.groups)
    } catch { /* */ }
    finally { setLoading(false) }
  }, [page, search, selectedGroup])

  const deleteSource = async (id: string) => {
    await apiFetch(`/api/knowledge/sources/${id}`, { method: 'DELETE' })
    fetchSources()
  }

  const importSources = async (arr: any[]) => {
    await apiFetch('/api/knowledge/sources/import', {
      method: 'POST', body: JSON.stringify({ sources_json: arr })
    })
    fetchSources()
  }

  useEffect(() => { fetchSources() }, [page, selectedGroup]) // eslint-disable-line react-hooks/exhaustive-deps

  const handleImport = useCallback(async () => {
    setImportError(null)
    try {
      const parsed = JSON.parse(importJson)
      const arr = Array.isArray(parsed) ? parsed : [parsed]
      await importSources(arr)
      setShowImport(false)
      setImportJson('')
    } catch (err) {
      setImportError(err instanceof Error ? err.message : 'JSON 格式无效')
    }
  }, [importJson, importSources])

  const handleTest = useCallback(
    async (source: BookSource) => {
      setTestingId(source.id)
      try {
        const data = await apiFetch<{status: string}>(`/api/knowledge/sources/${source.id}/test`, { method: 'POST' })
        setTestResult((prev) => ({ ...prev, [source.id]: data.status === 'ok' ? '正常' : '失败' }))
      } catch {
        setTestResult((prev) => ({ ...prev, [source.id]: '失败' }))
      } finally {
        setTestingId(null)
      }
    },
    []
  )

  const toggleSelect = (id: string) => {
    setSelected(prev => {
      const next = new Set(prev)
      next.has(id) ? next.delete(id) : next.add(id)
      return next
    })
  }

  const toggleSelectAll = () => {
    if (selected.size === sources.length) {
      setSelected(new Set())
    } else {
      setSelected(new Set(sources.map((s: any) => s.id)))
    }
  }

  const handleBatchDelete = async () => {
    if (selected.size === 0) return
    if (!confirm(`确定删除 ${selected.size} 个书源？`)) return
    await apiFetch('/api/knowledge/sources/batch-delete', {
      method: 'POST', body: JSON.stringify({ source_ids: Array.from(selected) })
    })
    setSelected(new Set())
    fetchSources()
  }

  const handleBatchTest = async () => {
    const ids = selected.size > 0 ? Array.from(selected) : null
    setBatchTesting(true)
    setBatchTestResult(null)
    try {
      const data = await apiFetch<any>('/api/knowledge/sources/batch-test', {
        method: 'POST',
        body: JSON.stringify(ids ? { source_ids: ids } : {}),
      })
      setBatchTestResult(data)
      // Start polling for progress
      if (data.status === 'started') {
        const poll = setInterval(async () => {
          try {
            const progress = await apiFetch<any>('/api/knowledge/sources/test-progress')
            setBatchTestResult((prev: any) => ({ ...prev, ...progress }))
            if (progress.tested >= progress.total) {
              clearInterval(poll)
              setBatchTesting(false)
              fetchSources()
            }
          } catch { /* */ }
        }, 3000)
        // Safety: stop polling after 10 min
        setTimeout(() => { clearInterval(poll); setBatchTesting(false) }, 600000)
      } else {
        setBatchTesting(false)
      }
    } catch { setBatchTesting(false) }
  }

  const handleDeleteFailed = async () => {
    if (!confirm('确定删除全部测试失败的书源？此操作不可撤销。')) return
    const data = await apiFetch<any>('/api/knowledge/sources/delete-all-failed', { method: 'POST' })
    alert(`已删除 ${data.deleted} 个失败书源`)
    setSelected(new Set())
    setBatchTestResult(null)
    fetchSources()
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between flex-wrap gap-2">
        <h2 className="text-lg font-semibold text-gray-900">书源管理 <span className="text-sm font-normal text-gray-400">({total})</span></h2>
        <div className="flex gap-2 flex-wrap">
          <button onClick={handleBatchTest} disabled={batchTesting}
            className="px-3 py-1.5 text-sm bg-green-600 text-white rounded-lg hover:bg-green-700 disabled:opacity-50">
            {batchTesting ? '测试中...' : selected.size > 0 ? `测试选中(${selected.size})` : '全部测试'}
          </button>
          <button onClick={handleDeleteFailed}
            className="px-3 py-1.5 text-sm bg-red-600 text-white rounded-lg hover:bg-red-700">
            删除全部失败
          </button>
          {selected.size > 0 && (
            <button onClick={handleBatchDelete}
              className="px-3 py-1.5 text-sm bg-red-500 text-white rounded-lg hover:bg-red-600">
              删除选中({selected.size})
            </button>
          )}
          <button onClick={() => setShowImport(!showImport)}
            className="px-3 py-1.5 text-sm bg-blue-600 text-white rounded-lg hover:bg-blue-700">
            导入书源
          </button>
        </div>
      </div>

      {/* Batch test results */}
      {batchTestResult && (
        <div className="bg-white rounded-lg border border-gray-200 p-4">
          <div className="flex items-center justify-between mb-2">
            <h3 className="text-sm font-semibold text-gray-900">
              {batchTesting ? '测试中...' : '测试完成'}
              {' '}共 {batchTestResult.total ?? batchTestResult.tested ?? 0}，
              已测 {batchTestResult.tested ?? 0}，
              可用 <span className="text-green-600">{batchTestResult.ok ?? 0}</span>，
              失败 <span className="text-red-600">{batchTestResult.failed ?? 0}</span>
            </h3>
            <button onClick={() => setBatchTestResult(null)} className="text-xs text-gray-400 hover:text-gray-600">关闭</button>
          </div>
          {batchTesting && batchTestResult.total > 0 && (
            <div className="w-full bg-gray-100 rounded-full h-2 mb-2">
              <div className="h-2 rounded-full bg-blue-500 transition-all"
                style={{ width: `${Math.round(((batchTestResult.tested ?? 0) / batchTestResult.total) * 100)}%` }} />
            </div>
          )}
          {batchTestResult.message && <p className="text-xs text-gray-500 mb-1">{batchTestResult.message}</p>}
        </div>
      )}

      {/* Search + Group filter */}
      <div className="flex gap-2">
        <input
          value={search}
          onChange={e => setSearch(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter') { setPage(1); fetchSources(1, search, selectedGroup) } }}
          placeholder="搜索书源..."
          className="flex-1 px-3 py-1.5 text-sm border border-gray-200 rounded-lg"
        />
        <button onClick={() => { setPage(1); fetchSources(1, search, selectedGroup) }}
          className="px-3 py-1.5 text-sm bg-gray-100 text-gray-700 rounded-lg">搜索</button>
        {groups.length > 0 && (
          <select value={selectedGroup} onChange={e => { setSelectedGroup(e.target.value); setPage(1) }}
            className="px-2 py-1.5 text-sm border border-gray-200 rounded-lg max-w-[120px]">
            <option value="">全部分组</option>
            {groups.slice(0, 30).map(g => <option key={g} value={g}>{g}</option>)}
          </select>
        )}
      </div>

      {showImport && (
        <div className="bg-white rounded-lg border border-gray-200 p-4 space-y-3">
          {/* File upload for large JSON */}
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-2">
              上传书源文件（支持大文件）
            </label>
            <input
              type="file"
              accept=".json"
              onChange={async (e) => {
                const file = e.target.files?.[0]
                if (!file) return
                setUploading(true)
                setImportError('')
                try {
                  const formData = new FormData()
                  formData.append('file', file)
                  const res = await fetch('/api/knowledge/sources/upload', {
                    method: 'POST',
                    headers: { 'Authorization': `Bearer ${localStorage.getItem('auth_token') || ''}` },
                    body: formData,
                  })
                  const data = await res.json()
                  if (!res.ok) throw new Error(data.detail || '上传失败')
                  alert(`导入成功：${data.imported} 个书源，跳过 ${data.skipped} 个重复`)
                  setShowImport(false)
                  fetchSources()
                } catch (err) {
                  setImportError(err instanceof Error ? err.message : '上传失败')
                } finally {
                  setUploading(false)
                }
              }}
              className="w-full text-sm text-gray-500 file:mr-3 file:py-2 file:px-4 file:rounded-lg file:border-0 file:text-sm file:font-medium file:bg-blue-50 file:text-blue-600 hover:file:bg-blue-100"
            />
          </div>

          <div className="text-xs text-gray-400 text-center">— 或者粘贴 JSON —</div>

          <textarea
            value={importJson}
            onChange={(e) => setImportJson(e.target.value)}
            placeholder='[{"bookSourceName": "书源名", "bookSourceUrl": "https://..."}]'
            className="w-full h-24 px-3 py-2 text-sm border border-gray-200 rounded-lg resize-none font-mono focus:ring-2 focus:ring-blue-500 focus:border-transparent"
          />
          {importError && (
            <p className="text-sm text-red-600">{importError}</p>
          )}
          <div className="flex gap-2">
            <button
              onClick={handleImport}
              disabled={!importJson.trim() || loading}
              className="px-4 py-2 text-sm bg-green-600 text-white rounded-lg hover:bg-green-700 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {loading ? '导入中...' : '确认导入'}
            </button>
            <button
              onClick={() => {
                setShowImport(false)
                setImportJson('')
                setImportError(null)
              }}
              className="px-4 py-2 text-sm bg-gray-200 text-gray-700 rounded-lg hover:bg-gray-300"
            >
              取消
            </button>
          </div>
        </div>
      )}


      {loading && sources.length === 0 ? (
        <LoadingState />
      ) : sources.length === 0 ? (
        <EmptyState message="暂无书源，点击导入添加" />
      ) : (
        <div className="bg-white rounded-lg border border-gray-200 overflow-hidden">
          <table className="w-full text-sm">
            <thead>
              <tr className="bg-gray-50 border-b border-gray-200">
                <th className="px-3 py-3 w-8">
                  <input type="checkbox" checked={selected.size === sources.length && sources.length > 0}
                    onChange={toggleSelectAll} className="rounded border-gray-300" />
                </th>
                <th className="text-left px-3 py-3 font-medium text-gray-600">名称</th>
                <th className="text-center px-3 py-3 font-medium text-gray-600">评分</th>
                <th className="text-left px-3 py-3 font-medium text-gray-600">状态</th>
                <th className="text-right px-3 py-3 font-medium text-gray-600">操作</th>
              </tr>
            </thead>
            <tbody>
              {sources.map((source) => {
                const s = source as any
                const score = s.score ?? 5
                const scoreColor = score >= 7 ? 'text-green-600' : score >= 4 ? 'text-yellow-600' : 'text-red-600'
                const enabled = s.enabled === 1
                return (
                <tr key={source.id} className={`border-b border-gray-100 last:border-b-0 ${!enabled ? 'opacity-50' : ''}`}>
                  <td className="px-3 py-3">
                    <input type="checkbox" checked={selected.has(source.id)}
                      onChange={() => toggleSelect(source.id)} className="rounded border-gray-300" />
                  </td>
                  <td className="px-3 py-3">
                    <div className="font-medium text-gray-900">{source.name}</div>
                    <div className="text-xs text-gray-400 truncate max-w-[200px]">{source.source_url}</div>
                    {source.source_group && <div className="text-[10px] text-gray-400">{source.source_group}</div>}
                  </td>
                  <td className="px-3 py-3 text-center">
                    <span className={`text-sm font-bold ${scoreColor}`}>{score.toFixed(1)}</span>
                    <div className="text-[10px] text-gray-400">
                      {s.success_count ?? s.successCount ?? 0}成功 / {s.fail_count ?? s.failCount ?? 0}失败
                    </div>
                    {(s.consecutive_fails ?? s.consecutiveFails ?? 0) >= 3 && (
                      <div className="text-[10px] text-red-500">连续失败{s.consecutive_fails ?? s.consecutiveFails}次</div>
                    )}
                  </td>
                  <td className="px-3 py-3">
                    <StatusBadge
                      ok={enabled && source.last_test_ok === 1}
                      label={!enabled ? '已停用' : testResult[source.id] || (source.last_test_ok === 1 ? '正常' : '未测试')}
                    />
                  </td>
                  <td className="px-3 py-3 text-right">
                    <div className="flex flex-wrap gap-1 justify-end">
                      <button onClick={() => handleTest(source)} disabled={testingId === source.id}
                        className="px-2 py-1 text-xs bg-gray-100 text-gray-700 rounded hover:bg-gray-200 disabled:opacity-50">
                        {testingId === source.id ? '...' : '测试'}
                      </button>
                      <button onClick={async () => {
                        await apiFetch(`/api/knowledge/sources/${source.id}/toggle`, { method: 'POST' })
                        fetchSources()
                      }} className={`px-2 py-1 text-xs rounded ${enabled ? 'bg-yellow-50 text-yellow-600' : 'bg-green-50 text-green-600'}`}>
                        {enabled ? '停用' : '启用'}
                      </button>
                      <button onClick={() => deleteSource(source.id)}
                        className="px-2 py-1 text-xs bg-red-50 text-red-600 rounded hover:bg-red-100">
                        删除
                      </button>
                    </div>
                  </td>
                </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}

      {/* Pagination */}
      {totalPages > 1 && (
        <div className="flex items-center justify-center gap-2">
          <button onClick={() => setPage(p => Math.max(1, p - 1))} disabled={page <= 1}
            className="px-3 py-1.5 text-xs bg-gray-200 rounded disabled:opacity-30">上一页</button>
          <span className="text-xs text-gray-500">{page} / {totalPages}</span>
          <button onClick={() => setPage(p => Math.min(totalPages, p + 1))} disabled={page >= totalPages}
            className="px-3 py-1.5 text-xs bg-gray-200 rounded disabled:opacity-30">下一页</button>
        </div>
      )}
    </div>
  )
}

/* ─── Books Tab ────────────────────────────────────────────── */

function BooksTab() {
  const { books, loading, error, fetchBooks, deleteBook, scoreBook } =
    useKnowledgeStore()
  const [showUpload, setShowUpload] = useState(false)
  const [uploadFile, setUploadFile] = useState<File | null>(null)
  const [uploadTitle, setUploadTitle] = useState('')
  const [uploadAuthor, setUploadAuthor] = useState('')
  const [uploading, setUploading] = useState(false)
  const [uploadError, setUploadError] = useState<string | null>(null)
  const [scoringId, setScoringId] = useState<string | null>(null)
  const [detailBookId, setDetailBookId] = useState<string | null>(null)

  useEffect(() => {
    fetchBooks()
  }, [fetchBooks])

  // Auto-refresh when books are processing
  const hasProcessing = books.some(b => ['pending', 'cleaning', 'extracting', 'crawling'].includes(b.status))
  useEffect(() => {
    if (!hasProcessing) return
    const interval = setInterval(fetchBooks, 3000)
    return () => clearInterval(interval)
  }, [hasProcessing, fetchBooks])

  const handleUpload = useCallback(async () => {
    if (!uploadFile) return
    setUploading(true)
    setUploadError(null)
    try {
      const formData = new FormData()
      formData.append('file', uploadFile)
      formData.append('title', uploadTitle)
      formData.append('author', uploadAuthor)
      const token = localStorage.getItem('auth_token') || ''
      const res = await fetch('/api/knowledge/upload', {
        method: 'POST',
        headers: { 'Authorization': `Bearer ${token}` },
        body: formData,
      })
      if (!res.ok) {
        const errData = await res.json().catch(() => ({ detail: res.statusText }))
        throw new Error(errData.detail || '上传失败')
      }
      setShowUpload(false)
      setUploadFile(null)
      setUploadTitle('')
      setUploadAuthor('')
      fetchBooks()
    } catch (err) {
      setUploadError(err instanceof Error ? err.message : '上传失败')
    } finally {
      setUploading(false)
    }
  }, [uploadFile, uploadTitle, uploadAuthor, fetchBooks])

  const handleScore = useCallback(
    async (id: string) => {
      setScoringId(id)
      try {
        await scoreBook(id)
      } finally {
        setScoringId(null)
      }
    },
    [scoreBook]
  )

  const detailBook = detailBookId ? books.find((b) => b.id === detailBookId) : null

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold text-gray-900">参考书库</h2>
        <button
          onClick={() => setShowUpload(!showUpload)}
          className="px-4 py-2 text-sm bg-blue-600 text-white rounded-lg hover:bg-blue-700"
        >
          上传书籍
        </button>
      </div>

      {showUpload && (
        <div className="bg-white rounded-lg border border-gray-200 p-4 space-y-3">
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">文件 *</label>
            <input
              type="file"
              accept=".txt,.epub,.pdf,.azw3"
              onChange={(e) => setUploadFile(e.target.files?.[0] ?? null)}
              className="w-full text-sm text-gray-500 file:mr-3 file:py-2 file:px-4 file:rounded-lg file:border-0 file:text-sm file:font-medium file:bg-blue-50 file:text-blue-700 hover:file:bg-blue-100"
            />
            <p className="text-[10px] text-gray-400 mt-1">支持 TXT / EPUB / AZW3 / PDF，书名和作者会自动从文件中提取</p>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">书名（选填）</label>
              <input type="text" value={uploadTitle} onChange={(e) => setUploadTitle(e.target.value)}
                className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg" placeholder="留空则从文件提取" />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">作者（选填）</label>
              <input type="text" value={uploadAuthor} onChange={(e) => setUploadAuthor(e.target.value)}
                className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg" placeholder="留空则从文件提取" />
            </div>
          </div>
          {uploadError && (
            <p className="text-sm text-red-600">{uploadError}</p>
          )}
          <div className="flex gap-2">
            <button
              onClick={handleUpload}
              disabled={!uploadFile || uploading}
              className="px-4 py-2 text-sm bg-green-600 text-white rounded-lg hover:bg-green-700 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {uploading ? '上传中...' : '上传'}
            </button>
            <button
              onClick={() => {
                setShowUpload(false)
                setUploadFile(null)
                setUploadTitle('')
                setUploadAuthor('')
                setUploadError(null)
              }}
              className="px-4 py-2 text-sm bg-gray-200 text-gray-700 rounded-lg hover:bg-gray-300"
            >
              取消
            </button>
          </div>
        </div>
      )}

      {error && (
        <div className="bg-red-50 border border-red-200 rounded-lg p-3 text-sm text-red-700">
          {error}
        </div>
      )}

      {/* Detail modal */}
      {detailBook && (
        <div className="bg-white rounded-lg border border-gray-200 p-4 space-y-2">
          <div className="flex items-center justify-between">
            <h3 className="font-semibold text-gray-900">{detailBook.title}</h3>
            <button
              onClick={() => setDetailBookId(null)}
              className="text-gray-400 hover:text-gray-600 text-lg leading-none"
            >
              x
            </button>
          </div>
          <div className="grid grid-cols-2 gap-3 text-sm">
            <div>
              <span className="text-gray-500">作者:</span>{' '}
              <span className="text-gray-900">{detailBook.author || '未知'}</span>
            </div>
            <div>
              <span className="text-gray-500">来源:</span>{' '}
              <span className="text-gray-900">{detailBook.source}</span>
            </div>
            <div>
              <span className="text-gray-500">章节数:</span>{' '}
              <span className="text-gray-900">{detailBook.totalChapters}</span>
            </div>
            <div>
              <span className="text-gray-500">字数:</span>{' '}
              <span className="text-gray-900">{(detailBook.totalWords || 0).toLocaleString()}</span>
            </div>
            <div>
              <span className="text-gray-500">状态:</span>{' '}
              <span className="text-gray-900">{detailBook.status}</span>
            </div>
          </div>
          {Object.keys(detailBook.metadataJson).length > 0 && (
            <div>
              <span className="text-sm text-gray-500">元数据:</span>
              <pre className="mt-1 text-xs bg-gray-50 rounded p-2 overflow-x-auto">
                {JSON.stringify(detailBook.metadataJson, null, 2)}
              </pre>
            </div>
          )}
        </div>
      )}

      {loading && books.length === 0 ? (
        <LoadingState />
      ) : books.length === 0 ? (
        <EmptyState message="暂无参考书，点击上传添加" />
      ) : (
        <div className="bg-white rounded-lg border border-gray-200 overflow-hidden">
          <table className="w-full text-sm">
            <thead>
              <tr className="bg-gray-50 border-b border-gray-200">
                <th className="text-left px-4 py-3 font-medium text-gray-600">书名</th>
                <th className="text-left px-4 py-3 font-medium text-gray-600">作者</th>
                <th className="text-left px-4 py-3 font-medium text-gray-600">来源</th>
                <th className="text-right px-4 py-3 font-medium text-gray-600">字数</th>
                <th className="text-left px-4 py-3 font-medium text-gray-600">状态</th>
                <th className="text-right px-4 py-3 font-medium text-gray-600">质量</th>
                <th className="text-right px-4 py-3 font-medium text-gray-600">操作</th>
              </tr>
            </thead>
            <tbody>
              {books.map((book) => (
                <tr key={book.id} className="border-b border-gray-100 last:border-b-0">
                  <td className="px-4 py-3 font-medium text-gray-900">{book.title}</td>
                  <td className="px-4 py-3 text-gray-500">{book.author || '-'}</td>
                  <td className="px-4 py-3 text-gray-500">{book.source}</td>
                  <td className="px-4 py-3 text-right text-gray-500">
                    {(book.totalWords || 0).toLocaleString()}
                  </td>
                  <td className="px-4 py-3">
                    <StatusBadge
                      ok={book.status === 'ready' || book.status === 'completed'}
                      label={{
                        pending: '等待处理',
                        cleaning: '解析中...',
                        extracting: '评分中...',
                        crawling: '抓取中...',
                        ready: '已就绪',
                        completed: '已完成',
                        error: '失败',
                        low_quality: '质量低',
                      }[book.status] || book.status}
                    />
                  </td>
                  <td className="px-4 py-3 text-right text-gray-700">
                    {(book.metadataJson as any)?.quality_score?.overall != null
                      ? `${Number((book.metadataJson as any).quality_score.overall).toFixed(1)}`
                      : '-'}
                  </td>
                  <td className="px-4 py-3 text-right space-x-2">
                    <button
                      onClick={() => setDetailBookId(book.id)}
                      className="px-3 py-1 text-xs bg-gray-100 text-gray-700 rounded hover:bg-gray-200"
                    >
                      详情
                    </button>
                    <button
                      onClick={() => handleScore(book.id)}
                      disabled={scoringId === book.id}
                      className="px-3 py-1 text-xs bg-blue-50 text-blue-600 rounded hover:bg-blue-100 disabled:opacity-50"
                    >
                      {scoringId === book.id ? '评分中...' : '评分'}
                    </button>
                    <button
                      onClick={async () => {
                        try {
                          const data = await apiFetch<any>(`/api/styles/detect-from-book/${book.id}`, { method: 'POST' })
                          alert(`已从《${book.title}》提取写法：${data.name}`)
                        } catch (e) {
                          alert(e instanceof Error ? e.message : '提取失败')
                        }
                      }}
                      className="px-3 py-1 text-xs bg-purple-50 text-purple-600 rounded hover:bg-purple-100"
                    >
                      提取风格
                    </button>
                    <button
                      onClick={() => deleteBook(book.id)}
                      className="px-3 py-1 text-xs bg-red-50 text-red-600 rounded hover:bg-red-100"
                    >
                      删除
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

/* ─── Crawl Tasks Tab ──────────────────────────────────────── */

function CrawlTab() {
  const { crawlTasks, loading, error, fetchCrawlTasks } = useKnowledgeStore()
  // Smart search
  const [searchKeyword, setSearchKeyword] = useState('')
  const [searching, setSearching] = useState(false)
  const [searchResults, setSearchResults] = useState<any[]>([])
  const [searchMsg, setSearchMsg] = useState('')
  const [crawling, setCrawling] = useState<string | null>(null)

  useEffect(() => {
    fetchCrawlTasks()
    const interval = setInterval(fetchCrawlTasks, 10000)
    return () => clearInterval(interval)
  }, [fetchCrawlTasks])

  const handleSmartSearch = async () => {
    if (!searchKeyword.trim() || searching) return
    setSearching(true)
    setSearchResults([])
    setSearchMsg('')
    try {
      const data = await apiFetch<any>('/api/knowledge/crawl-tasks/smart', {
        method: 'POST',
        body: JSON.stringify({ keyword: searchKeyword }),
      })
      setSearchResults(data.books || [])
      setSearchMsg(data.message || '')
    } catch (e) {
      setSearchMsg(e instanceof Error ? e.message : '搜索失败')
    } finally {
      setSearching(false)
    }
  }

  const handleStartCrawl = async (book: any) => {
    setCrawling(book.book_url)
    try {
      await apiFetch('/api/knowledge/crawl-tasks', {
        method: 'POST',
        body: JSON.stringify({
          title: book.title,
          author: book.author || '',
          book_url: book.book_url,
          source_id: book.source_id,
        }),
      })
      fetchCrawlTasks()
      // Remove from search results
      setSearchResults(prev => prev.filter(b => b.book_url !== book.book_url))
    } catch { /* */ }
    finally { setCrawling(null) }
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold text-gray-900">抓取任务</h2>
        <button onClick={fetchCrawlTasks} disabled={loading}
          className="px-3 py-1.5 text-sm bg-gray-200 text-gray-700 rounded-lg hover:bg-gray-300 disabled:opacity-50">
          {loading ? '刷新中...' : '刷新'}
        </button>
      </div>

      {/* Smart search: enter book name → auto search across sources → pick → crawl */}
      <div className="bg-white rounded-lg border border-gray-200 p-4 space-y-3">
        <h3 className="text-sm font-semibold text-gray-900">输入书名搜索并抓取</h3>
        <div className="flex gap-2">
          <input value={searchKeyword} onChange={e => setSearchKeyword(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter') handleSmartSearch() }}
            placeholder="输入书名，自动在书源中搜索..."
            className="flex-1 px-3 py-2 text-sm border border-gray-200 rounded-lg" />
          <button onClick={handleSmartSearch} disabled={searching || !searchKeyword.trim()}
            className="px-4 py-2 text-sm bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 shrink-0">
            {searching ? '搜索中...' : '搜索'}
          </button>
        </div>
        <p className="text-[10px] text-gray-400">将自动搜索评分最高的可用书源，找到后点击"抓取"即可开始下载</p>

        {searchMsg && <p className="text-xs text-gray-500">{searchMsg}</p>}

        {searchResults.length > 0 && (
          <div className="space-y-2 max-h-64 overflow-y-auto">
            {searchResults.map((book: any, idx: number) => (
              <div key={idx} className="flex items-center justify-between p-2.5 bg-gray-50 rounded-lg">
                <div className="flex-1 min-w-0">
                  <div className="font-medium text-sm text-gray-900 truncate">{book.title}</div>
                  <div className="text-xs text-gray-500">
                    {book.author && `${book.author} · `}{book.source_name}
                    {book.kind && ` · ${book.kind}`}
                  </div>
                  {book.intro && <div className="text-[10px] text-gray-400 truncate mt-0.5">{book.intro}</div>}
                </div>
                <button onClick={() => handleStartCrawl(book)}
                  disabled={crawling === book.book_url}
                  className="px-3 py-1.5 text-xs bg-green-600 text-white rounded hover:bg-green-700 disabled:opacity-50 shrink-0 ml-2">
                  {crawling === book.book_url ? '...' : '抓取'}
                </button>
              </div>
            ))}
          </div>
        )}
      </div>

      {error && (
        <div className="bg-red-50 border border-red-200 rounded-lg p-3 text-sm text-red-700">
          {error}
        </div>
      )}

      {/* Task list */}
      {crawlTasks.length > 0 && (
        <div className="space-y-3">
          <h3 className="text-sm font-semibold text-gray-700">进行中的任务</h3>
          {crawlTasks.map((task) => (
            <CrawlTaskCard key={task.id} task={task} />
          ))}
        </div>
      )}
    </div>
  )
}

function CrawlTaskCard({ task }: { task: CrawlTask }) {
  const progress =
    task.totalChapters > 0
      ? Math.round((task.completedChapters / task.totalChapters) * 100)
      : 0

  return (
    <div className="bg-white rounded-lg border border-gray-200 p-4 space-y-3">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-sm font-medium text-gray-900 truncate max-w-md">
            {task.bookUrl}
          </h3>
          <p className="text-xs text-gray-500 mt-0.5">任务 ID: {task.id}</p>
        </div>
        <StatusBadge
          ok={task.status === 'completed'}
          label={task.status}
        />
      </div>

      <div>
        <div className="flex items-center justify-between text-xs text-gray-500 mb-1">
          <span>
            {task.completedChapters} / {task.totalChapters} 章
          </span>
          <span>{progress}%</span>
        </div>
        <div className="w-full bg-gray-100 rounded-full h-2">
          <div
            className={`h-2 rounded-full transition-all ${
              task.status === 'completed'
                ? 'bg-green-500'
                : task.status === 'failed'
                  ? 'bg-red-500'
                  : 'bg-blue-500'
            }`}
            style={{ width: `${progress}%` }}
          />
        </div>
      </div>
    </div>
  )
}

/* ─── Explore Tab ──────────────────────────────────────────── */


function ExploreTab() {
  const [mode, setMode] = useState<'ranking' | 'search'>('ranking')
  const [rankingSource, setRankingSource] = useState('quark_male_hot')
  const [rankingCategory, setRankingCategory] = useState('全部')
  const [books, setBooks] = useState<any[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [searchKeyword, setSearchKeyword] = useState('')
  const [searchResults, setSearchResults] = useState<any[]>([])
  const [searching, setSearching] = useState(false)

  const handleSearch = async () => {
    if (!searchKeyword.trim()) return
    setSearching(true)
    setError('')
    setMode('search')
    try {
      const data = await apiFetch<any>('/api/knowledge/search', {
        method: 'POST',
        body: JSON.stringify({ keyword: searchKeyword }),
      })
      setSearchResults(data.books || [])
    } catch (e) { setError(e instanceof Error ? e.message : '搜索失败') }
    finally { setSearching(false) }
  }

  const RANKING_SOURCES = [
    { key: 'quark_male_hot', name: '夸克热搜·男频' },
    { key: 'quark_female_hot', name: '夸克热搜·女频' },
    { key: 'quark_male_good', name: '夸克好评·男频' },
    { key: 'quark_female_good', name: '夸克好评·女频' },
  ]
  const CATEGORIES = ['全部', '都市', '玄幻', '仙侠', '历史', '科幻', '灵异悬疑', '军事']

  const fetchRanking = async (src = rankingSource, cat = rankingCategory) => {
    setLoading(true)
    setError('')
    setRankingSource(src)
    setRankingCategory(cat)
    try {
      const data = await apiFetch<any>('/api/knowledge/rankings/fetch', {
        method: 'POST',
        body: JSON.stringify({ source_key: src, category: cat }),
      })
      setBooks(data.books || [])
      if (data.error) setError(data.error)
    } catch (e) { setError(e instanceof Error ? e.message : '获取失败') }
    finally { setLoading(false) }
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold text-gray-900">排行榜 & 搜索</h2>
      </div>

      {/* Search bar */}
      <div className="flex gap-2">
        <input value={searchKeyword} onChange={e => setSearchKeyword(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter') handleSearch() }}
          placeholder="搜索小说名称或作者..."
          className="flex-1 px-3 py-2 text-sm border border-gray-200 rounded-lg" />
        <button onClick={handleSearch} disabled={searching || !searchKeyword.trim()}
          className="px-4 py-2 text-sm bg-blue-600 text-white rounded-lg disabled:opacity-50 shrink-0">
          {searching ? '搜索中...' : '搜索'}
        </button>
      </div>
      <p className="text-[10px] text-gray-400">搜索依赖书源质量，部分书源可能因反爬/JS渲染而无法使用。排行榜数据来自夸克，稳定可用。</p>

      {/* Mode tabs */}
      <div className="flex gap-2">
        <button onClick={() => setMode('ranking')}
          className={`px-3 py-1.5 text-xs rounded-full ${mode === 'ranking' ? 'bg-gray-900 text-white' : 'bg-gray-200 text-gray-600'}`}>
          排行榜
        </button>
        <button onClick={() => { if (searchResults.length) setMode('search') }}
          className={`px-3 py-1.5 text-xs rounded-full ${mode === 'search' ? 'bg-gray-900 text-white' : 'bg-gray-200 text-gray-600'}`}>
          搜索结果 {searchResults.length > 0 ? `(${searchResults.length})` : ''}
        </button>
      </div>

      {/* Search results */}
      {mode === 'search' && (
        <div className="space-y-2">
          {searchResults.length === 0 ? (
            <p className="text-sm text-gray-400 text-center py-4">暂无搜索结果</p>
          ) : searchResults.map((book: any, idx: number) => (
            <div key={idx} className="bg-white rounded-lg border border-gray-200 p-3">
              <div className="flex justify-between items-start">
                <div className="flex-1 min-w-0">
                  <h3 className="font-medium text-gray-900 text-sm truncate">{book.title}</h3>
                  <div className="text-xs text-gray-500">{book.author} <span className="text-gray-300">|</span> {book.source_name}</div>
                </div>
                {book.kind && <span className="text-[10px] px-2 py-0.5 bg-blue-50 text-blue-600 rounded shrink-0 ml-2">{book.kind}</span>}
              </div>
              {book.intro && <p className="text-xs text-gray-500 mt-1 line-clamp-2">{book.intro}</p>}
            </div>
          ))}
        </div>
      )}

      {/* Ranking section (only when in ranking mode) */}
      {mode === 'ranking' && <>

      {/* Ranking source selector */}
      <div className="flex gap-1.5 overflow-x-auto">
        {RANKING_SOURCES.map(s => (
          <button key={s.key} onClick={() => fetchRanking(s.key, rankingCategory)}
            className={`px-3 py-1.5 text-xs rounded-full whitespace-nowrap ${
              rankingSource === s.key ? 'bg-blue-600 text-white' : 'bg-gray-200 text-gray-600'
            }`}>{s.name}</button>
        ))}
      </div>

      {/* Category filter */}
      <div className="flex gap-1 overflow-x-auto">
        {CATEGORIES.map(cat => (
          <button key={cat} onClick={() => fetchRanking(rankingSource, cat)}
            className={`px-2.5 py-1 text-xs rounded whitespace-nowrap ${
              rankingCategory === cat ? 'bg-blue-100 text-blue-700' : 'bg-gray-100 text-gray-600'
            }`}>{cat}</button>
        ))}
      </div>

      {error && <p className="text-xs text-red-500">{error}</p>}

      {loading ? (
        <p className="text-sm text-gray-400 text-center py-8">加载排行榜...</p>
      ) : books.length > 0 ? (
        <div className="space-y-2">
          {books.map((book: any, idx: number) => (
            <div key={idx} className="bg-white rounded-lg border border-gray-200 p-3">
              <div className="flex justify-between items-start">
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="text-xs text-gray-400 font-mono w-5">{idx + 1}</span>
                    <h3 className="font-medium text-gray-900 text-sm truncate">{book.title}</h3>
                  </div>
                  <div className="flex items-center gap-2 mt-0.5 ml-7">
                    <span className="text-xs text-gray-500">{book.author}</span>
                    {book.word_count && <span className="text-[10px] text-gray-400">{book.word_count}</span>}
                  </div>
                </div>
                {book.kind && <span className="text-[10px] px-2 py-0.5 bg-blue-50 text-blue-600 rounded shrink-0">{book.kind}</span>}
              </div>
              {book.intro && <p className="text-xs text-gray-500 mt-1.5 ml-7 line-clamp-2">{book.intro}</p>}
            </div>
          ))}
        </div>
      ) : rankingSource ? (
        <div className="bg-white rounded-lg border border-gray-200 p-8 text-center">
          <p className="text-sm text-gray-500">点击上方排行榜分类加载数据</p>
        </div>
      ) : null}

      </>}
    </div>
  )
}

/* ─── Shared Small Components ──────────────────────────────── */

function StatusBadge({ ok, label }: { ok: boolean; label: string }) {
  return (
    <span
      className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-medium ${
        ok
          ? 'bg-green-50 text-green-700'
          : label === 'failed' || label === 'unreachable'
            ? 'bg-red-50 text-red-700'
            : 'bg-gray-100 text-gray-600'
      }`}
    >
      {label}
    </span>
  )
}

function LoadingState() {
  return (
    <div className="bg-white rounded-lg border border-gray-200 p-12 text-center">
      <p className="text-sm text-gray-500">加载中...</p>
    </div>
  )
}

function EmptyState({ message }: { message: string }) {
  return (
    <div className="bg-white rounded-lg border border-gray-200 p-12 text-center">
      <p className="text-sm text-gray-500">{message}</p>
    </div>
  )
}
