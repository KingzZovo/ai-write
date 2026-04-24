'use client'

import React, { useEffect, useState, useCallback } from 'react'
import { apiFetch } from '@/lib/api'
import { useT, useLocale } from '@/lib/i18n/I18nProvider'
import { LOCALES, type Locale } from '@/lib/i18n/messages'

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
  tier: string
  enabled: number
  last_test_ok: number
  last_test_latency: number | null
  created_at: string
}

interface TestResult {
  success: boolean
  message: string
  latency_ms: number | null
}

// =========================================================================
// Constants
// =========================================================================

const PROVIDER_OPTIONS = [
  { value: 'anthropic', label: 'Anthropic' },
  { value: 'openai', label: 'OpenAI' },
  { value: 'openai_compatible', label: 'OpenAI 兼容' },
]

// v1.4 — LLM tier enum (matches backend LLMEndpoint.tier + prompt_assets.model_tier).
const TIER_OPTIONS = [
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

const MODEL_SUGGESTIONS: Record<string, string[]> = {
  anthropic: ['claude-sonnet-4-20250514', 'claude-haiku-4-5-20251001'],
  openai: ['gpt-4o', 'gpt-4o-mini', 'text-embedding-3-small'],
  openai_compatible: [],
}

// =========================================================================
// Main Page
// =========================================================================

export default function SettingsPage() {
  const [endpoints, setEndpoints] = useState<Endpoint[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const t = useT()

  const fetchAll = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const epRes = await apiFetch<{ endpoints: Endpoint[]; total: number }>('/api/model-config/endpoints')
      setEndpoints(epRes.endpoints)
    } catch (err) {
      setError(err instanceof Error ? err.message : t('settings.error.loadFailed'))
    } finally {
      setLoading(false)
    }
  }, [t])

  useEffect(() => { fetchAll() }, [fetchAll])

  if (loading) {
    return (
      <div className="space-y-6">
        <div className="bg-white rounded-lg border border-gray-200 p-12 text-center">
          <p className="text-sm text-gray-500">{t('settings.endpoints.loading')}</p>
        </div>
      </div>
    )
  }

  return (
    <div className="pt-14 px-3 md:px-8 max-w-5xl mx-auto pb-8 space-y-8">
      {error && (
        <div className="bg-red-50 border border-red-200 rounded-lg p-3 text-sm text-red-700">
          {error}
        </div>
      )}

      <PreferencesSection />

      <div className="bg-blue-50 border border-blue-200 rounded-lg p-4 text-sm text-blue-800">
        <p className="font-medium mb-1">{t('settings.v05.title')}</p>
        <p className="text-xs">
          {t('settings.v05.body')}{' '}
          <a href="/prompts" className="underline">{t('settings.v05.link')}</a>
        </p>
      </div>

      <EndpointsSection endpoints={endpoints} onRefresh={fetchAll} />
    </div>
  )
}

// =========================================================================
// Section: Preferences (chunk-20)
// =========================================================================

function PreferencesSection() {
  const t = useT()
  return (
    <section
      data-testid="preferences-section"
      className="bg-white rounded-lg border border-gray-200 p-5 space-y-3"
    >
      <h2 className="text-base font-semibold text-gray-900">
        {t('settings.preferences.title')}
      </h2>
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <p className="text-sm font-medium text-gray-800">
            {t('settings.preferences.language')}
          </p>
          <p className="text-xs text-gray-500">
            {t('settings.preferences.languageHint')}
          </p>
        </div>
        <LanguageSwitcher />
      </div>
    </section>
  )
}

function LanguageSwitcher() {
  const { locale, setLocale } = useLocale()
  const t = useT()
  return (
    <div
      role="group"
      aria-label={t('locale.switch')}
      data-testid="language-switcher"
      data-locale={locale}
      className="inline-flex rounded-md border border-gray-200 bg-gray-50 p-0.5"
    >
      {LOCALES.map((loc: Locale) => {
        const active = loc === locale
        const label = loc === 'zh' ? t('locale.zh') : t('locale.en')
        return (
          <button
            key={loc}
            type="button"
            onClick={() => setLocale(loc)}
            aria-pressed={active}
            data-locale={loc}
            className={
              'px-3 py-1 text-xs rounded ' +
              (active
                ? 'bg-white text-gray-900 shadow-sm border border-gray-200'
                : 'text-gray-500 hover:text-gray-700')
            }
          >
            {label}
          </button>
        )
      })}
    </div>
  )
}

// =========================================================================
// Section: API Endpoints (unchanged from v0.4)
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
    tier: 'standard',
  })
  const [saving, setSaving] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)
  const [testingId, setTestingId] = useState<string | null>(null)
  const [testResults, setTestResults] = useState<Record<string, TestResult>>(() => {
    const init: Record<string, TestResult> = {}
    for (const ep of endpoints) {
      if (ep.last_test_latency != null) {
        init[ep.id] = {
          success: ep.last_test_ok === 1,
          message: ep.last_test_ok === 1 ? `正常 ${ep.last_test_latency}ms` : '上次测试失败',
          latency_ms: ep.last_test_latency,
        }
      }
    }
    return init
  })

  const resetForm = useCallback(() => {
    setFormData({ name: '', provider_type: 'anthropic', base_url: '', api_key: '', default_model: '', tier: 'standard' })
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
      tier: ep.tier || 'standard',
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
          tier: formData.tier,
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
      if (!confirm('确定删除此端点？引用该端点的 Prompt 将失去关联。')) return
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

            {/* v1.4 — tier dropdown (flagship / standard / small / distill / embedding) */}
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">模型等级 *</label>
              <select
                data-testid="endpoint-tier-select"
                value={formData.tier}
                onChange={(e) => setFormData((d) => ({ ...d, tier: e.target.value }))}
                className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent bg-white"
              >
                {TIER_OPTIONS.map((opt) => (
                  <option key={opt.value} value={opt.value}>
                    {opt.label}
                  </option>
                ))}
              </select>
              <p className="mt-1 text-[11px] text-gray-500">
                用于 LLM 路由：flagship (旗舰) / standard (常规) / small (轻量) / distill (蒸馏) / embedding (向量)。
              </p>
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
                    <div className="flex flex-wrap gap-1 mt-1">
                      <span className="inline-flex items-center px-2 py-0.5 rounded text-[10px] font-medium bg-blue-50 text-blue-700">
                        {ep.provider_type}
                      </span>
                      {/* v1.4 — tier badge */}
                      <span
                        data-testid="endpoint-tier-badge"
                        data-tier={ep.tier || 'standard'}
                        className={`inline-flex items-center px-2 py-0.5 rounded text-[10px] font-medium ${
                          TIER_BADGE_CLASS[ep.tier || 'standard'] || 'bg-gray-100 text-gray-700'
                        }`}
                      >
                        {ep.tier || 'standard'}
                      </span>
                    </div>
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
