'use client'

import React, { useState, useEffect } from 'react'
import { apiFetch } from '@/lib/api'

interface Character {
  id: string
  name: string
  profile_json: {
    identity?: string
    personality?: string
    appearance?: string
    abilities?: string
    biography?: string
    current_status?: string
    [k: string]: unknown
  } | null
}

interface WorldRule {
  id: string
  category: string
  rule_text: string
}

interface CharResp { characters?: Character[]; total?: number }
interface RuleResp { rules?: WorldRule[]; world_rules?: WorldRule[]; total?: number }

interface SettingsPanelProps { projectId: string }

export function SettingsPanel({ projectId }: SettingsPanelProps) {
  const [tab, setTab] = useState<'characters' | 'world'>('characters')
  const [characters, setCharacters] = useState<Character[]>([])
  const [worldRules, setWorldRules] = useState<WorldRule[]>([])
  const [loading, setLoading] = useState(false)
  const [editingChar, setEditingChar] = useState<string | null>(null)
  // PR-FIX-CHAR-SETTINGS-FE (2026-05-04): inline edit UI for world rules.
  const [editingRule, setEditingRule] = useState<string | null>(null)
  const [showAddChar, setShowAddChar] = useState(false)
  const [showAddRule, setShowAddRule] = useState(false)

  const fetchCharacters = async () => {
    if (!projectId) return
    try {
      const data = await apiFetch<Character[] | CharResp>(`/api/projects/${projectId}/characters`)
      const arr = Array.isArray(data) ? data : (data.characters || [])
      setCharacters(arr)
    } catch { setCharacters([]) }
  }

  const fetchWorldRules = async () => {
    if (!projectId) return
    try {
      const data = await apiFetch<WorldRule[] | RuleResp>(`/api/projects/${projectId}/world-rules`)
      const arr = Array.isArray(data) ? data : (data.world_rules || data.rules || [])
      setWorldRules(arr)
    } catch { setWorldRules([]) }
  }

  useEffect(() => {
    setLoading(true)
    Promise.all([fetchCharacters(), fetchWorldRules()]).finally(() => setLoading(false))
  }, [projectId]) // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div className="space-y-3">
      <h3 className="text-sm font-semibold text-gray-900">设定集</h3>
      <div className="flex border-b border-stone-200">
        {(['characters', 'world'] as const).map(t => (
          <button key={t} onClick={() => setTab(t)} className={`px-3 py-1.5 text-xs font-medium border-b-2 ${tab === t ? 'border-blue-600 text-blue-600' : 'border-transparent text-stone-500 hover:text-stone-700'}`}>
            {t === 'characters' ? `人物 (${characters.length})` : `世界规则 (${worldRules.length})`}
          </button>
        ))}
      </div>

      {loading ? (
        <p className="text-xs text-gray-400">加载中...</p>
      ) : tab === 'characters' ? (
        <div className="space-y-2">
          {characters.map(char => {
            const profile = char.profile_json || {}
            return (
              <div key={char.id} className="bg-white border border-stone-200 rounded-lg p-2.5">
                <div className="flex items-center justify-between mb-1">
                  <span className="text-xs font-medium text-stone-800">{char.name}</span>
                  <button onClick={() => setEditingChar(editingChar === char.id ? null : char.id)} className="text-[10px] text-blue-500 hover:text-blue-600">
                    {editingChar === char.id ? '收起' : '编辑'}
                  </button>
                </div>
                {typeof profile.identity === 'string' && profile.identity && (
                  <p className="text-[11px] text-stone-500">{profile.identity}</p>
                )}
                {editingChar === char.id && (
                  <div className="mt-2 space-y-1.5">
                    {(['identity', 'personality', 'appearance', 'abilities', 'biography', 'current_status'] as const).map(field => {
                      const labelMap: Record<string, string> = { identity: '身份', personality: '性格', appearance: '外貌', abilities: '能力', biography: '小传', current_status: '当前状态' }
                      return (
                        <div key={field}>
                          <label className="text-[10px] text-stone-400">{labelMap[field]}</label>
                          <input
                            type="text"
                            defaultValue={(profile[field] as string) || ''}
                            className="w-full px-2 py-1 text-xs border border-stone-200 rounded"
                            onBlur={async (e) => {
                              const updated = { ...profile, [field]: e.target.value }
                              await apiFetch(`/api/projects/${projectId}/characters/${char.id}`, { method: 'PUT', body: JSON.stringify({ profile_json: updated }) })
                              fetchCharacters()
                            }}
                          />
                        </div>
                      )
                    })}
                  </div>
                )}
              </div>
            )
          })}
          {characters.length === 0 && !showAddChar && <p className="text-xs text-gray-400">还没有人物。</p>}
          {showAddChar ? (
            <AddCharacterForm projectId={projectId} onDone={() => { setShowAddChar(false); fetchCharacters() }} />
          ) : (
            <button onClick={() => setShowAddChar(true)} className="w-full px-2 py-1.5 text-xs border border-dashed border-stone-300 rounded-lg text-stone-500 hover:bg-stone-50">+ 添加人物</button>
          )}
        </div>
      ) : (
        <div className="space-y-2">
          {worldRules.map(rule => (
            <div key={rule.id} className="bg-white border border-stone-200 rounded-lg p-2.5">
              <div className="flex items-center justify-between mb-1">
                <span className="text-[10px] font-medium text-blue-600 uppercase">{rule.category}</span>
                <div className="flex items-center gap-2">
                  <button
                    onClick={() => setEditingRule(editingRule === rule.id ? null : rule.id)}
                    className="text-[10px] text-blue-500 hover:text-blue-600"
                  >{editingRule === rule.id ? '收起' : '编辑'}</button>
                  <button
                    onClick={async () => {
                      if (!confirm(`确定删除这条规则吗？\n\n${rule.rule_text.slice(0, 80)}${rule.rule_text.length > 80 ? '…' : ''}`)) return
                      await apiFetch(`/api/projects/${projectId}/world-rules/${rule.id}`, { method: 'DELETE' })
                      fetchWorldRules()
                    }}
                    className="text-[10px] text-rose-400 hover:text-rose-500"
                  >删除</button>
                </div>
              </div>
              {editingRule === rule.id ? (
                <div className="mt-1.5 space-y-1.5">
                  <div>
                    <label className="text-[10px] text-stone-400">分类</label>
                    <input
                      type="text"
                      defaultValue={rule.category}
                      className="w-full px-2 py-1 text-xs border border-stone-200 rounded"
                      onBlur={async (e) => {
                        const newCat = e.target.value.trim()
                        if (!newCat || newCat === rule.category) return
                        await apiFetch(`/api/projects/${projectId}/world-rules/${rule.id}`, {
                          method: 'PUT',
                          body: JSON.stringify({ category: newCat, rule_text: rule.rule_text }),
                        })
                        fetchWorldRules()
                      }}
                    />
                  </div>
                  <div>
                    <label className="text-[10px] text-stone-400">规则描述</label>
                    <textarea
                      defaultValue={rule.rule_text}
                      className="w-full px-2 py-1 text-xs border border-stone-200 rounded resize-y min-h-16"
                      onBlur={async (e) => {
                        const newText = e.target.value.trim()
                        if (!newText || newText === rule.rule_text) return
                        await apiFetch(`/api/projects/${projectId}/world-rules/${rule.id}`, {
                          method: 'PUT',
                          body: JSON.stringify({ category: rule.category, rule_text: newText }),
                        })
                        fetchWorldRules()
                      }}
                    />
                  </div>
                  <p className="text-[10px] text-stone-400">失焦自动保存。</p>
                </div>
              ) : (
                <p className="text-xs text-stone-700 leading-relaxed">{rule.rule_text}</p>
              )}
            </div>
          ))}
          {worldRules.length === 0 && !showAddRule && <p className="text-xs text-gray-400">还没有世界规则。</p>}
          {showAddRule ? (
            <AddWorldRuleForm projectId={projectId} onDone={() => { setShowAddRule(false); fetchWorldRules() }} />
          ) : (
            <button onClick={() => setShowAddRule(true)} className="w-full px-2 py-1.5 text-xs border border-dashed border-stone-300 rounded-lg text-stone-500 hover:bg-stone-50">+ 添加规则</button>
          )}
        </div>
      )}
    </div>
  )
}

