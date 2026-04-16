'use client'

import React, { useEffect, useState, useCallback } from 'react'
import { apiFetch } from '@/lib/api'

// =========================================================================
// Types
// =========================================================================

interface Endpoint {
  id: string
  name: string
  provider_type: string
  base_url: string
  api_key_masked: string
  default_model: string
  enabled: number
  created_at: string
}

interface TaskConfig {
  task_type: string
  endpoint: Endpoint | null
  model_name: string
  temperature: number
  max_tokens: number
}

interface TestResult {
  success: boolean
  message: string
  latency_ms: number | null
}

interface Preset {
  name: string
  description: string
  tasks: Record<string, { model_name: string; temperature: number; max_tokens: number }>
}

// =========================================================================
// Constants
// =========================================================================

const PROVIDER_OPTIONS = [
  { value: 'anthropic', label: 'Anthropic' },
  { value: 'openai', label: 'OpenAI' },
  { value: 'openai_compatible', label: 'OpenAI 兼容' },
]

const MODEL_SUGGESTIONS: Record<string, string[]> = {
  anthropic: ['claude-sonnet-4-20250514', 'claude-haiku-4-5-20251001'],
  openai: ['gpt-4o', 'gpt-4o-mini', 'text-embedding-3-small'],
  openai_compatible: [],
}

const TASK_LABELS: Record<string, string> = {
  outline: '\u5927\u7EB2\u751F\u6210',
  generation: '\u7AE0\u8282\u751F\u6210',
  polishing: '\u98CE\u683C\u6DA6\u8272',
  extraction: '\u6458\u8981\u63D0\u53D6',
  summary: '\u5185\u5BB9\u603B\u7ED3',
  evaluation: '\u8D28\u91CF\u8BC4\u4F30',
  embedding: '\u6587\u672C\u5411\u91CF\u5316',
}

// =========================================================================
// Main Page
// =========================================================================

export default function SettingsPage() {
  const [endpoints, setEndpoints] = useState<Endpoint[]>([])
  const [tasks, setTasks] = useState<TaskConfig[]>([])
  const [presets, setPresets] = useState<Preset[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const fetchAll = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const [epRes, taskRes, presetRes] = await Promise.all([
        apiFetch<{ endpoints: Endpoint[]; total: number }>('/api/model-config/endpoints'),
        apiFetch<{ tasks: TaskConfig[] }>('/api/model-config/tasks'),
        apiFetch<Preset[]>('/api/model-config/presets'),
      ])
      setEndpoints(epRes.endpoints)
      setTasks(taskRes.tasks)
      setPresets(presetRes)
    } catch (err) {
      setError(err instanceof Error ? err.message : '加载设置失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchAll()
  }, [fetchAll])

  if (loading) {
    return (
      <div className="space-y-6">
        <div className="bg-white rounded-lg border border-gray-200 p-12 text-center">
          <p className="text-sm text-gray-500">正在加载模型配置...</p>
        </div>
      </div>
    )
  }

  return (
    <div className="space-y-8">
      {error && (
        <div className="bg-red-50 border border-red-200 rounded-lg p-3 text-sm text-red-700">
          {error}
        </div>
      )}

      <EndpointsSection endpoints={endpoints} onRefresh={fetchAll} />
      <TaskRoutingSection endpoints={endpoints} tasks={tasks} onRefresh={fetchAll} />
      <PresetsSection presets={presets} endpoints={endpoints} onRefresh={fetchAll} />
    </div>
  )
}

// =========================================================================
// Section 1: API Endpoints
// =========================================================================

