'use client'

import { useState, useEffect, useCallback, useMemo } from 'react'
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
  recommendation?: {
    kind: 'chat' | 'embedding' | string
    tier: string
    reason: string
  } | null
}

interface MatrixRow {
  task_type: string
  mode: string
  prompt_id: string
  prompt_name: string
  endpoint_id: string | null
  endpoint_name: string | null
  endpoint_tier: string | null
  model_name: string | null
  model_tier: string | null
  effective_tier: string
  overridden: boolean
}

const MODE_LABELS: Record<string, string> = { text: '文本', structured: '结构化(JSON)' }

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
    return fetch(url, { method, headers, body: JSON.stringify(body) })
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
        `推荐：${recDesc}\n当前：${curDesc}·${detail.endpoint_name ?? '?'}\n` +
        (detail.recommendation_reason ? `原因：${detail.recommendation_reason}\n` : '') +
        `\n仍要保存吗？`
      if (!window.confirm(msg)) return { ok: false, mismatch: detail }
      res = await trySave(true)
    }
  }
  if (res.status === 401) throw new Error('Unauthorized')
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error(typeof err.detail === 'string' ? err.detail : 'API Error')
  }
  return { ok: true }
}


function TierBadge({ tier }: { tier: string | null | undefined }) {
  if (!tier) return <span className="text-[10px] text-gray-400">—</span>
  const cls = TIER_BADGE_CLASS[tier] ?? 'bg-gray-100 text-gray-700'
  return <span className={`text-[10px] px-1.5 py-0.5 rounded-full font-medium ${cls}`}>{tier}</span>
}