function AddCharacterForm({ projectId, onDone }: { projectId: string; onDone: () => void }) {
  const [name, setName] = useState('')
  const [identity, setIdentity] = useState('')
  const handleSubmit = async () => {
    if (!name.trim()) return
    await apiFetch(`/api/projects/${projectId}/characters`, { method: 'POST', body: JSON.stringify({ name, profile_json: { identity } }) })
    onDone()
  }
  return (
    <div className="bg-stone-50 rounded-lg p-2.5 space-y-1.5">
      <input value={name} onChange={e => setName(e.target.value)} placeholder="姓名" className="w-full px-2 py-1 text-xs border border-stone-200 rounded" />
      <input value={identity} onChange={e => setIdentity(e.target.value)} placeholder="身份 / 角色" className="w-full px-2 py-1 text-xs border border-stone-200 rounded" />
      <div className="flex gap-1.5">
        <button onClick={handleSubmit} className="flex-1 px-2 py-1 text-xs bg-blue-600 text-white rounded">添加</button>
        <button onClick={onDone} className="flex-1 px-2 py-1 text-xs bg-stone-200 text-stone-600 rounded">取消</button>
      </div>
    </div>
  )
}

function AddWorldRuleForm({ projectId, onDone }: { projectId: string; onDone: () => void }) {
  const [category, setCategory] = useState('')
  const [text, setText] = useState('')
  const handleSubmit = async () => {
    if (!category.trim() || !text.trim()) return
    await apiFetch(`/api/projects/${projectId}/world-rules`, { method: 'POST', body: JSON.stringify({ category, rule_text: text }) })
    onDone()
  }
  return (
    <div className="bg-stone-50 rounded-lg p-2.5 space-y-1.5">
      <input value={category} onChange={e => setCategory(e.target.value)} placeholder="分类 (如 power_system / geography)" className="w-full px-2 py-1 text-xs border border-stone-200 rounded" />
      <textarea value={text} onChange={e => setText(e.target.value)} placeholder="规则描述..." className="w-full px-2 py-1 text-xs border border-stone-200 rounded resize-none h-16" />
      <div className="flex gap-1.5">
        <button onClick={handleSubmit} className="flex-1 px-2 py-1 text-xs bg-blue-600 text-white rounded">添加</button>
        <button onClick={onDone} className="flex-1 px-2 py-1 text-xs bg-stone-200 text-stone-600 rounded">取消</button>
      </div>
    </div>
  )
}