function EndpointsSection({
  endpoints,
  onRefresh,
}: {
  endpoints: Endpoint[]
  onRefresh: () => void
}) {
  const [showForm, setShowForm] = useState(false)
  const [editingId, setEditingId] = useState<string | null>(null)
  const [formData, setFormData] = useState({
    name: '',
    provider_type: 'anthropic',
    base_url: '',
    api_key: '',
    default_model: '',
  })
  const [saving, setSaving] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)
  const [testingId, setTestingId] = useState<string | null>(null)
  const [testResults, setTestResults] = useState<Record<string, TestResult>>({})

  const resetForm = useCallback(() => {
    setFormData({ name: '', provider_type: 'anthropic', base_url: '', api_key: '', default_model: '' })
    setShowForm(false)
    setEditingId(null)
    setFormError(null)
  }, [])

  const handleEdit = useCallback((ep: Endpoint) => {
    setFormData({
      name: ep.name,
      provider_type: ep.provider_type,
      base_url: ep.base_url,
      api_key: '',
      default_model: ep.default_model,
    })
    setEditingId(ep.id)
    setShowForm(true)
    setFormError(null)
  }, [])

  const handleSubmit = useCallback(async () => {
    setSaving(true)
    setFormError(null)
    try {
      if (editingId) {
        const body: Record<string, string> = {
          name: formData.name,
          provider_type: formData.provider_type,
          base_url: formData.base_url,
          default_model: formData.default_model,
        }
        if (formData.api_key) {
          body.api_key = formData.api_key
        }
        await apiFetch(`/api/model-config/endpoints/${editingId}`, {
          method: 'PUT',
          body: JSON.stringify(body),
        })
      } else {
        await apiFetch('/api/model-config/endpoints', {
          method: 'POST',
          body: JSON.stringify(formData),
        })
      }
      resetForm()
      onRefresh()
    } catch (err) {
      setFormError(err instanceof Error ? err.message : '保存失败')
    } finally {
      setSaving(false)
    }
  }, [formData, editingId, resetForm, onRefresh])

  const handleDelete = useCallback(
    async (id: string) => {
      if (!confirm('确定删除此端点？引用该端点的任务配置将失去关联。')) return
      try {
        await apiFetch(`/api/model-config/endpoints/${id}`, { method: 'DELETE' })
        onRefresh()
      } catch (err) {
        alert(err instanceof Error ? err.message : '删除失败')
      }
    },
    [onRefresh]
  )

  const handleTest = useCallback(async (id: string) => {
    setTestingId(id)
    try {
      const result = await apiFetch<TestResult>(`/api/model-config/endpoints/${id}/test`, {
        method: 'POST',
      })
      setTestResults((prev) => ({ ...prev, [id]: result }))
    } catch (err) {
      setTestResults((prev) => ({
        ...prev,
        [id]: { success: false, message: err instanceof Error ? err.message : '测试失败', latency_ms: null },
      }))
    } finally {
      setTestingId(null)
    }
  }, [])

  const suggestions = MODEL_SUGGESTIONS[formData.provider_type] || []

  return (
    <section>
      <div className="flex items-center justify-between mb-4">
        <div>
          <h2 className="text-lg font-semibold text-gray-900">API 端点</h2>
          <p className="text-sm text-gray-500 mt-1">配置 LLM API 连接</p>
        </div>
        <button
          onClick={() => {
            resetForm()
            setShowForm(true)
          }}
          className="px-4 py-2 text-sm bg-blue-600 text-white rounded-lg hover:bg-blue-700"
        >
          添加端点
        </button>
      </div>

      {/* Add / Edit Form */}
      {showForm && (
        <div className="bg-white rounded-lg border border-gray-200 p-5 mb-4 space-y-4">
          <h3 className="text-sm font-semibold text-gray-700">
            {editingId ? '编辑端点' : '新建端点'}
          </h3>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">名称 *</label>
              <input
                type="text"
                value={formData.name}
                onChange={(e) => setFormData((d) => ({ ...d, name: e.target.value }))}
                placeholder="例如 Claude API、本地 Qwen"
                className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent"
              />
            </div>

            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">供应商类型 *</label>
              <select
                value={formData.provider_type}
                onChange={(e) =>
                  setFormData((d) => ({ ...d, provider_type: e.target.value, base_url: '', default_model: '' }))
                }
                className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent bg-white"
              >
                {PROVIDER_OPTIONS.map((opt) => (
                  <option key={opt.value} value={opt.value}>
                    {opt.label}
                  </option>
                ))}
              </select>
            </div>

            {formData.provider_type === 'openai_compatible' && (
              <div className="md:col-span-2">
                <label className="block text-sm font-medium text-gray-700 mb-1">基础地址 *</label>
                <input
                  type="text"
                  value={formData.base_url}
                  onChange={(e) => setFormData((d) => ({ ...d, base_url: e.target.value }))}
                  placeholder="http://localhost:11434/v1"
                  className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent"
                />
              </div>
            )}

            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">API 密钥</label>
              <input
                type="password"
                value={formData.api_key}
                onChange={(e) => setFormData((d) => ({ ...d, api_key: e.target.value }))}
                placeholder={editingId ? '(留空则保持当前密钥)' : 'sk-...'}
                className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent"
              />
            </div>

            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">默认模型 *</label>
              {suggestions.length > 0 ? (
                <div className="flex gap-2">
                  <input
                    type="text"
                    value={formData.default_model}
                    onChange={(e) => setFormData((d) => ({ ...d, default_model: e.target.value }))}
                    placeholder="模型名称"
                    className="flex-1 px-3 py-2 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent"
                    list="model-suggestions"
                  />
                  <datalist id="model-suggestions">
                    {suggestions.map((s) => (
                      <option key={s} value={s} />
                    ))}
                  </datalist>
                </div>
              ) : (
                <input
                  type="text"
                  value={formData.default_model}
                  onChange={(e) => setFormData((d) => ({ ...d, default_model: e.target.value }))}
                  placeholder="Model name"
                  className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent"
                />
              )}
              {suggestions.length > 0 && (
                <div className="flex gap-1.5 mt-1.5">
                  {suggestions.map((s) => (
                    <button
                      key={s}
                      type="button"
                      onClick={() => setFormData((d) => ({ ...d, default_model: s }))}
                      className="px-2 py-0.5 text-xs bg-gray-100 text-gray-600 rounded hover:bg-gray-200"
                    >
                      {s}
                    </button>
                  ))}
                </div>
              )}
            </div>
          </div>

          {formError && <p className="text-sm text-red-600">{formError}</p>}

          <div className="flex gap-2">
            <button
              onClick={handleSubmit}
              disabled={saving || !formData.name || !formData.default_model}
              className="px-4 py-2 text-sm bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {saving ? '保存中...' : editingId ? '更新' : '创建'}
            </button>
            <button
              onClick={resetForm}
              className="px-4 py-2 text-sm bg-gray-200 text-gray-700 rounded-lg hover:bg-gray-300"
            >
              取消
            </button>
          </div>
        </div>
      )}

      {/* Endpoints Table */}
      {endpoints.length === 0 ? (
        <div className="bg-white rounded-lg border border-gray-200 p-12 text-center">
          <p className="text-sm text-gray-500">尚未配置端点，请添加一个以开始使用。</p>
        </div>
      ) : (
        <div className="space-y-3">
          {endpoints.map((ep) => {
            const tr = testResults[ep.id]
            return (
              <div key={ep.id} className="bg-white rounded-lg border border-gray-200 p-4">
                <div className="flex items-start justify-between mb-2">
                  <div>
                    <h4 className="font-medium text-gray-900 text-sm">{ep.name}</h4>
                    <span className="inline-flex items-center px-2 py-0.5 rounded text-[10px] font-medium bg-blue-50 text-blue-700 mt-1">
                      {ep.provider_type}
                    </span>
                  </div>
                  {tr ? (
                    <span className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-medium ${
                      tr.success ? 'bg-green-50 text-green-700' : 'bg-red-50 text-red-700'
                    }`}>
                      {tr.success ? `正常${tr.latency_ms ? ` ${tr.latency_ms}ms` : ''}` : '失败'}
                    </span>
                  ) : (
                    <span className="text-[10px] text-gray-400">未测试</span>
                  )}
                </div>
                <div className="space-y-1 text-xs text-gray-500 mb-3">
                  {ep.base_url && <div className="truncate"><span className="text-gray-400">地址:</span> {ep.base_url}</div>}
                  <div><span className="text-gray-400">密钥:</span> <span className="font-mono">{ep.api_key_masked}</span></div>
                  <div><span className="text-gray-400">模型:</span> <span className="font-mono text-gray-700">{ep.default_model}</span></div>
                </div>
                <div className="flex gap-2">
                  <button onClick={() => handleTest(ep.id)} disabled={testingId === ep.id}
                    className="flex-1 px-3 py-1.5 text-xs bg-gray-100 text-gray-700 rounded hover:bg-gray-200 disabled:opacity-50">
                    {testingId === ep.id ? '测试中...' : '测试'}
                  </button>
                  <button onClick={() => handleEdit(ep)}
                    className="flex-1 px-3 py-1.5 text-xs bg-blue-50 text-blue-600 rounded hover:bg-blue-100">
                    编辑
                  </button>
                  <button onClick={() => handleDelete(ep.id)}
                    className="flex-1 px-3 py-1.5 text-xs bg-red-50 text-red-600 rounded hover:bg-red-100">
                    删除
                  </button>
                </div>
              </div>
            )
          })}
        </div>
      )}
    </section>
  )
}

// =========================================================================
// Section 2: Task Routing
// =========================================================================

function TaskRoutingSection({
  endpoints,
  tasks,
  onRefresh,
}: {
  endpoints: Endpoint[]
  tasks: TaskConfig[]
  onRefresh: () => void
}) {
  const [localTasks, setLocalTasks] = useState<TaskConfig[]>(tasks)
  const [saving, setSaving] = useState<string | null>(null)
  const [savedTasks, setSavedTasks] = useState<Set<string>>(new Set())

  useEffect(() => {
    setLocalTasks(tasks)
  }, [tasks])

  const updateLocal = useCallback((taskType: string, updates: Partial<TaskConfig>) => {
    setLocalTasks((prev) =>
      prev.map((t) => (t.task_type === taskType ? { ...t, ...updates } : t))
    )
    setSavedTasks((prev) => {
      const next = new Set(prev)
      next.delete(taskType)
      return next
    })
  }, [])

  const handleSave = useCallback(
    async (taskType: string) => {
      const task = localTasks.find((t) => t.task_type === taskType)
      if (!task) return
      setSaving(taskType)
      try {
        const body: Record<string, unknown> = {
          temperature: task.temperature,
          max_tokens: task.max_tokens,
        }
        if (task.endpoint) {
          body.endpoint_id = task.endpoint.id
        }
        if (task.model_name) {
          body.model_name = task.model_name
        }
        await apiFetch(`/api/model-config/tasks/${taskType}`, {
          method: 'PUT',
          body: JSON.stringify(body),
        })
        setSavedTasks((prev) => new Set(prev).add(taskType))
        onRefresh()
      } catch (err) {
        alert(err instanceof Error ? err.message : '保存失败')
      } finally {
        setSaving(null)
      }
    },
    [localTasks, onRefresh]
  )

  return (
    <section>
      <div className="mb-4">
        <h2 className="text-lg font-semibold text-gray-900">任务路由</h2>
        <p className="text-sm text-gray-500 mt-1">
          为每种任务类型分配端点和模型
        </p>
      </div>

      <div className="space-y-3">
        {localTasks.map((task) => (
          <div key={task.task_type} className="bg-white rounded-lg border border-gray-200 p-4">
            <div className="flex items-center justify-between mb-3">
              <div>
                <div className="font-medium text-gray-900 text-sm">
                  {TASK_LABELS[task.task_type] || task.task_type}
                </div>
                <div className="text-[10px] text-gray-400">{task.task_type}</div>
              </div>
              <button
                onClick={() => handleSave(task.task_type)}
                disabled={saving === task.task_type}
                className={`px-3 py-1.5 text-xs rounded ${
                  savedTasks.has(task.task_type)
                    ? 'bg-green-50 text-green-700'
                    : 'bg-blue-600 text-white hover:bg-blue-700'
                } disabled:opacity-50`}
              >
                {saving === task.task_type ? '保存中...' : savedTasks.has(task.task_type) ? '已保存' : '保存'}
              </button>
            </div>
            <div className="space-y-2">
              <div>
                <label className="block text-xs text-gray-500 mb-1">选择端点</label>
                <select
                  value={task.endpoint?.id || ''}
                  onChange={(e) => {
                    const ep = endpoints.find((x) => x.id === e.target.value) || null
                    updateLocal(task.task_type, { endpoint: ep })
                  }}
                  className="w-full px-2 py-1.5 text-sm border border-gray-200 rounded-lg bg-white"
                >
                  <option value="">-- 未分配 --</option>
                  {endpoints.map((ep) => (
                    <option key={ep.id} value={ep.id}>{ep.name} ({ep.provider_type})</option>
                  ))}
                </select>
              </div>
              <div>
                <label className="block text-xs text-gray-500 mb-1">模型覆盖</label>
                <input type="text" value={task.model_name}
                  onChange={(e) => updateLocal(task.task_type, { model_name: e.target.value })}
                  placeholder={task.endpoint?.default_model || '使用端点默认模型'}
                  className="w-full px-2 py-1.5 text-sm border border-gray-200 rounded-lg" />
              </div>
              <div className="flex gap-3">
                <div className="flex-1">
                  <label className="block text-xs text-gray-500 mb-1">创造性: {task.temperature}</label>
                  <input type="range" min="0" max="1" step="0.1" value={task.temperature}
                    onChange={(e) => updateLocal(task.task_type, { temperature: parseFloat(e.target.value) })}
                    className="w-full" />
                </div>
                <div className="w-24">
                  <label className="block text-xs text-gray-500 mb-1">最大长度</label>
                  <input type="number" value={task.max_tokens}
                    onChange={(e) => updateLocal(task.task_type, { max_tokens: parseInt(e.target.value) || 0 })}
                    className="w-full px-2 py-1.5 text-sm border border-gray-200 rounded-lg" min={1} max={65536} />
                </div>
              </div>
            </div>
          </div>
        ))}
      </div>

      {endpoints.length === 0 && (
        <p className="text-sm text-amber-600 mt-2">
          请先在上方添加至少一个端点，然后再分配任务。
        </p>
      )}
    </section>
  )
}

// =========================================================================
// Section 3: Quick Setup / Presets
// =========================================================================

function PresetsSection({
  presets,
  endpoints,
  onRefresh,
}: {
  presets: Preset[]
  endpoints: Endpoint[]
  onRefresh: () => void
}) {
  const [applying, setApplying] = useState<string | null>(null)

  const handleApplyPreset = useCallback(
    async (preset: Preset) => {
      if (endpoints.length === 0) {
        alert('请先添加至少一个端点，然后再应用预设。')
        return
      }

      const targetEndpoint = endpoints[0]
      if (!confirm(`使用端点"${targetEndpoint.name}"应用"${preset.description}"？这将覆盖所有当前任务分配。`)) {
        return
      }

      setApplying(preset.name)
      try {
        for (const [taskType, config] of Object.entries(preset.tasks)) {
          await apiFetch(`/api/model-config/tasks/${taskType}`, {
            method: 'PUT',
            body: JSON.stringify({
              endpoint_id: targetEndpoint.id,
              model_name: config.model_name,
              temperature: config.temperature,
              max_tokens: config.max_tokens,
            }),
          })
        }
        onRefresh()
      } catch (err) {
        alert(err instanceof Error ? err.message : '应用预设失败')
      } finally {
        setApplying(null)
      }
    },
    [endpoints, onRefresh]
  )

  const presetStyles: Record<string, { label: string; desc: string; color: string }> = {
    cloud_anthropic: {
      label: '纯云端 (Anthropic)',
      desc: '所有任务使用同一个 Anthropic 端点',
      color: 'border-purple-200 hover:border-purple-400',
    },
    cloud_openai: {
      label: '纯云端 (OpenAI)',
      desc: '所有任务使用同一个 OpenAI 端点',
      color: 'border-green-200 hover:border-green-400',
    },
    hybrid: {
      label: '混合模式',
      desc: '生成用云端，提取用本地，向量化独立',
      color: 'border-blue-200 hover:border-blue-400',
    },
    local_only: {
      label: '纯本地',
      desc: '所有任务使用同一个 OpenAI 兼容的本地端点',
      color: 'border-orange-200 hover:border-orange-400',
    },
  }

  return (
    <section>
      <div className="mb-4">
        <h2 className="text-lg font-semibold text-gray-900">快速设置</h2>
        <p className="text-sm text-gray-500 mt-1">
          应用预设快速配置所有任务，需要至少一个端点。
        </p>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
        {presets.map((preset) => {
          const style = presetStyles[preset.name] || {
            label: preset.name,
            desc: preset.description,
            color: 'border-gray-200 hover:border-gray-400',
          }
          return (
            <button
              key={preset.name}
              onClick={() => handleApplyPreset(preset)}
              disabled={applying !== null || endpoints.length === 0}
              className={`bg-white rounded-lg border-2 ${style.color} p-5 text-left transition-colors disabled:opacity-50 disabled:cursor-not-allowed`}
            >
              <h3 className="text-sm font-semibold text-gray-900 mb-1">{style.label}</h3>
              <p className="text-xs text-gray-500">{style.desc}</p>
              {applying === preset.name && (
                <p className="text-xs text-blue-600 mt-2">应用中...</p>
              )}
            </button>
          )
        })}
      </div>
    </section>
  )
}