export default function PromptsPage() {
  const [prompts, setPrompts] = useState<PromptAsset[]>([])
  const [endpoints, setEndpoints] = useState<Endpoint[]>([])
  const [loading, setLoading] = useState(true)
  const [editing, setEditing] = useState<PromptAsset | null>(null)
  const [showCreate, setShowCreate] = useState(false)
  const [expandedId, setExpandedId] = useState<string | null>(null)
  const [filterEndpointId, setFilterEndpointId] = useState<string>('')
  const [view, setView] = useState<'list' | 'matrix'>('list')
  const [matrixRows, setMatrixRows] = useState<MatrixRow[]>([])
  const [matrixLoading, setMatrixLoading] = useState(false)
  const [tierFilter, setTierFilter] = useState<string>('')

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

  const fetchMatrix = useCallback(async (tier: string) => {
    setMatrixLoading(true)
    try {
      const qs = tier ? `?tier=${encodeURIComponent(tier)}` : ''
      const data = await apiFetch<{ rows: MatrixRow[] }>(`/api/llm-routing/matrix${qs}`)
      setMatrixRows(Array.isArray(data?.rows) ? data.rows : [])
    } catch { setMatrixRows([]) }
    finally { setMatrixLoading(false) }
  }, [])

  useEffect(() => { fetchPrompts(); fetchEndpoints() }, [fetchPrompts, fetchEndpoints])
  useEffect(() => { if (view === 'matrix') fetchMatrix(tierFilter) }, [view, tierFilter, fetchMatrix])

  const handleDelete = async (id: string) => {
    if (!confirm('确定删除此 Prompt？')) return
    await apiFetch(`/api/prompts/${id}`, { method: 'DELETE' })
    fetchPrompts()
  }

  const patchField = async (id: string, patch: Partial<PromptAsset>) => {
    await apiFetch(`/api/prompts/${id}`, { method: 'PUT', body: JSON.stringify(patch) })
    fetchPrompts()
  }

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

  const matrixGrouped = useMemo(() => {
    const order: string[] = []
    const map = new Map<string, MatrixRow[]>()
    for (const r of matrixRows) {
      if (!map.has(r.task_type)) { order.push(r.task_type); map.set(r.task_type, []) }
      map.get(r.task_type)!.push(r)
    }
    return order.map(t => ({ task_type: t, rows: map.get(t)! }))
  }, [matrixRows])

  return (
    <div className="pt-14 px-4 md:px-8 max-w-5xl mx-auto pb-12">
      {/* Header */}
      <div className="flex items-end justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold text-gray-900 tracking-tight">Prompt 管理</h1>
          <p className="text-sm text-gray-500 mt-1">注册表 + 路由矩阵</p>
        </div>
        <button onClick={() => { setEditing(null); setShowCreate(true) }}
          className="px-4 py-2 text-sm bg-gray-900 text-white rounded-lg hover:bg-gray-800">
          + 新建
        </button>
      </div>

      {/* View toggle + filters */}
      <div className="flex items-center gap-3 mb-4 flex-wrap">
        <div className="inline-flex rounded-lg border border-gray-200 overflow-hidden text-sm">
          <button
            onClick={() => setView('list')}
            className={`px-3 py-1.5 ${view === 'list' ? 'bg-gray-900 text-white' : 'bg-white text-gray-600 hover:bg-gray-50'}`}
          >列表</button>
          <button
            onClick={() => setView('matrix')}
            className={`px-3 py-1.5 ${view === 'matrix' ? 'bg-gray-900 text-white' : 'bg-white text-gray-600 hover:bg-gray-50'}`}
          >路由矩阵</button>
        </div>

        {view === 'list' && (
          <select
            value={filterEndpointId}
            onChange={e => setFilterEndpointId(e.target.value)}
            className="px-2 py-1.5 text-xs border border-gray-200 rounded-lg bg-white"
          >
            <option value="">全部端点 ({prompts.length})</option>
            {endpoints.map(ep => (
              <option key={ep.id} value={ep.id}>
                {ep.name} · {ep.tier || 'standard'} ({prompts.filter(p => p.endpoint_id === ep.id).length})
              </option>
            ))}
          </select>
        )}

        {view === 'matrix' && (
          <select
            value={tierFilter}
            onChange={e => setTierFilter(e.target.value)}
            className="px-2 py-1.5 text-xs border border-gray-200 rounded-lg bg-white"
          >
            <option value="">全部等级</option>
            <option value="flagship">旗舰</option>
            <option value="standard">常规</option>
            <option value="small">轻量</option>
            <option value="distill">蒸馏</option>
            <option value="embedding">向量嵌入</option>
          </select>
        )}
      </div>

      {/* Create/Edit form */}
      {(showCreate || editing) && (
        <PromptForm
          prompt={editing}
          endpoints={endpoints}
          onClose={() => { setShowCreate(false); setEditing(null) }}
          onSaved={() => { setShowCreate(false); setEditing(null); fetchPrompts() }}
        />
      )}


      {/* LIST VIEW */}
      {view === 'list' && (
        loading ? (
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
                <div className="bg-white rounded-xl border border-gray-200 divide-y divide-gray-100">
                  {assets.map(p => {
                    const isExpanded = expandedId === p.id
                    const endpoint = endpoints.find(e => e.id === p.endpoint_id)
                    const effTier = p.model_tier || endpoint?.tier || 'standard'
                    return (
                      <div key={p.id} className={`${!p.is_active ? 'opacity-50' : ''}`}>
                        {/* Collapsed row */}
                        <div
                          className="px-5 py-3 flex items-center justify-between cursor-pointer hover:bg-gray-50"
                          onClick={() => setExpandedId(isExpanded ? null : p.id)}
                        >
                          <div className="flex items-center gap-2 flex-wrap min-w-0">
                            <span className="text-sm font-medium text-gray-800 truncate">{p.name}</span>
                            <code className="text-[10px] px-1 py-0.5 bg-gray-100 text-gray-500 rounded">{p.task_type}</code>
                            <TierBadge tier={effTier} />
                            {endpoint && <span className="text-[10px] text-gray-400">{endpoint.name}</span>}
                            {!p.is_active && <span className="text-[10px] px-1.5 py-0.5 bg-gray-100 text-gray-400 rounded-full">历史</span>}
                          </div>
                          <svg className={`w-4 h-4 text-gray-400 shrink-0 transition-transform ${isExpanded ? 'rotate-180' : ''}`} fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" /></svg>
                        </div>

                        {/* Expanded detail */}
                        {isExpanded && (
                          <div className="px-5 pb-4 pt-1 border-t border-gray-100 space-y-3">
                            <div className="flex items-center gap-2 flex-wrap text-[10px]">
                              <span className="px-1.5 py-0.5 bg-blue-50 text-blue-600 rounded">{MODE_LABELS[p.mode] || p.mode}</span>
                              <span className="px-1.5 py-0.5 bg-gray-100 text-gray-500 rounded">v{p.version}</span>
                              {p.always_enabled === 1 && <span className="px-1.5 py-0.5 bg-amber-50 text-amber-700 rounded">always-on</span>}
                              {p.recommendation && (
                                <span className="px-1.5 py-0.5 border border-purple-300 text-purple-700 rounded" title={p.recommendation.reason}>
                                  建议 {p.recommendation.kind === 'embedding' ? 'embedding' : `思考·${p.recommendation.tier}`}
                                </span>
                              )}
                              {(p.success_count + p.fail_count) > 0 && (
                                <span className="text-gray-400">
                                  调用 {p.success_count + p.fail_count} 次 · 成功率 {Math.round(p.success_count / (p.success_count + p.fail_count) * 100)}%
                                </span>
                              )}
                            </div>

                            {p.description && <p className="text-xs text-gray-500">{p.description}</p>}

                            {/* Routing controls */}
                            <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
                              <div>
                                <label className="block text-[10px] text-gray-400 mb-0.5">端点</label>
                                <select
                                  value={p.endpoint_id || ''}
                                  onChange={e => patchField(p.id, { endpoint_id: e.target.value || null })}
                                  className="w-full px-2 py-1 text-xs border border-gray-200 rounded bg-white"
                                >
                                  <option value="">未分配</option>
                                  {endpoints.map(ep => <option key={ep.id} value={ep.id}>{ep.name}</option>)}
                                </select>
                              </div>
                              <div>
                                <label className="block text-[10px] text-gray-400 mb-0.5">模型覆盖</label>
                                <input
                                  type="text" defaultValue={p.model_name}
                                  placeholder={endpoint?.default_model || '默认'}
                                  onBlur={e => { if (e.target.value !== p.model_name) patchField(p.id, { model_name: e.target.value }) }}
                                  className="w-full px-2 py-1 text-xs border border-gray-200 rounded"
                                />
                              </div>
                              <div>
                                <label className="block text-[10px] text-gray-400 mb-0.5">温度 {p.temperature?.toFixed(1)}</label>
                                <input
                                  type="range" min="0" max="1" step="0.1" defaultValue={p.temperature}
                                  onMouseUp={e => { const v = parseFloat((e.target as HTMLInputElement).value); if (v !== p.temperature) patchField(p.id, { temperature: v }) }}
                                  className="w-full"
                                />
                              </div>
                              <div>
                                <label className="block text-[10px] text-gray-400 mb-0.5">最大长度</label>
                                <input
                                  type="number" defaultValue={p.max_tokens} min={1} max={131072}
                                  onBlur={e => { const v = parseInt(e.target.value) || 4096; if (v !== p.max_tokens) patchField(p.id, { max_tokens: v }) }}
                                  className="w-full px-2 py-1 text-xs border border-gray-200 rounded"
                                />
                              </div>
                            </div>

                            {/* System prompt preview */}
                            <details className="group">
                              <summary className="text-[10px] text-gray-400 cursor-pointer hover:text-gray-600">System Prompt</summary>
                              <pre className="mt-1 text-[11px] text-gray-600 whitespace-pre-wrap font-mono bg-gray-50 rounded p-2 max-h-40 overflow-auto">{p.system_prompt}</pre>
                            </details>

                            <div className="flex gap-1.5 pt-1">
                              <button onClick={() => setEditing(p)} className="px-2.5 py-1 text-xs bg-gray-100 text-gray-700 rounded hover:bg-gray-200">编辑</button>
                              <button onClick={() => handleDelete(p.id)} className="px-2.5 py-1 text-xs bg-red-50 text-red-600 rounded hover:bg-red-100 ml-auto">删除</button>
                            </div>
                          </div>
                        )}
                      </div>
                    )
                  })}
                </div>
              </section>
            ))}
          </div>
        )
      )}

      {/* MATRIX VIEW */}
      {view === 'matrix' && (
        matrixLoading ? (
          <p className="text-sm text-gray-400 text-center py-16">加载中...</p>
        ) : matrixRows.length === 0 ? (
          <p className="text-sm text-gray-400 text-center py-16">没有匹配的路由记录</p>
        ) : (
          <div className="bg-white rounded-xl border border-gray-200 overflow-hidden">
            {matrixGrouped.map((group, gi) => (
              <div key={group.task_type} className={gi > 0 ? 'border-t border-gray-200' : ''}>
                <div className="bg-gray-50 px-4 py-2 text-sm font-medium text-gray-700">
                  {group.task_type} <span className="text-xs text-gray-400">({group.rows.length})</span>
                </div>
                <table className="w-full text-sm">
                  <thead className="text-left text-xs text-gray-500 bg-white">
                    <tr>
                      <th className="px-4 py-2 font-medium">模式</th>
                      <th className="px-4 py-2 font-medium">Prompt</th>
                      <th className="px-4 py-2 font-medium">端点</th>
                      <th className="px-4 py-2 font-medium">模型</th>
                      <th className="px-4 py-2 font-medium">生效等级</th>
                    </tr>
                  </thead>
                  <tbody>
                    {group.rows.map(r => (
                      <tr key={`${r.task_type}:${r.mode}:${r.prompt_id}`} className="border-t border-gray-50">
                        <td className="px-4 py-2 text-gray-700">{MODE_LABELS[r.mode] ?? r.mode}</td>
                        <td className="px-4 py-2 text-gray-900 font-medium">{r.prompt_name}</td>
                        <td className="px-4 py-2">
                          {r.endpoint_name ? (
                            <span className="text-gray-700">{r.endpoint_name} <TierBadge tier={r.endpoint_tier} /></span>
                          ) : <span className="text-gray-400">—</span>}
                        </td>
                        <td className="px-4 py-2 text-gray-700">{r.model_name || <span className="text-gray-400">—</span>}</td>
                        <td className="px-4 py-2">
                          <TierBadge tier={r.effective_tier} />
                          {r.overridden && <span className="ml-1 text-xs text-amber-600 font-semibold">*</span>}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ))}
          </div>
        )
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
          model_tier: modelTier || null, temperature, max_tokens: maxTokens,
        })
        if (!r.ok) { setSaving(false); return }
      } else {
        const r = await savePromptWithGuard('/api/prompts', 'POST', {
          task_type: taskType, name, description, mode,
          system_prompt: systemPrompt, user_template: userTemplate,
          category, endpoint_id: endpointId || null, model_name: modelName,
          model_tier: modelTier || null, temperature, max_tokens: maxTokens,
        })
        if (!r.ok) { setSaving(false); return }
      }
      onSaved()
    } catch (e) {
      alert(e instanceof Error ? e.message : '保存失败')
    } finally { setSaving(false) }
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
          <input value={taskType} onChange={e => setTaskType(e.target.value)} disabled={isEdit}
            placeholder="如 outline_book, rewrite_emotion"
            className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg disabled:bg-gray-50" />
        </div>
        <div>
          <label className="block text-xs font-medium text-gray-600 mb-1">名称 *</label>
          <input value={name} onChange={e => setName(e.target.value)}
            className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg" />
        </div>
        <div>
          <label className="block text-xs font-medium text-gray-600 mb-1">分类</label>
          <input value={category} onChange={e => setCategory(e.target.value)}
            placeholder="核心 / 大纲 / 提取 / 编辑"
            className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg" />
        </div>
      </div>

      <div>
        <label className="block text-xs font-medium text-gray-600 mb-1">描述</label>
        <input value={description} onChange={e => setDescription(e.target.value)}
          className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg" />
      </div>

      <div className="grid grid-cols-1 md:grid-cols-4 gap-3">
        <div className="md:col-span-2">
          <label className="block text-xs font-medium text-gray-600 mb-1">端点</label>
          <select value={endpointId} onChange={e => setEndpointId(e.target.value)}
            className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg bg-white">
            <option value="">-- 未分配 --</option>
            {endpoints.map(ep => <option key={ep.id} value={ep.id}>{ep.name} ({ep.provider_type})</option>)}
          </select>
        </div>
        <div>
          <label className="block text-xs font-medium text-gray-600 mb-1">模型覆盖</label>
          <input value={modelName} onChange={e => setModelName(e.target.value)}
            placeholder="留空用默认"
            className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg" />
        </div>
        <div>
          <label className="block text-xs font-medium text-gray-600 mb-1">模式</label>
          <select value={mode} onChange={e => setMode(e.target.value)} disabled={isEdit}
            className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg disabled:bg-gray-50">
            <option value="text">文本</option>
            <option value="structured">结构化 JSON</option>
          </select>
        </div>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
        <div>
          <label className="block text-xs font-medium text-gray-600 mb-1">模型等级</label>
          <select value={modelTier} onChange={e => setModelTier(e.target.value)}
            className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg bg-white">
            {TIER_OPTIONS.map(opt => <option key={opt.value} value={opt.value}>{opt.label}</option>)}
          </select>
        </div>
        <div>
          <label className="block text-xs font-medium text-gray-600 mb-1">温度: {temperature.toFixed(1)}</label>
          <input type="range" min="0" max="1" step="0.1" value={temperature}
            onChange={e => setTemperature(parseFloat(e.target.value))} className="w-full" />
        </div>
        <div>
          <label className="block text-xs font-medium text-gray-600 mb-1">最大长度</label>
          <input type="number" value={maxTokens} min={1} max={131072}
            onChange={e => setMaxTokens(parseInt(e.target.value) || 4096)}
            className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg" />
        </div>
      </div>

      <div>
        <label className="block text-xs font-medium text-gray-600 mb-1">System Prompt *</label>
        <textarea value={systemPrompt} onChange={e => setSystemPrompt(e.target.value)}
          className="w-full h-36 px-3 py-2 text-sm border border-gray-200 rounded-lg font-mono resize-none" />
      </div>

      <div>
        <label className="block text-xs font-medium text-gray-600 mb-1">User Template（可选）</label>
        <textarea value={userTemplate} onChange={e => setUserTemplate(e.target.value)}
          className="w-full h-20 px-3 py-2 text-sm border border-gray-200 rounded-lg font-mono resize-none" />
      </div>

      <div className="flex gap-3">
        <button onClick={handleSave}
          disabled={saving || !taskType.trim() || !systemPrompt.trim()}
          className="px-5 py-2 text-sm bg-gray-900 text-white rounded-lg disabled:opacity-50">
          {saving ? '保存中...' : isEdit ? '更新' : '创建'}
        </button>
        {!isEdit && <p className="text-xs text-gray-400 self-center">创建会自动停用同任务类型的旧版本</p>}
      </div>
    </div>
  )
}
