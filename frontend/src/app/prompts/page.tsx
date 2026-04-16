'use client'

import { useState, useEffect, useCallback } from 'react'
import { apiFetch } from '@/lib/api'

interface PromptAsset {
  id: string
  task_type: string
  name: string
  description: string
  mode: string
  system_prompt: string
  user_template: string
  output_schema: Record<string, unknown> | null
  context_policy: string
  version: number
  is_active: number
  success_count: number
  fail_count: number
  avg_score: number
  created_at: string
  updated_at: string
}

const MODE_LABELS: Record<string, string> = { text: '文本', structured: '结构化(JSON)' }

export default function PromptsPage() {
  const [prompts, setPrompts] = useState<PromptAsset[]>([])
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

  useEffect(() => { fetchPrompts() }, [fetchPrompts])

  const handleDelete = async (id: string) => {
    if (!confirm('确定删除此 Prompt？')) return
    await apiFetch(`/api/prompts/${id}`, { method: 'DELETE' })
    fetchPrompts()
  }

  // Group by task_type
  const grouped = prompts.reduce<Record<string, PromptAsset[]>>((acc, p) => {
    (acc[p.task_type] = acc[p.task_type] || []).push(p)
    return acc
  }, {})

  return (
    <div className="pt-14 px-4 md:px-8 max-w-5xl mx-auto pb-12">
      <div className="flex items-end justify-between mb-8">
        <div>
          <h1 className="text-2xl font-bold text-gray-900 tracking-tight">Prompt 注册表</h1>
          <p className="text-sm text-gray-500 mt-1">统一管理所有 AI 提示词 — 版本化、可追溯、可编辑</p>
        </div>
        <button onClick={() => { setEditing(null); setShowCreate(true) }}
          className="px-4 py-2 text-sm bg-gray-900 text-white rounded-lg hover:bg-gray-800">
          + 新建版本
        </button>
      </div>

      {/* Create/Edit form */}
      {(showCreate || editing) && (
        <PromptForm
          prompt={editing}
          onClose={() => { setShowCreate(false); setEditing(null) }}
          onSaved={() => { setShowCreate(false); setEditing(null); fetchPrompts() }}
        />
      )}

      {loading ? (
        <p className="text-sm text-gray-400 text-center py-16">加载中...</p>
      ) : prompts.length === 0 ? (
        <p className="text-sm text-gray-400 text-center py-16">暂无 Prompt</p>
      ) : (
        <div className="space-y-4">
          {Object.entries(grouped).map(([taskType, assets]) => (
            <div key={taskType} className="bg-white rounded-xl border border-gray-200 overflow-hidden">
              <div className="px-5 py-3 bg-gray-50/50 border-b border-gray-100 flex items-center justify-between">
                <div>
                  <span className="text-sm font-semibold text-gray-900">{taskType}</span>
                  <span className="text-xs text-gray-400 ml-2">{assets.length} 个版本</span>
                </div>
                <span className="text-[10px] px-2 py-0.5 bg-blue-50 text-blue-600 rounded">
                  {MODE_LABELS[assets[0]?.mode] || assets[0]?.mode}
                </span>
              </div>

              {assets.map(p => {
                const totalCalls = p.success_count + p.fail_count
                const successRate = totalCalls > 0 ? Math.round(p.success_count / totalCalls * 100) : 0
                return (
                  <div key={p.id} className={`px-5 py-3 border-b border-gray-50 last:border-b-0 ${!p.is_active ? 'opacity-50' : ''}`}>
                    <div className="flex items-center justify-between mb-1">
                      <div className="flex items-center gap-2">
                        <span className="text-sm font-medium text-gray-800">{p.name}</span>
                        <span className="text-[10px] px-1.5 py-0.5 bg-gray-100 text-gray-500 rounded">v{p.version}</span>
                        {p.is_active ? (
                          <span className="text-[10px] px-1.5 py-0.5 bg-green-50 text-green-600 rounded-full">激活</span>
                        ) : (
                          <span className="text-[10px] px-1.5 py-0.5 bg-gray-100 text-gray-400 rounded-full">历史</span>
                        )}
                      </div>
                      <div className="flex items-center gap-3 text-xs text-gray-400">
                        {totalCalls > 0 && (
                          <span>调用 {totalCalls} 次 | 成功率 {successRate}%</span>
                        )}
                      </div>
                    </div>

                    {p.description && <p className="text-xs text-gray-500 mb-2">{p.description}</p>}

                    <div className="bg-gray-50 rounded-lg p-3 mb-2">
                      <pre className="text-xs text-gray-600 whitespace-pre-wrap line-clamp-3 font-mono">{p.system_prompt}</pre>
                    </div>

                    <div className="flex gap-1.5">
                      <button onClick={() => setEditing(p)}
                        className="px-2.5 py-1 text-xs bg-gray-100 text-gray-700 rounded hover:bg-gray-200">编辑</button>
                      <button onClick={() => handleDelete(p.id)}
                        className="px-2.5 py-1 text-xs bg-red-50 text-red-600 rounded hover:bg-red-100 ml-auto">删除</button>
                    </div>
                  </div>
                )
              })}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function PromptForm({
  prompt,
  onClose,
  onSaved,
}: {
  prompt: PromptAsset | null
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
  const [saving, setSaving] = useState(false)

  const handleSave = async () => {
    if (!taskType.trim() || !name.trim() || !systemPrompt.trim()) return
    setSaving(true)
    try {
      if (isEdit) {
        await apiFetch(`/api/prompts/${prompt.id}`, {
          method: 'PUT',
          body: JSON.stringify({ name, description, system_prompt: systemPrompt, user_template: userTemplate }),
        })
      } else {
        await apiFetch('/api/prompts', {
          method: 'POST',
          body: JSON.stringify({ task_type: taskType, name, description, mode, system_prompt: systemPrompt, user_template: userTemplate }),
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
        <h3 className="text-sm font-semibold text-gray-900">{isEdit ? '编辑 Prompt' : '新建 Prompt 版本'}</h3>
        <button onClick={onClose} className="text-xs text-gray-400 hover:text-gray-600">取消</button>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
        <div>
          <label className="block text-xs font-medium text-gray-600 mb-1">任务类型 *</label>
          <input value={taskType} onChange={e => setTaskType(e.target.value)} disabled={isEdit}
            placeholder="如 generation, evaluation"
            className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg disabled:bg-gray-50" />
        </div>
        <div>
          <label className="block text-xs font-medium text-gray-600 mb-1">名称 *</label>
          <input value={name} onChange={e => setName(e.target.value)}
            className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg" />
        </div>
        <div>
          <label className="block text-xs font-medium text-gray-600 mb-1">模式</label>
          <select value={mode} onChange={e => setMode(e.target.value)} disabled={isEdit}
            className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg disabled:bg-gray-50">
            <option value="text">文本</option>
            <option value="structured">结构化(JSON)</option>
          </select>
        </div>
      </div>

      <div>
        <label className="block text-xs font-medium text-gray-600 mb-1">描述</label>
        <input value={description} onChange={e => setDescription(e.target.value)}
          className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg" />
      </div>

      <div>
        <label className="block text-xs font-medium text-gray-600 mb-1">System Prompt *</label>
        <textarea value={systemPrompt} onChange={e => setSystemPrompt(e.target.value)}
          className="w-full h-40 px-3 py-2 text-sm border border-gray-200 rounded-lg font-mono resize-none" />
      </div>

      <div>
        <label className="block text-xs font-medium text-gray-600 mb-1">User Template（可选，支持 {'{{变量}}'} 占位符）</label>
        <textarea value={userTemplate} onChange={e => setUserTemplate(e.target.value)}
          className="w-full h-20 px-3 py-2 text-sm border border-gray-200 rounded-lg font-mono resize-none" />
      </div>

      <div className="flex gap-3">
        <button onClick={handleSave} disabled={saving || !taskType.trim() || !systemPrompt.trim()}
          className="px-5 py-2 text-sm bg-gray-900 text-white rounded-lg disabled:opacity-50">
          {saving ? '保存中...' : isEdit ? '更新' : '创建新版本'}
        </button>
        {!isEdit && <p className="text-xs text-gray-400 self-center">创建新版本会自动停用同任务类型的旧版本</p>}
      </div>
    </div>
  )
}
