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
  const { sources, loading, error, fetchSources, importSources, deleteSource } =
    useKnowledgeStore()
  const [showImport, setShowImport] = useState(false)
  const [importJson, setImportJson] = useState('')
  const [importError, setImportError] = useState<string | null>(null)
  const [uploading, setUploading] = useState(false)
  const [testingId, setTestingId] = useState<string | null>(null)
  const [testResult, setTestResult] = useState<Record<string, string>>({})

  useEffect(() => {
    fetchSources()
  }, [fetchSources])

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
        const res = await fetch(source.sourceUrl, { method: 'HEAD', mode: 'no-cors' })
        setTestResult((prev) => ({ ...prev, [source.id]: 'reachable' }))
      } catch {
        setTestResult((prev) => ({ ...prev, [source.id]: 'unreachable' }))
      } finally {
        setTestingId(null)
      }
    },
    []
  )

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold text-gray-900">书源管理</h2>
        <button
          onClick={() => setShowImport(!showImport)}
          className="px-4 py-2 text-sm bg-blue-600 text-white rounded-lg hover:bg-blue-700"
        >
          导入书源
        </button>
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

      {error && (
        <div className="bg-red-50 border border-red-200 rounded-lg p-3 text-sm text-red-700">
          {error}
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
                <th className="text-left px-4 py-3 font-medium text-gray-600">名称</th>
                <th className="text-left px-4 py-3 font-medium text-gray-600">地址</th>
                <th className="text-left px-4 py-3 font-medium text-gray-600">分组</th>
                <th className="text-left px-4 py-3 font-medium text-gray-600">状态</th>
                <th className="text-right px-4 py-3 font-medium text-gray-600">操作</th>
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
                  <td className="px-4 py-3">
                    <div className="font-medium text-gray-900">{source.name}</div>
                    <div className="text-xs text-gray-400 truncate max-w-[200px]">{source.sourceUrl}</div>
                    {source.sourceGroup && <div className="text-[10px] text-gray-400">{source.sourceGroup}</div>}
                  </td>
                  <td className="px-4 py-3 text-center">
                    <span className={`text-sm font-bold ${scoreColor}`}>{score.toFixed(1)}</span>
                    <div className="text-[10px] text-gray-400">
                      {s.success_count ?? s.successCount ?? 0}成功 / {s.fail_count ?? s.failCount ?? 0}失败
                    </div>
                    {(s.consecutive_fails ?? s.consecutiveFails ?? 0) >= 3 && (
                      <div className="text-[10px] text-red-500">连续失败{s.consecutive_fails ?? s.consecutiveFails}次</div>
                    )}
                  </td>
                  <td className="px-4 py-3">
                    <StatusBadge
                      ok={enabled && source.lastTestOk === 1}
                      label={!enabled ? '已停用' : testResult[source.id] || (source.lastTestOk === 1 ? '正常' : '未测试')}
                    />
                  </td>
                  <td className="px-4 py-3 text-right">
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

  const handleUpload = useCallback(async () => {
    if (!uploadFile || !uploadTitle.trim()) return
    setUploading(true)
    setUploadError(null)
    try {
      const formData = new FormData()
      formData.append('file', uploadFile)
      formData.append('title', uploadTitle)
      formData.append('author', uploadAuthor)
      const API_BASE = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'
      const res = await fetch(`${API_BASE}/api/knowledge/upload`, {
        method: 'POST',
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
            <label className="block text-sm font-medium text-gray-700 mb-1">书名 *</label>
            <input
              type="text"
              value={uploadTitle}
              onChange={(e) => setUploadTitle(e.target.value)}
              className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent"
              placeholder="书名"
            />
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">作者</label>
            <input
              type="text"
              value={uploadAuthor}
              onChange={(e) => setUploadAuthor(e.target.value)}
              className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent"
              placeholder="作者"
            />
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">文件 *</label>
            <input
              type="file"
              accept=".txt,.epub,.pdf"
              onChange={(e) => setUploadFile(e.target.files?.[0] ?? null)}
              className="w-full text-sm text-gray-500 file:mr-3 file:py-2 file:px-4 file:rounded-lg file:border-0 file:text-sm file:font-medium file:bg-blue-50 file:text-blue-700 hover:file:bg-blue-100"
            />
          </div>
          {uploadError && (
            <p className="text-sm text-red-600">{uploadError}</p>
          )}
          <div className="flex gap-2">
            <button
              onClick={handleUpload}
              disabled={!uploadFile || !uploadTitle.trim() || uploading}
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
              <span className="text-gray-900">{detailBook.totalWords.toLocaleString()}</span>
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
                    {book.totalWords.toLocaleString()}
                  </td>
                  <td className="px-4 py-3">
                    <StatusBadge
                      ok={book.status === 'completed'}
                      label={book.status}
                    />
                  </td>
                  <td className="px-4 py-3 text-right text-gray-700">
                    {typeof book.metadataJson?.quality_score === 'number'
                      ? `${(book.metadataJson.quality_score as number).toFixed(1)}`
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

  useEffect(() => {
    fetchCrawlTasks()
    const interval = setInterval(fetchCrawlTasks, 10000)
    return () => clearInterval(interval)
  }, [fetchCrawlTasks])

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold text-gray-900">抓取任务</h2>
        <button
          onClick={fetchCrawlTasks}
          disabled={loading}
          className="px-4 py-2 text-sm bg-gray-200 text-gray-700 rounded-lg hover:bg-gray-300 disabled:opacity-50"
        >
          {loading ? '刷新中...' : '刷新'}
        </button>
      </div>

      {error && (
        <div className="bg-red-50 border border-red-200 rounded-lg p-3 text-sm text-red-700">
          {error}
        </div>
      )}

      {loading && crawlTasks.length === 0 ? (
        <LoadingState />
      ) : crawlTasks.length === 0 ? (
        <EmptyState message="暂无抓取任务" />
      ) : (
        <div className="space-y-3">
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
  const [sources, setSources] = useState<any[]>([])
  const [selectedSource, setSelectedSource] = useState('')
  const [books, setBooks] = useState<any[]>([])
  const [loading, setLoading] = useState(false)
  const [page, setPage] = useState(1)
  const [searchKeyword, setSearchKeyword] = useState('')

  useEffect(() => {
    // Get top-scored sources for explore
    apiFetch<any>('/api/knowledge/sources/ranking')
      .then(data => {
        const list = Array.isArray(data) ? data : (data.sources || [])
        // Show top 50 enabled sources
        setSources(list.filter((s: any) => s.enabled).slice(0, 50))
      })
      .catch(() => {
        // Fallback to regular list
        apiFetch<any>('/api/knowledge/sources')
          .then(data => {
            const list = Array.isArray(data) ? data : (data.sources || [])
            setSources(Array.isArray(list) ? list.filter((s: any) => s.enabled).slice(0, 50) : [])
          })
          .catch(() => {})
      })
  }, [])

  const handleExplore = async (sourceId: string, p: number = 1) => {
    setLoading(true)
    setSelectedSource(sourceId)
    setPage(p)
    try {
      const data = await apiFetch<{ books: any[] }>(`/api/knowledge/sources/${sourceId}/explore`, {
        method: 'POST',
        body: JSON.stringify({ source_id: sourceId, page: p }),
      })
      setBooks(data.books || [])
    } catch { setBooks([]) }
    finally { setLoading(false) }
  }

  return (
    <div className="space-y-4">
      <h2 className="text-lg font-semibold text-gray-900">排行榜浏览</h2>

      {sources.length === 0 ? (
        <div className="bg-white rounded-lg border border-gray-200 p-8 text-center">
          <p className="text-sm text-gray-500">请先在"书源管理"中导入书源</p>
        </div>
      ) : (
        <>
          <div className="flex gap-2 overflow-x-auto">
            {sources.map((s: any) => (
              <button key={s.id} onClick={() => handleExplore(s.id)}
                className={`px-3 py-1.5 text-xs rounded-full whitespace-nowrap ${
                  selectedSource === s.id ? 'bg-blue-600 text-white' : 'bg-gray-200 text-gray-600'
                }`}>
                {s.name}
              </button>
            ))}
          </div>

          {loading ? (
            <p className="text-sm text-gray-400">加载排行榜...</p>
          ) : books.length > 0 ? (
            <div className="space-y-2">
              {books.map((book: any, idx: number) => (
                <div key={idx} className="bg-white rounded-lg border border-gray-200 p-3">
                  <div className="flex justify-between items-start">
                    <div>
                      <h3 className="font-medium text-gray-900 text-sm">{book.title}</h3>
                      <p className="text-xs text-gray-500">{book.author}</p>
                    </div>
                    {book.kind && <span className="text-[10px] px-2 py-0.5 bg-blue-50 text-blue-600 rounded">{book.kind}</span>}
                  </div>
                  {book.intro && <p className="text-xs text-gray-600 mt-1 line-clamp-2">{book.intro}</p>}
                  <div className="flex gap-2 mt-2">
                    {book.word_count && <span className="text-[10px] text-gray-400">{book.word_count}</span>}
                    {book.last_chapter && <span className="text-[10px] text-gray-400">最新: {book.last_chapter}</span>}
                  </div>
                </div>
              ))}
              <div className="flex gap-2 justify-center">
                {page > 1 && <button onClick={() => handleExplore(selectedSource, page - 1)}
                  className="px-3 py-1.5 text-xs bg-gray-200 rounded">上一页</button>}
                <span className="text-xs text-gray-400 py-1.5">第 {page} 页</span>
                <button onClick={() => handleExplore(selectedSource, page + 1)}
                  className="px-3 py-1.5 text-xs bg-gray-200 rounded">下一页</button>
              </div>
            </div>
          ) : selectedSource ? (
            <div className="bg-white rounded-lg border border-gray-200 p-8 text-center">
              <p className="text-sm text-gray-500">该书源暂无排行榜数据，或书源不支持 explore 规则</p>
            </div>
          ) : null}
        </>
      )}
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
