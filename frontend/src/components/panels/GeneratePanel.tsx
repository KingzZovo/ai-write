'use client'

import React, { useEffect, useState } from 'react'
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

function StyleSelector() {
  const [styles, setStyles] = useState<StyleInfo[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    apiFetch<StyleInfo[]>('/api/styles')
      .then(setStyles)
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [])

  const activeStyles = styles.filter(s => s.is_active)

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
      {activeStyles.length > 0 ? (
        <>
          <p className="text-xs text-gray-500">当前激活的写法（生成时自动注入）：</p>
          {activeStyles.map(s => (
            <div key={s.id} className="px-3 py-2 bg-green-50 border border-green-200 rounded-lg">
              <div className="flex items-center justify-between">
                <span className="text-sm font-medium text-green-800">{s.name}</span>
                <span className="text-[10px] px-1.5 py-0.5 bg-green-100 text-green-600 rounded">
                  {s.bind_level === 'global' ? '全局' : s.bind_level === 'book' ? '整本书' : '单章'}
                </span>
              </div>
              <div className="flex flex-wrap gap-1 mt-1">
                {s.tone_keywords?.slice(0, 5).map((kw, i) => (
                  <span key={i} className="text-[10px] px-1 py-0.5 bg-green-100 text-green-700 rounded">{kw}</span>
                ))}
              </div>
              <p className="text-[10px] text-green-600 mt-1">{s.rules_json?.length || 0} 条规则</p>
            </div>
          ))}
        </>
      ) : (
        <p className="text-xs text-gray-500">无激活的写法 — 生成将使用默认风格</p>
      )}
      <Link href="/styles" className="block text-xs text-blue-600 hover:text-blue-700">
        管理写法 ({styles.length}) &rarr;
      </Link>
    </div>
  )
}

interface GeneratePanelProps {
  onGenerate?: () => void
  onGenerateOutline?: (level: string) => void
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

export function GeneratePanel({ onGenerate, onGenerateOutline }: GeneratePanelProps) {
  const { isGenerating } = useGenerationStore()
  const [endpoints, setEndpoints] = useState<EndpointInfo[]>([])
  const [tasks, setTasks] = useState<TaskConfig[]>([])
  const [loading, setLoading] = useState(true)

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
          <button onClick={() => onGenerateOutline?.('book')} disabled={isGenerating || !hasEndpoints}
            className="w-full px-4 py-2 text-sm bg-purple-600 text-white rounded-lg hover:bg-purple-700 disabled:opacity-50">
            生成全书大纲
          </button>
          <button onClick={() => onGenerateOutline?.('volume')} disabled={isGenerating || !hasEndpoints}
            className="w-full px-4 py-2 text-sm bg-indigo-600 text-white rounded-lg hover:bg-indigo-700 disabled:opacity-50">
            生成分卷大纲
          </button>
          <button onClick={() => onGenerateOutline?.('chapter')} disabled={isGenerating || !hasEndpoints}
            className="w-full px-4 py-2 text-sm bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50">
            生成章节大纲
          </button>
          <button onClick={onGenerate} disabled={isGenerating || !hasEndpoints}
            className="w-full px-4 py-2 text-sm bg-green-600 text-white rounded-lg hover:bg-green-700 disabled:opacity-50">
            {isGenerating ? '生成中...' : '生成章节正文'}
          </button>
        </div>
      </div>

      {/* Writing style selector */}
      <div className="border-t pt-4">
        <h3 className="text-sm font-semibold text-gray-900 mb-3">写作风格</h3>
        <StyleSelector />
      </div>
    </div>
  )
}
