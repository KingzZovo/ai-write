'use client'

import { useState, useEffect, useCallback } from 'react'
import { apiFetch, getToken } from '@/lib/api'

interface Endpoint {
  id: string
  name: string
  provider_type: string
  default_model: string
  tier: string
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
  model_tier: string | null
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
  // v1.4.1 — per-task-type endpoint recommendation (chat vs embedding).
  recommendation?: {
    kind: 'chat' | 'embedding' | string
    tier: string
    reason: string
  } | null
}

const MODE_LABELS: Record<string, string> = { text: '文本', structured: '结构化(JSON)' }

// v1.4 — LLM tier enum (matches backend).
const TIER_OPTIONS = [
  { value: '', label: '— 继承端点 —' },
  { value: 'flagship', label: 'Flagship' },
  { value: 'standard', label: 'Standard' },
  { value: 'small', label: 'Small' },
  { value: 'distill', label: 'Distill' },
  { value: 'embedding', label: 'Embedding' },
]
const TIER_BADGE_CLASS: Record<string, string> = {
  flagship: 'bg-purple-50 text-purple-700',
  standard: 'bg-blue-50 text-blue-700',
  small: 'bg-gray-100 text-gray-700',
  distill: 'bg-amber-50 text-amber-700',
  embedding: 'bg-emerald-50 text-emerald-700',
}

// v1.5.0 B2 — recommendation mismatch soft-guard payload (HTTP 409 detail).
interface RecommendationMismatchPayload {
  code?: 'recommendation_mismatch' | string
  task_type?: string
  recommended_kind?: string
  recommended_tier?: string
  recommendation_reason?: string
  current_kind?: string
  current_tier?: string
  endpoint_name?: string
  endpoint_tier?: string | null
  prompt_model_tier?: string | null
  kind_mismatch?: boolean
  tier_mismatch?: boolean
}

/**
 * v1.5.0 B2 — raw fetch wrapper for prompt POST/PUT that surfaces 409 mismatch
 * payloads instead of swallowing them in a generic Error. Re-implements the
 * minimal subset of `apiFetch` we need (auth header + JSON parsing). The
 * standard `apiFetch` stringifies error.detail into a plain Error, which
 * loses the structured fields we need to render the confirm dialog.
 */
async function savePromptWithGuard(
  path: string,
  method: 'POST' | 'PUT',
  body: Record<string, unknown>,
): Promise<{ ok: true } | { ok: false; mismatch: RecommendationMismatchPayload }> {
  const trySave = async (confirmMismatch: boolean) => {
    const url = confirmMismatch ? `${path}?confirm_mismatch=true` : path
    const headers: Record<string, string> = { 'Content-Type': 'application/json' }
    const token = getToken()
    if (token) headers['Authorization'] = `Bearer ${token}`
    const res = await fetch(url, {
      method,
      headers,
      body: JSON.stringify(body),
    })
    return res
  }

  let res = await trySave(false)
  if (res.status === 409) {
    const data = await res.json().catch(() => ({}))
    const detail = (data?.detail ?? {}) as RecommendationMismatchPayload
    if (detail?.code === 'recommendation_mismatch') {
      const recDesc = detail.recommended_kind === 'embedding'
        ? 'embedding 端点'
        : `思考·${detail.recommended_tier ?? ''}`
      const curDesc = detail.current_kind === 'embedding'
        ? 'embedding 端点'
        : `思考·${detail.current_tier ?? ''}`
      const msg =
        `警告：当前绑定的端点与推荐不一致。\n\n` +
        `任务类型：${detail.task_type ?? '?'}\n` +
        `推荐：${recDesc}\n` +
        `当前：${curDesc}·${detail.endpoint_name ?? '?'}\n` +
        (detail.recommendation_reason ? `原因：${detail.recommendation_reason}\n` : '') +
        `\n仍要保存吗？`
      if (!window.confirm(msg)) {
        return { ok: false, mismatch: detail }
      }
      res = await trySave(true)
    }
  }
  if (res.status === 401) {
    throw new Error('Unauthorized')
  }
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    const msg = typeof err.detail === 'string' ? err.detail : 'API Error'
    throw new Error(msg)
  }
  return { ok: true }
}

