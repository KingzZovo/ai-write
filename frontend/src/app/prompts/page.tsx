'use client'

import { useState, useEffect, useCallback } from 'react'
import { apiFetch } from '@/lib/api'

interface Endpoint {
  id: string
  name: string
  provider_type: string
  default_model: string
}

interface PromptAsset {
  id: string
  task_type: string
  name: string
  name_en: string
  description: string
  description_en: string
  mode: string
  system_prompt: string
  user_template: string
  output_schema: Record<string, unknown> | null
  context_policy: string
  version: number
  is_active: number
  endpoint_id: string | null
  model_name: string
  temperature: number
  max_tokens: number
  category: string
  order: number
  always_enabled: number
  success_count: number
  fail_count: number
  avg_score: number
  created_at: string
  updated_at: string
}

const MODE_LABELS: Record<string, string> = { text: '文本', structured: '结构化(JSON)' }

export default function PromptsPage() {
  const [prompts, setPrompts] = useState<PromptAsset[]>([])
  const [endpoints, setEndpoints] = useState<Endpoint[]>([])
  const [loading, setLoading] = useState(true)
  const [editing, setEditing] = useState<PromptAsset | null>(null)
  const [showCreate, setShowCreate] = useState(false)

  const fetchPrompts = useCallback(async () => {
    try {
      const data = await apiFetch<PromptAsset[]>('/api/prompts')
      setPrompts(data)
    } catch { /* */ }
    finally { setLoading(false) }
  }, [])

  const fetchEndpoints = useCallback(async () => {
    try {
      const r = await apiFetch<{ endpoints: Endpoint[] }>('/api/model-config/endpoints')
      setEndpoints(r.endpoints)
    } catch { /* */ }
  }, [])

  useEffect(() => { fetchPrompts(); fetchEndpoints() }, [fetchPrompts, fetchEndpoints])

  const handleDelete = async (id: string) => {
    if (!confirm('确定删除此 Prompt？')) return
    await apiFetch(`/api/prompts/${id}`, { method: 'DELETE' })
    fetchPrompts()
  }

  const patchField = async (id: string, patch: Partial<PromptAsset>) => {
    await apiFetch(`/api/prompts/${id}`, {
      method: 'PUT',
      body: JSON.stringify(patch),
    })
    fetchPrompts()
  }

  // Group by category, sort each by order
  const grouped = prompts.reduce<Record<string, PromptAsset[]>>((acc, p) => {
    const key = p.category || 'Other'
    ;(acc[key] = acc[key] || []).push(p)
    return acc
  }, {})
  Object.values(grouped).forEach(arr =>
    arr.sort((a, b) => (a.order || 0) - (b.order || 0) || a.version - b.version)
  )

  return (
    <div className="pt-14 px-4 md:px-8 max-w-5xl mx-auto pb-12">
      <div className="flex items-end justify-between mb-8">
        <div>
          <h1 className="text-2xl font-bold text-gray-900 tracking-tight">Prompt 注册表</h1>
          <p className="text-sm text-gray-500 mt-1">每个 Prompt 绑定独立端点 · 版本化 · 调用可追溯</p>
        </div>
        <button onClick={() => { setEditing(null); setShowCreate(true) }}
          className="px-4 py-2 text-sm bg-gray-900 text-white rounded-lg hover:bg-gray-800">
          + 新建 Prompt
        </button>
      </div>

      {(showCreate || editing) && (
        <PromptForm
          prompt={editing}
          endpoints={endpoints}
          onClose={() => { setShowCreate(false); setEditing(null) }}
          onSaved={() => { setShowCreate(false); setEditing(null); fetchPrompts() }}
        />
      )}

      {loading ? (
        <p className="text-sm text-gray-400 text-center py-16">加载中...</p>
      ) : prompts.length === 0 ? (
        <p className="text-sm text-gray-400 text-center py-16">暂无 Prompt</p>
      ) : (
        <div className="space-y-6">
          {Object.entries(grouped).map(([category, assets]) => (
            <section key={category}>
              <h2 className="text-xs font-semibold text-gray-400 uppercase tracking-wider mb-2 px-1">
                {category}
              </h2>
              <div className="bg-white rounded-xl border border-gray-200 overflow-hidden">
                {assets.map(p => {
                  const totalCalls = p.success_count + p.fail_count
                  const successRate = totalCalls > 0 ? Math.round(p.success_count / totalCalls * 100) : 0
                  const endpoint = endpoints.find(e => e.id === p.endpoint_id)
                  return (
                    <div
                      key={p.id}
                      className={`px-5 py-3 border-b border-gray-50 last:border-b-0 ${!p.is_active ? 'opacity-50' : ''}`}
                    >
                      <div className="flex items-center justify-between mb-2">
                        <div className="flex items-center gap-2 flex-wrap">
                          <span className="text-sm font-medium text-gray-800">{p.name}</span>
                          <code className="text-[10px] px-1 py-0.5 bg-gray-100 text-gray-500 rounded">{p.task_type}</code>
                          <span className="text-[10px] px-1.5 py-0.5 bg-gray-100 text-gray-500 rounded">v{p.version}</span>
                          <span className="text-[10px] px-1.5 py-0.5 bg-blue-50 text-blue-600 rounded">{MODE_LABELS[p.mode] || p.mode}</span>
                          {p.always_enabled === 1 && (
                            <span className="text-[10px] px-1.5 py-0.5 bg-amber-50 text-amber-700 rounded">always-on</span>
                          )}
                          {p.is_active === 1 ? (
                            <span className="text-[10px] px-1.5 py-0.5 bg-green-50 text-green-600 rounded-full">激活</span>
                          ) : (
                            <span className="text-[10px] px-1.5 py-0.5 bg-gray-100 text-gray-400 rounded-full">历史</span>
                          )}
                        </div>
                        <div className="flex items-center gap-3 text-xs text-gray-400">
                          {totalCalls > 0 && <span>调用 {totalCalls} 次 · 成功率 {successRate}%</span>}
                        </div>
                      </div>

                      {p.description && <p className="text-xs text-gray-500 mb-2">{p.description}</p>}

                      {/* Routing controls (v0.5) */}
                      <div className="grid grid-cols-1 md:grid-cols-2 gap-2 mb-2">
                        <div>
                          <label className="block text-[10px] text-gray-400 mb-0.5">端点</label>
                          <select
                            value={p.endpoint_id || ''}
                            onChange={e => patchField(p.id, { endpoint_id: e.target.value || null })}
                            className="w-full px-2 py-1 text-xs border border-gray-200 rounded bg-white"
                          >
                            <option value="">-- 未分配 --</option>
                            {endpoints.map(ep => (
                              <option key={ep.id} value={ep.id}>{ep.name} ({ep.provider_type})</option>
                            ))}
                          </select>
                        </div>
                        <div>
                          <label className="block text-[10px] text-gray-400 mb-0.5">模型覆盖</label>
                          <input
                            type="text"
                            defaultValue={p.model_name}
                            placeholder={endpoint?.default_model || '使用端点默认'}
                            onBlur={e => {
                              if (e.target.value !== p.model_name) {
                                patchField(p.id, { model_name: e.target.value })
                              }
                            }}
                            className="w-full px-2 py-1 text-xs border border-gray-200 rounded"
                          />
                        </div>
                        <div>
                          <label className="block text-[10px] text-gray-400 mb-0.5">温度 {p.temperature?.toFixed(1)}</label>
                          <input
                            type="range" min="0" max="1" step="0.1"
                            defaultValue={p.temperature}
                            onMouseUp={e => {
                              const v = parseFloat((e.target as HTMLInputElement).value)
                              if (v !== p.temperature) patchField(p.id, { temperature: v })
                            }}
                            className="w-full"
                          />
                        </div>
                        <div>
                          <label className="block text-[10px] text-gray-400 mb-0.5">最大长度</label>
                          <input
                            type="number"
                            defaultValue={p.max_tokens}
                            min={1} max={131072}
                            onBlur={e => {
                              const v = parseInt(e.target.value) || 4096
                              if (v !== p.max_tokens) patchField(p.id, { max_tokens: v })
                            }}
                            className="w-full px-2 py-1 text-xs border border-gray-200 rounded"
                          />
                        </div>
                      </div>

                      <div className="bg-gray-50 rounded p-2 mb-2">
                        <pre className="text-[11px] text-gray-600 whitespace-pre-wrap line-clamp-3 font-mono">{p.system_prompt}</pre>
                      </div>

                      <div className="flex gap-1.5">
                        <button
                          onClick={() => setEditing(p)}
                          className="px-2.5 py-1 text-xs bg-gray-100 text-gray-700 rounded hover:bg-gray-200"
                        >编辑</button>
                        <button
                          onClick={() => handleDelete(p.id)}
                          className="px-2.5 py-1 text-xs bg-red-50 text-red-600 rounded hover:bg-red-100 ml-auto"
                        >删除</button>
                      </div>
                    </div>
                  )
                })}
              </div>
            </section>
          ))}
        </div>
      )}
    </div>
  )
}

