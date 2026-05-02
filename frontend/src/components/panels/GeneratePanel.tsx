'use client'

import React, { useEffect, useState, useCallback } from 'react'
import Link from 'next/link'
import { useGenerationStore } from '@/stores/generationStore'
import { apiFetch } from '@/lib/api'

interface StyleInfo {
  id: string
  name: string
  is_active: number
  bind_level: string
  rules_json: { rule: string }[]
  tone_keywords: string[]
}

// Exported so DesktopWorkspace can read the selected values
let _selectedStyleId: string | null = null
let _selectedStructureBookId: string | null = null
export function getSelectedStyleId(_projectId?: string) { return _selectedStyleId }
export function getSelectedStructureBookId(_projectId?: string) { return _selectedStructureBookId }

function StyleSelector() {
  const [styles, setStyles] = useState<StyleInfo[]>([])
  const [loading, setLoading] = useState(true)
  const [selectedId, setSelectedId] = useState<string>('')

  useEffect(() => {
    apiFetch<StyleInfo[]>('/api/styles')
      .then(data => {
        setStyles(data)
        // Auto-select the first active style
        const active = data.find(s => s.is_active)
        if (active) {
          setSelectedId(active.id)
          _selectedStyleId = active.id
        }
      })
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [])

  const handleChange = (id: string) => {
    setSelectedId(id)
    _selectedStyleId = id || null
  }

  const selected = styles.find(s => s.id === selectedId)

  if (loading) return <p className="text-xs text-gray-400">加载写法...</p>

  if (styles.length === 0) {
    return (
      <div className="text-xs text-gray-500 space-y-1">
        <p>暂无写法档案</p>
        <Link href="/styles" className="text-blue-600 hover:text-blue-700">
          前往创建写法 &rarr;
        </Link>
      </div>
    )
  }

  return (
    <div className="space-y-2">
      <select value={selectedId} onChange={e => handleChange(e.target.value)}
        className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg bg-white">
        <option value="">不使用写法（默认风格）</option>
        {styles.map(s => (
          <option key={s.id} value={s.id}>
            {s.name} ({s.rules_json?.length || 0}条规则)
          </option>
        ))}
      </select>

      {selected && (
        <div className="px-3 py-2 bg-blue-50 border border-blue-100 rounded-lg">
          <div className="flex flex-wrap gap-1">
            {selected.tone_keywords?.slice(0, 6).map((kw, i) => (
              <span key={i} className="text-[10px] px-1.5 py-0.5 bg-blue-100 text-blue-700 rounded">{kw}</span>
            ))}
          </div>
          <p className="text-[10px] text-blue-600 mt-1">{selected.rules_json?.length || 0} 条规则 · 生成时注入</p>
        </div>
      )}

      <Link href="/styles" className="block text-xs text-blue-600 hover:text-blue-700">
        管理写法 ({styles.length}) &rarr;
      </Link>
    </div>
  )
}

interface GeneratePanelProps {
  projectId?: string
  onGenerate?: () => void
  onGenerateOutline?: (level: string) => void
  onViewOutline?: (level: string) => void
}

interface EndpointInfo {
  id: string
  name: string
  provider_type: string
  default_model: string
  enabled: number
}

interface TaskConfig {
  task_type: string
  endpoint: EndpointInfo | null
  model_name: string
  temperature: number
  max_tokens: number
}

const TASK_LABELS: Record<string, string> = {
  generation: '正文生成',
  polishing: '润色',
  outline: '大纲生成',
  extraction: '信息提取',
  evaluation: '质量评估',
  summary: '摘要',
  embedding: '向量嵌入',
}

export function GeneratePanel({ projectId, onGenerate, onGenerateOutline, onViewOutline }: GeneratePanelProps) {
  const { isGenerating } = useGenerationStore()
  const [endpoints, setEndpoints] = useState<EndpointInfo[]>([])
  const [tasks, setTasks] = useState<TaskConfig[]>([])
  const [loading, setLoading] = useState(true)

  // outline 存在性状态。key: book/volume/chapter
  const [outlineCounts, setOutlineCounts] = useState<Record<string, number>>({ book: 0, volume: 0, chapter: 0 })
  const [confirmLevel, setConfirmLevel] = useState<string | null>(null)
  const refreshOutlineCounts = useCallback(async () => {
    if (!projectId) return
    try {
      const all = await apiFetch<Array<{ level: string }>>(`/api/projects/${projectId}/outlines`)
      const counts: Record<string, number> = { book: 0, volume: 0, chapter: 0 }
      ;(all || []).forEach(o => { counts[o.level] = (counts[o.level] || 0) + 1 })
      setOutlineCounts(counts)
    } catch { /* ignore */ }
  }, [projectId])

  useEffect(() => {
    Promise.all([
      apiFetch<{ endpoints: EndpointInfo[] }>('/api/model-config/endpoints').catch(() => ({ endpoints: [] })),
      apiFetch<{ tasks: TaskConfig[] }>('/api/model-config/tasks').catch(() => ({ tasks: [] })),
    ]).then(([epData, taskData]) => {
      setEndpoints(epData.endpoints)
      setTasks(taskData.tasks)
      setLoading(false)
    })
  }, [])

  useEffect(() => { refreshOutlineCounts() }, [refreshOutlineCounts, isGenerating])

  const enabledEndpoints = endpoints.filter(e => e.enabled)
  const hasEndpoints = enabledEndpoints.length > 0

  // Key tasks for writing
  const writingTasks = tasks.filter(t => ['generation', 'outline', 'polishing'].includes(t.task_type))
  const otherTasks = tasks.filter(t => !['generation', 'outline', 'polishing'].includes(t.task_type))

  return (
    <div className="p-4 space-y-5">
      {/* Current model status */}
      <div>
        <h3 className="text-sm font-semibold text-gray-900 mb-3">当前模型配置</h3>
        {loading ? (
          <p className="text-xs text-gray-400">加载中...</p>
        ) : !hasEndpoints ? (
          <div className="p-3 bg-amber-50 border border-amber-200 rounded-lg">
            <p className="text-xs text-amber-700 mb-2">尚未配置模型端点</p>
            <Link href="/settings"
              className="text-xs text-blue-600 hover:text-blue-700 font-medium">
              前往设置页面配置 &rarr;
            </Link>
          </div>
        ) : (
          <div className="space-y-2">
            {/* Writing-related tasks */}
            {writingTasks.map(t => (
              <div key={t.task_type}
                className="flex items-center justify-between px-3 py-2 bg-gray-50 rounded-lg">
                <span className="text-xs font-medium text-gray-600">
                  {TASK_LABELS[t.task_type] || t.task_type}
                </span>
                {t.endpoint ? (
                  <span className="text-xs text-gray-800 font-mono truncate max-w-[140px]"
                    title={`${t.endpoint.name} / ${t.model_name || t.endpoint.default_model}`}>
                    {t.model_name || t.endpoint.default_model}
                  </span>
                ) : (
                  <span className="text-xs text-gray-400">未分配</span>
                )}
              </div>
            ))}

            {/* Other tasks collapsed */}
            {otherTasks.length > 0 && (
              <details className="text-xs">
                <summary className="text-gray-400 cursor-pointer hover:text-gray-600 py-1">
                  其他任务 ({otherTasks.length})
                </summary>
                <div className="space-y-1 mt-1">
                  {otherTasks.map(t => (
                    <div key={t.task_type}
                      className="flex items-center justify-between px-3 py-1.5 bg-gray-50 rounded">
                      <span className="text-gray-500">
                        {TASK_LABELS[t.task_type] || t.task_type}
                      </span>
                      {t.endpoint ? (
                        <span className="text-gray-700 font-mono truncate max-w-[120px]">
                          {t.model_name || t.endpoint.default_model}
                        </span>
                      ) : (
                        <span className="text-gray-400">未分配</span>
                      )}
                    </div>
                  ))}
                </div>
              </details>
            )}

            <Link href="/settings"
              className="block text-xs text-blue-600 hover:text-blue-700 mt-1">
              管理端点和任务分配 &rarr;
            </Link>
          </div>
        )}
      </div>

      {/* Generation buttons */}
      <div className="border-t pt-4">
        <h3 className="text-sm font-semibold text-gray-900 mb-3">内容生成</h3>
        <div className="space-y-2">
          <OutlineButtonRow
            level="book" label="全书大纲" colorClass="bg-purple-600 hover:bg-purple-700"
            count={outlineCounts.book} disabled={isGenerating || !hasEndpoints}
            onView={() => onViewOutline?.("book")}
            onGenerate={() => outlineCounts.book > 0 ? setConfirmLevel("book") : onGenerateOutline?.("book")}
          />
          <OutlineButtonRow
            level="volume" label="分卷大纲" colorClass="bg-indigo-600 hover:bg-indigo-700"
            count={outlineCounts.volume} disabled={isGenerating || !hasEndpoints}
            onView={() => onViewOutline?.("volume")}
            onGenerate={() => outlineCounts.volume > 0 ? setConfirmLevel("volume") : onGenerateOutline?.("volume")}
          />
          <OutlineButtonRow
            level="chapter" label="章节大纲" colorClass="bg-blue-600 hover:bg-blue-700"
            count={outlineCounts.chapter} disabled={isGenerating || !hasEndpoints}
            onView={() => onViewOutline?.("chapter")}
            onGenerate={() => outlineCounts.chapter > 0 ? setConfirmLevel("chapter") : onGenerateOutline?.("chapter")}
          />
          <button onClick={onGenerate} disabled={isGenerating || !hasEndpoints}
            className="w-full px-4 py-2 text-sm bg-green-600 text-white rounded-lg hover:bg-green-700 disabled:opacity-50">
            {isGenerating ? "生成中..." : "生成章节正文"}
          </button>
          {confirmLevel && (
            <ConfirmModal
              level={confirmLevel}
              onCancel={() => setConfirmLevel(null)}
              onConfirm={() => { onGenerateOutline?.(confirmLevel); setConfirmLevel(null) }}
            />
          )}
        </div>
      </div>

      {/* Writing style selector */}
      <div className="border-t pt-4">
        <h3 className="text-sm font-semibold text-gray-900 mb-3">写作风格</h3>
        <StyleSelector />
      </div>

      {/* Plot structure selector (optional) */}
      <div className="border-t pt-4">
        <h3 className="text-sm font-semibold text-gray-900 mb-3">剧情架构（可选）</h3>
        <StructureSelector />
      </div>
    </div>
  )
}

function StructureSelector() {
  const [structures, setStructures] = useState<any[]>([])
  const [selectedId, setSelectedId] = useState<string>('')

  useEffect(() => {
    apiFetch<any[]>('/api/styles/structures')
      .then(setStructures)
      .catch(() => {})
  }, [])

  const handleChange = (id: string) => {
    setSelectedId(id)
    _selectedStructureBookId = id || null
  }

  if (structures.length === 0) {
    return <p className="text-xs text-gray-400">暂无架构数据，请先在参考书库中"提取架构"</p>
  }

  return (
    <div className="space-y-2">
      <select value={selectedId} onChange={e => handleChange(e.target.value)}
        className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg bg-white">
        <option value="">不使用剧情架构</option>
        {structures.map((s: any) => (
          <option key={s.book_id} value={s.book_id}>
            {s.book_title} — {s.arc_pattern || ''}
          </option>
        ))}
      </select>
      {selectedId && (
        <p className="text-[10px] text-orange-500">
          {structures.find((s: any) => s.book_id === selectedId)?.structure_summary || ''}
        </p>
      )}
    </div>
  )
}

function OutlineButtonRow({ level, label, colorClass, count, disabled, onView, onGenerate }: {
  level: string; label: string; colorClass: string; count: number; disabled: boolean;
  onView: () => void; onGenerate: () => void
}) {
  const _ = level // reserved for analytics later
  const exists = count > 0
  if (exists) {
    return (
      <div className="flex gap-1.5">
        <button onClick={onView} disabled={disabled}
          className={`flex-1 px-3 py-2 text-sm text-white rounded-lg disabled:opacity-50 ${colorClass}`}>
          📖 查看{label} ({count})
        </button>
        <button onClick={onGenerate} disabled={disabled} title="重新生成（需确认）"
          className="px-2 py-2 text-xs bg-stone-200 text-stone-700 rounded-lg hover:bg-stone-300 disabled:opacity-50">
          ↺
        </button>
      </div>
    )
  }
  return (
    <button onClick={onGenerate} disabled={disabled}
      className={`w-full px-4 py-2 text-sm text-white rounded-lg disabled:opacity-50 ${colorClass}`}>
      ⚡ 生成{label}
    </button>
  )
}

function ConfirmModal({ level, onCancel, onConfirm }: { level: string; onCancel: () => void; onConfirm: () => void }) {
  const labels: Record<string, string> = { book: "全书大纲", volume: "分卷大纲", chapter: "章节大纲" }
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
      <div className="bg-white rounded-lg shadow-xl max-w-sm w-full p-5 space-y-3">
        <h3 className="text-base font-semibold text-stone-900">重新生成{labels[level] || level}？</h3>
        <p className="text-sm text-stone-600">已存在{labels[level] || level}。重新生成会产生新的版本，原有版本会保留但不再被默认使用。</p>
        <div className="flex gap-2 justify-end">
          <button onClick={onCancel} className="px-3 py-1.5 text-sm text-stone-700 hover:bg-stone-100 rounded">取消</button>
          <button onClick={onConfirm} className="px-3 py-1.5 text-sm bg-red-600 text-white hover:bg-red-700 rounded">确认重新生成</button>
        </div>
      </div>
    </div>
  )
}
