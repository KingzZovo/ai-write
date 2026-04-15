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
  { value: 'openai_compatible', label: 'OpenAI Compatible' },
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
  summary: '\u6458\u8981\u63D0\u53D6',
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
      setError(err instanceof Error ? err.message : 'Failed to load settings')
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
          <p className="text-sm text-gray-500">Loading model configuration...</p>
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
      setFormError(err instanceof Error ? err.message : 'Save failed')
    } finally {
      setSaving(false)
    }
  }, [formData, editingId, resetForm, onRefresh])

  const handleDelete = useCallback(
    async (id: string) => {
      if (!confirm('Delete this endpoint? Task configs referencing it will lose their assignment.')) return
      try {
        await apiFetch(`/api/model-config/endpoints/${id}`, { method: 'DELETE' })
        onRefresh()
      } catch (err) {
        alert(err instanceof Error ? err.message : 'Delete failed')
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
        [id]: { success: false, message: err instanceof Error ? err.message : 'Test failed', latency_ms: null },
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
          <h2 className="text-lg font-semibold text-gray-900">API Endpoints</h2>
          <p className="text-sm text-gray-500 mt-1">Configure LLM API connections</p>
        </div>
        <button
          onClick={() => {
            resetForm()
            setShowForm(true)
          }}
          className="px-4 py-2 text-sm bg-blue-600 text-white rounded-lg hover:bg-blue-700"
        >
          Add Endpoint
        </button>
      </div>

      {/* Add / Edit Form */}
      {showForm && (
        <div className="bg-white rounded-lg border border-gray-200 p-5 mb-4 space-y-4">
          <h3 className="text-sm font-semibold text-gray-700">
            {editingId ? 'Edit Endpoint' : 'New Endpoint'}
          </h3>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Name *</label>
              <input
                type="text"
                value={formData.name}
                onChange={(e) => setFormData((d) => ({ ...d, name: e.target.value }))}
                placeholder="e.g. Claude API, Local Qwen"
                className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent"
              />
            </div>

            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Provider Type *</label>
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
                <label className="block text-sm font-medium text-gray-700 mb-1">Base URL *</label>
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
              <label className="block text-sm font-medium text-gray-700 mb-1">API Key</label>
              <input
                type="password"
                value={formData.api_key}
                onChange={(e) => setFormData((d) => ({ ...d, api_key: e.target.value }))}
                placeholder={editingId ? '(leave blank to keep current)' : 'sk-...'}
                className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent"
              />
            </div>

            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Default Model *</label>
              {suggestions.length > 0 ? (
                <div className="flex gap-2">
                  <input
                    type="text"
                    value={formData.default_model}
                    onChange={(e) => setFormData((d) => ({ ...d, default_model: e.target.value }))}
                    placeholder="Model name"
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
              {saving ? 'Saving...' : editingId ? 'Update' : 'Create'}
            </button>
            <button
              onClick={resetForm}
              className="px-4 py-2 text-sm bg-gray-200 text-gray-700 rounded-lg hover:bg-gray-300"
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {/* Endpoints Table */}
      {endpoints.length === 0 ? (
        <div className="bg-white rounded-lg border border-gray-200 p-12 text-center">
          <p className="text-sm text-gray-500">No endpoints configured yet. Add one to get started.</p>
        </div>
      ) : (
        <div className="bg-white rounded-lg border border-gray-200 overflow-hidden">
          <table className="w-full text-sm">
            <thead>
              <tr className="bg-gray-50 border-b border-gray-200">
                <th className="text-left px-4 py-3 font-medium text-gray-600">Name</th>
                <th className="text-left px-4 py-3 font-medium text-gray-600">Provider</th>
                <th className="text-left px-4 py-3 font-medium text-gray-600">Base URL</th>
                <th className="text-left px-4 py-3 font-medium text-gray-600">API Key</th>
                <th className="text-left px-4 py-3 font-medium text-gray-600">Default Model</th>
                <th className="text-left px-4 py-3 font-medium text-gray-600">Status</th>
                <th className="text-right px-4 py-3 font-medium text-gray-600">Actions</th>
              </tr>
            </thead>
            <tbody>
              {endpoints.map((ep) => {
                const tr = testResults[ep.id]
                return (
                  <tr key={ep.id} className="border-b border-gray-100 last:border-b-0">
                    <td className="px-4 py-3 font-medium text-gray-900">{ep.name}</td>
                    <td className="px-4 py-3">
                      <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-blue-50 text-blue-700">
                        {ep.provider_type}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-gray-500 truncate max-w-[200px]">
                      {ep.base_url || '-'}
                    </td>
                    <td className="px-4 py-3 text-gray-400 font-mono text-xs">
                      {ep.api_key_masked}
                    </td>
                    <td className="px-4 py-3 text-gray-700 font-mono text-xs">
                      {ep.default_model}
                    </td>
                    <td className="px-4 py-3">
                      {tr ? (
                        <span
                          className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-medium ${
                            tr.success
                              ? 'bg-green-50 text-green-700'
                              : 'bg-red-50 text-red-700'
                          }`}
                          title={tr.message}
                        >
                          {tr.success
                            ? `OK ${tr.latency_ms ? `(${tr.latency_ms}ms)` : ''}`
                            : 'Failed'}
                        </span>
                      ) : (
                        <span className="text-xs text-gray-400">Untested</span>
                      )}
                    </td>
                    <td className="px-4 py-3 text-right space-x-2">
                      <button
                        onClick={() => handleTest(ep.id)}
                        disabled={testingId === ep.id}
                        className="px-3 py-1 text-xs bg-gray-100 text-gray-700 rounded hover:bg-gray-200 disabled:opacity-50"
                      >
                        {testingId === ep.id ? 'Testing...' : 'Test'}
                      </button>
                      <button
                        onClick={() => handleEdit(ep)}
                        className="px-3 py-1 text-xs bg-blue-50 text-blue-600 rounded hover:bg-blue-100"
                      >
                        Edit
                      </button>
                      <button
                        onClick={() => handleDelete(ep.id)}
                        className="px-3 py-1 text-xs bg-red-50 text-red-600 rounded hover:bg-red-100"
                      >
                        Delete
                      </button>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
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
        alert(err instanceof Error ? err.message : 'Save failed')
      } finally {
        setSaving(null)
      }
    },
    [localTasks, onRefresh]
  )

  return (
    <section>
      <div className="mb-4">
        <h2 className="text-lg font-semibold text-gray-900">Task Routing</h2>
        <p className="text-sm text-gray-500 mt-1">
          Assign an endpoint and model to each task type
        </p>
      </div>

      <div className="bg-white rounded-lg border border-gray-200 overflow-hidden">
        <table className="w-full text-sm">
          <thead>
            <tr className="bg-gray-50 border-b border-gray-200">
              <th className="text-left px-4 py-3 font-medium text-gray-600 w-36">Task</th>
              <th className="text-left px-4 py-3 font-medium text-gray-600">Endpoint</th>
              <th className="text-left px-4 py-3 font-medium text-gray-600">Model Override</th>
              <th className="text-left px-4 py-3 font-medium text-gray-600 w-44">Temperature</th>
              <th className="text-left px-4 py-3 font-medium text-gray-600 w-28">Max Tokens</th>
              <th className="text-right px-4 py-3 font-medium text-gray-600 w-24"></th>
            </tr>
          </thead>
          <tbody>
            {localTasks.map((task) => (
              <tr key={task.task_type} className="border-b border-gray-100 last:border-b-0">
                <td className="px-4 py-3">
                  <div className="font-medium text-gray-900">
                    {TASK_LABELS[task.task_type] || task.task_type}
                  </div>
                  <div className="text-xs text-gray-400">{task.task_type}</div>
                </td>
                <td className="px-4 py-3">
                  <select
                    value={task.endpoint?.id || ''}
                    onChange={(e) => {
                      const ep = endpoints.find((ep) => ep.id === e.target.value) || null
                      updateLocal(task.task_type, { endpoint: ep })
                    }}
                    className="w-full px-2 py-1.5 text-sm border border-gray-200 rounded-lg bg-white focus:ring-2 focus:ring-blue-500 focus:border-transparent"
                  >
                    <option value="">-- Not assigned --</option>
                    {endpoints.map((ep) => (
                      <option key={ep.id} value={ep.id}>
                        {ep.name} ({ep.provider_type})
                      </option>
                    ))}
                  </select>
                </td>
                <td className="px-4 py-3">
                  <input
                    type="text"
                    value={task.model_name}
                    onChange={(e) => updateLocal(task.task_type, { model_name: e.target.value })}
                    placeholder={task.endpoint?.default_model || 'Use endpoint default'}
                    className="w-full px-2 py-1.5 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent"
                  />
                </td>
                <td className="px-4 py-3">
                  <div className="flex items-center gap-2">
                    <input
                      type="range"
                      min="0"
                      max="1"
                      step="0.1"
                      value={task.temperature}
                      onChange={(e) =>
                        updateLocal(task.task_type, { temperature: parseFloat(e.target.value) })
                      }
                      className="flex-1"
                    />
                    <span className="text-xs text-gray-500 w-8 text-right">{task.temperature}</span>
                  </div>
                </td>
                <td className="px-4 py-3">
                  <input
                    type="number"
                    value={task.max_tokens}
                    onChange={(e) =>
                      updateLocal(task.task_type, { max_tokens: parseInt(e.target.value) || 0 })
                    }
                    className="w-full px-2 py-1.5 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent"
                    min={1}
                    max={65536}
                  />
                </td>
                <td className="px-4 py-3 text-right">
                  <button
                    onClick={() => handleSave(task.task_type)}
                    disabled={saving === task.task_type}
                    className={`px-3 py-1 text-xs rounded ${
                      savedTasks.has(task.task_type)
                        ? 'bg-green-50 text-green-700'
                        : 'bg-blue-600 text-white hover:bg-blue-700'
                    } disabled:opacity-50`}
                  >
                    {saving === task.task_type
                      ? 'Saving...'
                      : savedTasks.has(task.task_type)
                        ? 'Saved'
                        : 'Save'}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {endpoints.length === 0 && (
        <p className="text-sm text-amber-600 mt-2">
          Add at least one endpoint above before assigning tasks.
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
        alert('Please add at least one endpoint before applying a preset.')
        return
      }

      const targetEndpoint = endpoints[0]
      if (!confirm(`Apply "${preset.description}" using endpoint "${targetEndpoint.name}"? This will overwrite all current task assignments.`)) {
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
        alert(err instanceof Error ? err.message : 'Failed to apply preset')
      } finally {
        setApplying(null)
      }
    },
    [endpoints, onRefresh]
  )

  const presetStyles: Record<string, { label: string; desc: string; color: string }> = {
    cloud_anthropic: {
      label: 'Cloud Only (Anthropic)',
      desc: 'All tasks use the same Anthropic endpoint',
      color: 'border-purple-200 hover:border-purple-400',
    },
    cloud_openai: {
      label: 'Cloud Only (OpenAI)',
      desc: 'All tasks use the same OpenAI endpoint',
      color: 'border-green-200 hover:border-green-400',
    },
    hybrid: {
      label: 'Hybrid',
      desc: 'Generation on cloud, extraction on local, embedding separate',
      color: 'border-blue-200 hover:border-blue-400',
    },
    local_only: {
      label: 'Local Only',
      desc: 'All tasks use one OpenAI-compatible local endpoint',
      color: 'border-orange-200 hover:border-orange-400',
    },
  }

  return (
    <section>
      <div className="mb-4">
        <h2 className="text-lg font-semibold text-gray-900">Quick Setup</h2>
        <p className="text-sm text-gray-500 mt-1">
          Apply a preset to quickly configure all tasks. Requires at least one endpoint.
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
                <p className="text-xs text-blue-600 mt-2">Applying...</p>
              )}
            </button>
          )
        })}
      </div>
    </section>
  )
}