function PromptForm({
  prompt,
  endpoints,
  onClose,
  onSaved,
}: {
  prompt: PromptAsset | null
  endpoints: Endpoint[]
  onClose: () => void
  onSaved: () => void
}) {
  const isEdit = !!prompt
  const [taskType, setTaskType] = useState(prompt?.task_type || '')
  const [name, setName] = useState(prompt?.name || '')
  const [description, setDescription] = useState(prompt?.description || '')
  const [mode, setMode] = useState(prompt?.mode || 'text')
  const [systemPrompt, setSystemPrompt] = useState(prompt?.system_prompt || '')
  const [userTemplate, setUserTemplate] = useState(prompt?.user_template || '')
  const [category, setCategory] = useState(prompt?.category || 'Core')
  const [endpointId, setEndpointId] = useState<string>(prompt?.endpoint_id || '')
  const [modelName, setModelName] = useState(prompt?.model_name || '')
  const [temperature, setTemperature] = useState(prompt?.temperature ?? 0.7)
  const [maxTokens, setMaxTokens] = useState(prompt?.max_tokens ?? 4096)
  const [saving, setSaving] = useState(false)

  const handleSave = async () => {
    if (!taskType.trim() || !name.trim() || !systemPrompt.trim()) return
    setSaving(true)
    try {
      if (isEdit) {
        await apiFetch(`/api/prompts/${prompt.id}`, {
          method: 'PUT',
          body: JSON.stringify({
            name, description, system_prompt: systemPrompt, user_template: userTemplate,
            category, endpoint_id: endpointId || null, model_name: modelName,
            temperature, max_tokens: maxTokens,
          }),
        })
      } else {
        await apiFetch('/api/prompts', {
          method: 'POST',
          body: JSON.stringify({
            task_type: taskType, name, description, mode,
            system_prompt: systemPrompt, user_template: userTemplate,
            category, endpoint_id: endpointId || null, model_name: modelName,
            temperature, max_tokens: maxTokens,
          }),
        })
      }
      onSaved()
    } catch (e) {
      alert(e instanceof Error ? e.message : '保存失败')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="mb-6 bg-white rounded-xl border border-gray-200 p-5 space-y-4">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold text-gray-900">{isEdit ? '编辑 Prompt' : '新建 Prompt'}</h3>
        <button onClick={onClose} className="text-xs text-gray-400 hover:text-gray-600">取消</button>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
        <div>
          <label className="block text-xs font-medium text-gray-600 mb-1">任务类型 *</label>
          <input
            value={taskType} onChange={e => setTaskType(e.target.value)} disabled={isEdit}
            placeholder="如 outline_book, rewrite_emotion"
            className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg disabled:bg-gray-50"
          />
        </div>
        <div>
          <label className="block text-xs font-medium text-gray-600 mb-1">名称 *</label>
          <input
            value={name} onChange={e => setName(e.target.value)}
            className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg"
          />
        </div>
        <div>
          <label className="block text-xs font-medium text-gray-600 mb-1">分类</label>
          <input
            value={category} onChange={e => setCategory(e.target.value)}
            placeholder="Core / Outline / Extraction / Editing"
            className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg"
          />
        </div>
      </div>

      <div>
        <label className="block text-xs font-medium text-gray-600 mb-1">描述</label>
        <input
          value={description} onChange={e => setDescription(e.target.value)}
          className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg"
        />
      </div>

      <div className="grid grid-cols-1 md:grid-cols-4 gap-3">
        <div className="md:col-span-2">
          <label className="block text-xs font-medium text-gray-600 mb-1">端点</label>
          <select
            value={endpointId} onChange={e => setEndpointId(e.target.value)}
            className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg bg-white"
          >
            <option value="">-- 未分配 --</option>
            {endpoints.map(ep => (
              <option key={ep.id} value={ep.id}>{ep.name} ({ep.provider_type})</option>
            ))}
          </select>
        </div>
        <div>
          <label className="block text-xs font-medium text-gray-600 mb-1">模型覆盖</label>
          <input
            value={modelName} onChange={e => setModelName(e.target.value)}
            placeholder="留空用默认"
            className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg"
          />
        </div>
        <div>
          <label className="block text-xs font-medium text-gray-600 mb-1">模式</label>
          <select
            value={mode} onChange={e => setMode(e.target.value)} disabled={isEdit}
            className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg disabled:bg-gray-50"
          >
            <option value="text">文本</option>
            <option value="structured">结构化 JSON</option>
          </select>
        </div>
      </div>

      <div className="grid grid-cols-2 gap-3">
        <div>
          <label className="block text-xs font-medium text-gray-600 mb-1">温度: {temperature.toFixed(1)}</label>
          <input
            type="range" min="0" max="1" step="0.1" value={temperature}
            onChange={e => setTemperature(parseFloat(e.target.value))}
            className="w-full"
          />
        </div>
        <div>
          <label className="block text-xs font-medium text-gray-600 mb-1">最大长度</label>
          <input
            type="number" value={maxTokens}
            min={1} max={131072}
            onChange={e => setMaxTokens(parseInt(e.target.value) || 4096)}
            className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg"
          />
        </div>
      </div>

      <div>
        <label className="block text-xs font-medium text-gray-600 mb-1">System Prompt *</label>
        <textarea
          value={systemPrompt} onChange={e => setSystemPrompt(e.target.value)}
          className="w-full h-40 px-3 py-2 text-sm border border-gray-200 rounded-lg font-mono resize-none"
        />
      </div>

      <div>
        <label className="block text-xs font-medium text-gray-600 mb-1">
          User Template（可选，{'{{变量}}'}）
        </label>
        <textarea
          value={userTemplate} onChange={e => setUserTemplate(e.target.value)}
          className="w-full h-20 px-3 py-2 text-sm border border-gray-200 rounded-lg font-mono resize-none"
        />
      </div>

      <div className="flex gap-3">
        <button
          onClick={handleSave}
          disabled={saving || !taskType.trim() || !systemPrompt.trim()}
          className="px-5 py-2 text-sm bg-gray-900 text-white rounded-lg disabled:opacity-50"
        >
          {saving ? '保存中...' : isEdit ? '更新' : '创建'}
        </button>
        {!isEdit && <p className="text-xs text-gray-400 self-center">创建会自动停用同任务类型的旧版本</p>}
      </div>
    </div>
  )
}