export default function PromptsPage() {
  const [prompts, setPrompts] = useState<PromptAsset[]>([])
  const [endpoints, setEndpoints] = useState<Endpoint[]>([])
  const [loading, setLoading] = useState(true)
  const [editing, setEditing] = useState<PromptAsset | null>(null)
  const [showCreate, setShowCreate] = useState(false)
  // v1.4 — filter by endpoint (empty string = all).
  const [filterEndpointId, setFilterEndpointId] = useState<string>('')

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
  // v1.4 — apply endpoint filter before grouping.
  const visiblePrompts = filterEndpointId
    ? prompts.filter(p => (p.endpoint_id || '') === filterEndpointId)
    : prompts
  const grouped = visiblePrompts.reduce<Record<string, PromptAsset[]>>((acc, p) => {
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

      {/* v1.4 — endpoint filter bar */}
      <div className="flex items-center gap-2 mb-4 text-xs text-gray-500">
        <span>端点过滤：</span>
        <select
          data-testid="prompt-endpoint-filter"
          value={filterEndpointId}
          onChange={e => setFilterEndpointId(e.target.value)}
          className="px-2 py-1 text-xs border border-gray-200 rounded bg-white"
        >
          <option value="">全部 ({prompts.length})</option>
          {endpoints.map(ep => {
            const count = prompts.filter(p => p.endpoint_id === ep.id).length
            return (
              <option key={ep.id} value={ep.id}>
                {ep.name} · {ep.tier || 'standard'} ({count})
              </option>
            )
          })}
        </select>
        {filterEndpointId && (
          <button
            onClick={() => setFilterEndpointId('')}
            className="text-[11px] text-gray-400 hover:text-gray-600 underline"
          >清除</button>
        )}
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
                          {/* v1.4 — model_tier badge (inherits endpoint.tier when null) */}
                          {(() => {
                            const effTier = p.model_tier || endpoint?.tier || 'standard'
                            const overridden = !!p.model_tier && !!endpoint && p.model_tier !== endpoint.tier
                            return (
                              <span
                                data-testid="prompt-tier-badge"
                                data-tier={effTier}
                                data-overridden={overridden ? '1' : '0'}
                                className={`text-[10px] px-1.5 py-0.5 rounded ${TIER_BADGE_CLASS[effTier] || 'bg-gray-100 text-gray-700'}`}
                                title={p.model_tier ? `模型等级覆盖 = ${p.model_tier}` : `继承端点等级 = ${effTier}`}
                              >
                                {effTier}{overridden ? '*' : ''}
                              </span>
                            )
                          })()}
                          {/* v1.4.1 — per-task-type endpoint recommendation badge.
                              Tells the operator whether this prompt should be
                              bound to a thinking (chat) endpoint or an embedding
                              endpoint, and if chat, at what tier. */}
                          {p.recommendation && (() => {
                            const rec = p.recommendation!
                            const isEmbedding = rec.kind === 'embedding'
                            const label = isEmbedding
                              ? '建议 embedding'
                              : `建议 思考·${rec.tier}`
                            const cls = isEmbedding
                              ? 'border border-emerald-300 text-emerald-700 bg-white'
                              : rec.tier === 'flagship'
                              ? 'border border-purple-300 text-purple-700 bg-white'
                              : rec.tier === 'standard'
                              ? 'border border-blue-300 text-blue-700 bg-white'
                              : rec.tier === 'distill'
                              ? 'border border-amber-300 text-amber-700 bg-white'
                              : 'border border-gray-300 text-gray-600 bg-white'
                            return (
                              <span
                                data-testid="prompt-recommendation-badge"
                                data-recommendation-kind={rec.kind}
                                data-recommendation-tier={rec.tier}
                                className={`text-[10px] px-1.5 py-0.5 rounded ${cls}`}
                                title={rec.reason}
                              >
                                {label}
                              </span>
                            )
                          })()}
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
  // v1.4 — model_tier override (empty string = inherit from endpoint).
  const [modelTier, setModelTier] = useState<string>(prompt?.model_tier || '')
  const [temperature, setTemperature] = useState(prompt?.temperature ?? 0.7)
  const [maxTokens, setMaxTokens] = useState(prompt?.max_tokens ?? 4096)
  const [saving, setSaving] = useState(false)

  const handleSave = async () => {
    if (!taskType.trim() || !name.trim() || !systemPrompt.trim()) return
    setSaving(true)
    try {
      if (isEdit) {
        const r = await savePromptWithGuard(`/api/prompts/${prompt.id}`, 'PUT', {
          name, description, system_prompt: systemPrompt, user_template: userTemplate,
          category, endpoint_id: endpointId || null, model_name: modelName,
          model_tier: modelTier || null,
          temperature, max_tokens: maxTokens,
        })
        if (!r.ok) { setSaving(false); return }
      } else {
        const r = await savePromptWithGuard('/api/prompts', 'POST', {
          task_type: taskType, name, description, mode,
          system_prompt: systemPrompt, user_template: userTemplate,
          category, endpoint_id: endpointId || null, model_name: modelName,
          model_tier: modelTier || null,
          temperature, max_tokens: maxTokens,
        })
        if (!r.ok) { setSaving(false); return }
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

      {/* v1.4 — model_tier override */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        <div>
          <label className="block text-xs font-medium text-gray-600 mb-1">模型等级覆盖</label>
          <select
            data-testid="prompt-model-tier-select"
            value={modelTier}
            onChange={e => setModelTier(e.target.value)}
            className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg bg-white"
          >
            {TIER_OPTIONS.map(opt => (
              <option key={opt.value} value={opt.value}>{opt.label}</option>
            ))}
          </select>
          <p className="mt-1 text-[11px] text-gray-500">
            留空表示继承端点等级；显式指定后以此为准路由。
          </p>
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
