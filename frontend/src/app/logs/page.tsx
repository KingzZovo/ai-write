'use client'

import { useState, useEffect, useCallback } from 'react'
import { apiFetch } from '@/lib/api'

interface LogRow {
  id: string
  task_type: string
  project_id: string | null
  chapter_id: string | null
  model: string
  input_tokens: number
  output_tokens: number
  latency_ms: number
  status: string
  created_at: string
  rag_hits_count: number
  response_preview: string
}

interface Message {
  role: string
  content: string
}

interface RagHit {
  collection: string
  payload: Record<string, unknown>
  score?: number
}

interface LogDetail extends LogRow {
  messages_json: Message[]
  rag_hits_json: RagHit[]
  response_text: string
  error_message: string | null
  prompt_id: string | null
  endpoint_id: string | null
}

const TASK_TYPES = [
  'generation', 'polishing', 'outline_book', 'outline_volume', 'outline_chapter',
  'evaluation', 'extraction', 'summary', 'rewrite',
]

export default function LogsPage() {
  const [logs, setLogs] = useState<LogRow[]>([])
  const [selected, setSelected] = useState<LogDetail | null>(null)
  const [filters, setFilters] = useState({
    project_id: '', chapter_id: '', task_type: '', status: '',
  })

  const fetchLogs = useCallback(async () => {
    const qs = Object.entries(filters)
      .filter(([, v]) => v)
      .map(([k, v]) => `${k}=${encodeURIComponent(v)}`)
      .join('&')
    try {
      const r = await apiFetch<{ logs: LogRow[] }>(
        `/api/call-logs?limit=100${qs ? '&' + qs : ''}`
      )
      setLogs(r.logs)
    } catch { /* */ }
  }, [filters])

  useEffect(() => { fetchLogs() }, [fetchLogs])

  const openLog = async (id: string) => {
    try {
      const r = await apiFetch<LogDetail>(`/api/call-logs/${id}`)
      setSelected(r)
    } catch (err) {
      alert(err instanceof Error ? err.message : '加载详情失败')
    }
  }

  const deleteLog = async (id: string) => {
    if (!confirm('删除此日志？')) return
    await apiFetch(`/api/call-logs/${id}`, { method: 'DELETE' })
    if (selected?.id === id) setSelected(null)
    fetchLogs()
  }

  return (
    <div className="pt-14 px-4 md:px-8 max-w-7xl mx-auto pb-12">
      <h1 className="text-2xl font-bold mb-4">LLM 调用日志</h1>

      <div className="flex gap-3 mb-4 text-sm flex-wrap">
        <input
          placeholder="project_id"
          value={filters.project_id}
          onChange={e => setFilters(f => ({ ...f, project_id: e.target.value }))}
          className="px-2 py-1 border rounded"
        />
        <input
          placeholder="chapter_id"
          value={filters.chapter_id}
          onChange={e => setFilters(f => ({ ...f, chapter_id: e.target.value }))}
          className="px-2 py-1 border rounded"
        />
        <select
          value={filters.task_type}
          onChange={e => setFilters(f => ({ ...f, task_type: e.target.value }))}
          className="px-2 py-1 border rounded"
        >
          <option value="">-- 任务类型 --</option>
          {TASK_TYPES.map(t => <option key={t} value={t}>{t}</option>)}
        </select>
        <select
          value={filters.status}
          onChange={e => setFilters(f => ({ ...f, status: e.target.value }))}
          className="px-2 py-1 border rounded"
        >
          <option value="">-- 状态 --</option>
          <option value="ok">ok</option>
          <option value="error">error</option>
        </select>
        <button onClick={fetchLogs} className="px-3 py-1 bg-gray-100 rounded hover:bg-gray-200">
          刷新
        </button>
      </div>

      <div className="grid grid-cols-12 gap-4">
        <section className="col-span-5 space-y-1 max-h-[80vh] overflow-y-auto">
          {logs.length === 0 ? (
            <p className="text-sm text-gray-400 text-center py-8">暂无日志</p>
          ) : logs.map(l => (
            <button
              key={l.id}
              onClick={() => openLog(l.id)}
              className={`w-full text-left p-2 rounded border text-xs ${
                selected?.id === l.id ? 'border-blue-500 bg-blue-50' : 'border-gray-200 bg-white'
              }`}
            >
              <div className="flex justify-between mb-1">
                <span className="font-medium">{l.task_type}</span>
                <span className={l.status === 'ok' ? 'text-green-600' : 'text-red-600'}>
                  {l.status}
                </span>
              </div>
              <div className="text-gray-500">
                {l.model || '—'} · {l.input_tokens}+{l.output_tokens}t · {l.latency_ms}ms · RAG {l.rag_hits_count}
              </div>
              <div className="text-gray-400 truncate mt-1">{l.response_preview || '(no response)'}</div>
              <div className="text-gray-400 text-[10px] mt-1">{new Date(l.created_at).toLocaleString()}</div>
            </button>
          ))}
        </section>

        <aside className="col-span-7 bg-white rounded border border-gray-200 p-4 max-h-[80vh] overflow-y-auto">
          {!selected ? (
            <p className="text-sm text-gray-400">点击左侧日志查看详情</p>
          ) : (
            <div className="space-y-4 text-xs">
              <div className="flex justify-between">
                <div>
                  <code className="text-gray-500">{selected.id}</code>
                  <div className="text-gray-400 mt-1">{new Date(selected.created_at).toLocaleString()}</div>
                </div>
                <button
                  onClick={() => deleteLog(selected.id)}
                  className="px-2 py-1 text-[10px] bg-red-50 text-red-600 rounded"
                >
                  删除
                </button>
              </div>
              <div>
                <h3 className="font-semibold mb-1">Messages ({selected.messages_json.length})</h3>
                {selected.messages_json.map((m, i) => (
                  <div key={i} className="mb-2 p-2 bg-gray-50 rounded">
                    <div className="font-medium text-[10px] text-gray-500 uppercase mb-1">{m.role}</div>
                    <pre className="whitespace-pre-wrap break-all">{m.content}</pre>
                  </div>
                ))}
              </div>
              <div>
                <h3 className="font-semibold mb-1">RAG 命中 ({selected.rag_hits_json.length})</h3>
                {selected.rag_hits_json.length === 0 ? (
                  <p className="text-gray-400">— 无 —</p>
                ) : selected.rag_hits_json.map((h, i) => (
                  <div key={i} className="p-2 bg-green-50 rounded mb-1">
                    <span className="text-green-700 font-medium">{h.collection}</span>
                    {typeof h.score === 'number' && (
                      <span className="text-green-600 ml-2">score: {h.score.toFixed(3)}</span>
                    )}
                    <pre className="whitespace-pre-wrap break-all mt-1">{JSON.stringify(h.payload, null, 2)}</pre>
                  </div>
                ))}
              </div>
              <div>
                <h3 className="font-semibold mb-1">Response</h3>
                <pre className="whitespace-pre-wrap p-2 bg-gray-50 rounded break-all">{selected.response_text}</pre>
              </div>
              {selected.error_message && (
                <div>
                  <h3 className="font-semibold mb-1 text-red-600">Error</h3>
                  <pre className="whitespace-pre-wrap p-2 bg-red-50 rounded break-all">{selected.error_message}</pre>
                </div>
              )}
            </div>
          )}
        </aside>
      </div>
    </div>
  )
}
