'use client'

import React, { useState, useEffect } from 'react'
import { apiFetch } from '@/lib/api'

interface Character {
  id: string
  name: string
  profileJson: {
    identity?: string
    personality?: string
    appearance?: string
    abilities?: string
    current_status?: string
    [key: string]: unknown
  }
}

interface WorldRule {
  id: string
  category: string
  ruleText: string
}

interface SettingsPanelProps {
  projectId: string
}

export function SettingsPanel({ projectId }: SettingsPanelProps) {
  const [tab, setTab] = useState<'characters' | 'world'>('characters')
  const [characters, setCharacters] = useState<Character[]>([])
  const [worldRules, setWorldRules] = useState<WorldRule[]>([])
  const [loading, setLoading] = useState(false)
  const [editingChar, setEditingChar] = useState<string | null>(null)
  const [showAddChar, setShowAddChar] = useState(false)
  const [showAddRule, setShowAddRule] = useState(false)

  const fetchCharacters = async () => {
    if (!projectId) return
    try {
      const data = await apiFetch<Character[]>(`/api/projects/${projectId}/characters`)
      setCharacters(data)
    } catch { /* ignore */ }
  }

  const fetchWorldRules = async () => {
    if (!projectId) return
    try {
      const data = await apiFetch<WorldRule[]>(`/api/projects/${projectId}/world-rules`)
      setWorldRules(data)
    } catch { /* ignore */ }
  }

  useEffect(() => {
    setLoading(true)
    Promise.all([fetchCharacters(), fetchWorldRules()]).finally(() => setLoading(false))
  }, [projectId]) // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div className="space-y-3">
      <h3 className="text-sm font-semibold text-gray-900">Story Settings</h3>

      {/* Tabs */}
      <div className="flex border-b border-gray-200">
        {(['characters', 'world'] as const).map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={`px-3 py-1.5 text-xs font-medium border-b-2 ${
              tab === t
                ? 'border-blue-600 text-blue-600'
                : 'border-transparent text-gray-500 hover:text-gray-700'
            }`}
          >
            {t === 'characters' ? 'Characters' : 'World Rules'}
          </button>
        ))}
      </div>

      {loading ? (
        <p className="text-xs text-gray-400">Loading...</p>
      ) : tab === 'characters' ? (
        <div className="space-y-2">
          {characters.map((char) => (
            <div
              key={char.id}
              className="bg-white border border-gray-200 rounded-lg p-2.5"
            >
              <div className="flex items-center justify-between mb-1">
                <span className="text-xs font-medium text-gray-800">{char.name}</span>
                <button
                  onClick={() => setEditingChar(editingChar === char.id ? null : char.id)}
                  className="text-[10px] text-blue-500 hover:text-blue-600"
                >
                  {editingChar === char.id ? 'Close' : 'Edit'}
                </button>
              </div>
              {char.profileJson.identity && (
                <p className="text-[11px] text-gray-500">{char.profileJson.identity}</p>
              )}
              {editingChar === char.id && (
                <div className="mt-2 space-y-1.5">
                  {['identity', 'personality', 'appearance', 'abilities', 'current_status'].map((field) => (
                    <div key={field}>
                      <label className="text-[10px] text-gray-400 capitalize">{field.replace('_', ' ')}</label>
                      <input
                        type="text"
                        defaultValue={(char.profileJson[field] as string) || ''}
                        className="w-full px-2 py-1 text-xs border border-gray-200 rounded"
                        onBlur={async (e) => {
                          const updated = { ...char.profileJson, [field]: e.target.value }
                          await apiFetch(`/api/projects/${projectId}/characters/${char.id}`, {
                            method: 'PUT',
                            body: JSON.stringify({ profile_json: updated }),
                          })
                          fetchCharacters()
                        }}
                      />
                    </div>
                  ))}
                </div>
              )}
            </div>
          ))}

          {characters.length === 0 && !showAddChar && (
            <p className="text-xs text-gray-400">No characters defined.</p>
          )}

          {showAddChar ? (
            <AddCharacterForm
              projectId={projectId}
              onDone={() => { setShowAddChar(false); fetchCharacters() }}
            />
          ) : (
            <button
              onClick={() => setShowAddChar(true)}
              className="w-full px-2 py-1.5 text-xs border border-dashed border-gray-300 rounded-lg text-gray-500 hover:bg-gray-50"
            >
              + Add Character
            </button>
          )}
        </div>
      ) : (
        <div className="space-y-2">
          {worldRules.map((rule) => (
            <div
              key={rule.id}
              className="bg-white border border-gray-200 rounded-lg p-2.5"
            >
              <div className="flex items-center justify-between mb-1">
                <span className="text-[10px] font-medium text-blue-600 uppercase">{rule.category}</span>
                <button
                  onClick={async () => {
                    await apiFetch(`/api/projects/${projectId}/world-rules/${rule.id}`, { method: 'DELETE' })
                    fetchWorldRules()
                  }}
                  className="text-[10px] text-red-400 hover:text-red-500"
                >
                  Delete
                </button>
              </div>
              <p className="text-xs text-gray-700 leading-relaxed">{rule.ruleText}</p>
            </div>
          ))}

          {worldRules.length === 0 && !showAddRule && (
            <p className="text-xs text-gray-400">No world rules defined.</p>
          )}

          {showAddRule ? (
            <AddWorldRuleForm
              projectId={projectId}
              onDone={() => { setShowAddRule(false); fetchWorldRules() }}
            />
          ) : (
            <button
              onClick={() => setShowAddRule(true)}
              className="w-full px-2 py-1.5 text-xs border border-dashed border-gray-300 rounded-lg text-gray-500 hover:bg-gray-50"
            >
              + Add World Rule
            </button>
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
    await apiFetch(`/api/projects/${projectId}/characters`, {
      method: 'POST',
      body: JSON.stringify({ name, profile_json: { identity } }),
    })
    onDone()
  }

  return (
    <div className="bg-gray-50 rounded-lg p-2.5 space-y-1.5">
      <input
        value={name}
        onChange={(e) => setName(e.target.value)}
        placeholder="Character name"
        className="w-full px-2 py-1 text-xs border border-gray-200 rounded"
      />
      <input
        value={identity}
        onChange={(e) => setIdentity(e.target.value)}
        placeholder="Identity / Role"
        className="w-full px-2 py-1 text-xs border border-gray-200 rounded"
      />
      <div className="flex gap-1.5">
        <button onClick={handleSubmit} className="flex-1 px-2 py-1 text-xs bg-blue-600 text-white rounded">Add</button>
        <button onClick={onDone} className="flex-1 px-2 py-1 text-xs bg-gray-200 text-gray-600 rounded">Cancel</button>
      </div>
    </div>
  )
}

function AddWorldRuleForm({ projectId, onDone }: { projectId: string; onDone: () => void }) {
  const [category, setCategory] = useState('')
  const [text, setText] = useState('')

  const handleSubmit = async () => {
    if (!category.trim() || !text.trim()) return
    await apiFetch(`/api/projects/${projectId}/world-rules`, {
      method: 'POST',
      body: JSON.stringify({ category, rule_text: text }),
    })
    onDone()
  }

  return (
    <div className="bg-gray-50 rounded-lg p-2.5 space-y-1.5">
      <input
        value={category}
        onChange={(e) => setCategory(e.target.value)}
        placeholder="Category (e.g., power_system, geography)"
        className="w-full px-2 py-1 text-xs border border-gray-200 rounded"
      />
      <textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        placeholder="Rule description..."
        className="w-full px-2 py-1 text-xs border border-gray-200 rounded resize-none h-16"
      />
      <div className="flex gap-1.5">
        <button onClick={handleSubmit} className="flex-1 px-2 py-1 text-xs bg-blue-600 text-white rounded">Add</button>
        <button onClick={onDone} className="flex-1 px-2 py-1 text-xs bg-gray-200 text-gray-600 rounded">Cancel</button>
      </div>
    </div>
  )
}
