'use client'

import { useState, useEffect, useCallback } from 'react'
import { apiFetch } from '@/lib/api'

interface StyleRule {
  rule: string
  weight: number
  category: string
}

interface AntiAIRule {
  pattern: string
  replacement: string
  autoRewrite: boolean
}

interface StyleProfile {
  id: string
  name: string
  description: string
  source_book: string | null
  rules_json: StyleRule[]
  anti_ai_rules: AntiAIRule[]
  tone_keywords: string[]
  sample_passages: string[]
  bind_level: string
  bind_target_id: string | null
  is_active: number
  config_json: Record<string, unknown>
  created_at: string
  updated_at: string
}

const BIND_LABELS: Record<string, string> = { global: '全局', book: '整本书', chapter: '单章' }
const WEIGHT_LABELS = (w: number) => w >= 0.85 ? '必须' : w >= 0.65 ? '优先' : '参考'
const WEIGHT_COLORS = (w: number) => w >= 0.85 ? 'text-red-600' : w >= 0.65 ? 'text-amber-600' : 'text-gray-500'
const CATEGORIES = ['rhythm', 'structure', 'style', 'dialogue', 'combat', 'description', 'custom']
const CATEGORY_LABELS: Record<string, string> = {
  rhythm: '节奏', structure: '结构', style: '风格', dialogue: '对话',
  combat: '战斗', description: '描写', custom: '自定义',
}

export default function StylesPage() {
  const [styles, setStyles] = useState<StyleProfile[]>([])
  const [loading, setLoading] = useState(true)
  const [editingStyle, setEditingStyle] = useState<StyleProfile | null>(null)
  const [showCreate, setShowCreate] = useState(false)
  const [showDetect, setShowDetect] = useState(false)
  const [testWriteId, setTestWriteId] = useState<string | null>(null)
  const [testResult, setTestResult] = useState('')
  const [testLoading, setTestLoading] = useState(false)

  const fetchStyles = useCallback(async () => {
    try {
      const data = await apiFetch<StyleProfile[]>('/api/styles')
      setStyles(data)
    } catch { /* */ }
    finally { setLoading(false) }
  }, [])

  useEffect(() => { fetchStyles() }, [fetchStyles])

  const handleDelete = async (id: string) => {
    if (!confirm('确定删除此写法？')) return
    await apiFetch(`/api/styles/${id}`, { method: 'DELETE' })
    fetchStyles()
  }

  const handleToggle = async (style: StyleProfile) => {
    await apiFetch(`/api/styles/${style.id}`, {
      method: 'PUT',
      body: JSON.stringify({ is_active: style.is_active ? 0 : 1 }),
    })
    fetchStyles()
  }

  const handleTestWrite = async (id: string) => {
    setTestWriteId(id)
    setTestLoading(true)
    setTestResult('')
    try {
      const data = await apiFetch<{ text: string }>(`/api/styles/${id}/test-write`, {
        method: 'POST',
        body: JSON.stringify({ prompt: '写一段200字的场景描写，一个人走在雨中的小巷里。' }),
      })
      setTestResult(data.text)
    } catch (e) {
      setTestResult(e instanceof Error ? e.message : '试写失败')
    } finally {
      setTestLoading(false)
    }
  }

  return (
    <div className="pt-14 px-4 md:px-8 max-w-5xl mx-auto pb-12">
      {/* Header */}
      <div className="flex items-end justify-between mb-8">
        <div>
          <h1 className="text-2xl font-bold text-gray-900 tracking-tight">写法引擎</h1>
          <p className="text-sm text-gray-500 mt-1">创建、管理和绑定写作风格 — 控制 AI 的笔触</p>
        </div>
        <div className="flex gap-2">
          <button onClick={() => setShowDetect(true)}
            className="px-4 py-2 text-sm border border-gray-300 text-gray-700 rounded-lg hover:bg-gray-50 transition-colors">
            从文本检测
          </button>
          <button onClick={() => { setEditingStyle(null); setShowCreate(true) }}
            className="px-4 py-2 text-sm bg-gray-900 text-white rounded-lg hover:bg-gray-800 transition-colors">
            + 新建写法
          </button>
        </div>
      </div>

      {/* Detect from text modal */}
      {showDetect && <DetectModal onClose={() => setShowDetect(false)} onCreated={() => { setShowDetect(false); fetchStyles() }} />}

      {/* Create/Edit form */}
      {(showCreate || editingStyle) && (
        <StyleForm
          style={editingStyle}
          onClose={() => { setShowCreate(false); setEditingStyle(null) }}
          onSaved={() => { setShowCreate(false); setEditingStyle(null); fetchStyles() }}
        />
      )}

      {/* Test write result */}
      {testWriteId && (
        <div className="mb-6 bg-white rounded-xl border border-gray-200 p-5">
          <div className="flex items-center justify-between mb-3">
            <h3 className="text-sm font-semibold text-gray-900">
              试写结果 — {styles.find(s => s.id === testWriteId)?.name}
            </h3>
            <button onClick={() => { setTestWriteId(null); setTestResult('') }}
              className="text-xs text-gray-400 hover:text-gray-600">关闭</button>
          </div>
          {testLoading ? (
            <div className="text-sm text-gray-400 animate-pulse">正在用选定写法生成中...</div>
          ) : (
            <div className="text-sm text-gray-700 leading-relaxed whitespace-pre-wrap bg-gray-50 rounded-lg p-4 border border-gray-100"
              style={{ fontFamily: "'Noto Serif SC', serif" }}>
              {testResult}
            </div>
          )}
        </div>
      )}

      {/* Style cards */}
      {loading ? (
        <div className="text-sm text-gray-400 text-center py-16">加载写法列表...</div>
      ) : styles.length === 0 ? (
        <div className="text-center py-20 bg-white rounded-xl border border-dashed border-gray-300">
          <div className="text-3xl mb-3 opacity-30">&#9998;</div>
          <p className="text-sm text-gray-500 mb-1">暂无写法</p>
          <p className="text-xs text-gray-400">创建一个写法来控制 AI 的写作风格</p>
        </div>
      ) : (
        <div className="grid gap-4 md:grid-cols-2">
          {styles.map(style => (
            <div key={style.id}
              className={`bg-white rounded-xl border p-5 transition-all hover:shadow-md ${
                style.is_active ? 'border-gray-200' : 'border-gray-100 opacity-60'
              }`}>
              {/* Card header */}
              <div className="flex items-start justify-between mb-3">
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2">
                    <h3 className="font-semibold text-gray-900 truncate">{style.name}</h3>
                    {style.is_active ? (
                      <span className="text-[10px] px-1.5 py-0.5 bg-green-50 text-green-600 rounded-full">启用</span>
                    ) : (
                      <span className="text-[10px] px-1.5 py-0.5 bg-gray-100 text-gray-400 rounded-full">停用</span>
                    )}
                  </div>
                  {style.description && (
                    <p className="text-xs text-gray-500 mt-0.5 line-clamp-1">{style.description}</p>
                  )}
                </div>
                <span className="text-[10px] px-2 py-0.5 bg-blue-50 text-blue-600 rounded shrink-0 ml-2">
                  {BIND_LABELS[style.bind_level] || style.bind_level}
                </span>
              </div>

              {/* Stats row */}
              <div className="flex items-center gap-4 text-xs text-gray-500 mb-3">
                <span>{style.rules_json?.length || 0} 条规则</span>
                <span>{style.anti_ai_rules?.length || 0} 条Anti-AI</span>
                {style.tone_keywords?.length > 0 && (
                  <span>{style.tone_keywords.length} 关键词</span>
                )}
              </div>

              {/* Rule preview chips */}
              {(style.rules_json?.length > 0) && (
                <div className="flex flex-wrap gap-1.5 mb-3">
                  {style.rules_json.slice(0, 4).map((r, i) => (
                    <span key={i} className="text-[10px] px-2 py-0.5 bg-gray-50 text-gray-600 rounded border border-gray-100 truncate max-w-[150px]">
                      <span className={`font-medium ${WEIGHT_COLORS(r.weight)}`}>{WEIGHT_LABELS(r.weight)}</span>
                      {' '}{r.rule}
                    </span>
                  ))}
                  {style.rules_json.length > 4 && (
                    <span className="text-[10px] px-2 py-0.5 text-gray-400">+{style.rules_json.length - 4}</span>
                  )}
                </div>
              )}

              {/* Keywords */}
              {style.tone_keywords?.length > 0 && (
                <div className="flex flex-wrap gap-1 mb-3">
                  {style.tone_keywords.slice(0, 6).map((kw, i) => (
                    <span key={i} className="text-[10px] px-1.5 py-0.5 bg-amber-50 text-amber-700 rounded">
                      {kw}
                    </span>
                  ))}
                </div>
              )}

              {/* Actions */}
              <div className="flex gap-1.5 pt-2 border-t border-gray-100">
                <button onClick={() => setEditingStyle(style)}
                  className="px-2.5 py-1 text-xs bg-gray-100 text-gray-700 rounded hover:bg-gray-200 transition-colors">
                  编辑
                </button>
                <button onClick={() => handleTestWrite(style.id)}
                  className="px-2.5 py-1 text-xs bg-blue-50 text-blue-600 rounded hover:bg-blue-100 transition-colors">
                  试写
                </button>
                <button onClick={() => handleToggle(style)}
                  className={`px-2.5 py-1 text-xs rounded transition-colors ${
                    style.is_active ? 'bg-yellow-50 text-yellow-600 hover:bg-yellow-100' : 'bg-green-50 text-green-600 hover:bg-green-100'
                  }`}>
                  {style.is_active ? '停用' : '启用'}
                </button>
                <button onClick={() => handleDelete(style.id)}
                  className="px-2.5 py-1 text-xs bg-red-50 text-red-600 rounded hover:bg-red-100 transition-colors ml-auto">
                  删除
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

/* ─── Style Create/Edit Form ───────────────────────────────── */

function StyleForm({
  style,
  onClose,
  onSaved,
}: {
  style: StyleProfile | null
  onClose: () => void
  onSaved: () => void
}) {
  const isEdit = !!style
  const [name, setName] = useState(style?.name || '')
  const [description, setDescription] = useState(style?.description || '')
  const [rules, setRules] = useState<StyleRule[]>(style?.rules_json || [])
  const [antiAI, setAntiAI] = useState<AntiAIRule[]>(style?.anti_ai_rules || [])
  const [keywords, setKeywords] = useState<string[]>(style?.tone_keywords || [])
  const [samples, setSamples] = useState<string[]>(style?.sample_passages || [])
  const [bindLevel, setBindLevel] = useState(style?.bind_level || 'global')
  const [bindTargetId, setBindTargetId] = useState(style?.bind_target_id || '')
  const [saving, setSaving] = useState(false)
  const [newKeyword, setNewKeyword] = useState('')
  const [compiledPreview, setCompiledPreview] = useState('')

  // New rule inputs
  const [newRule, setNewRule] = useState('')
  const [newRuleWeight, setNewRuleWeight] = useState(0.7)
  const [newRuleCategory, setNewRuleCategory] = useState('custom')

  // New anti-AI input
  const [newAntiPattern, setNewAntiPattern] = useState('')
  const [newAntiReplacement, setNewAntiReplacement] = useState('')

  const handleSave = async () => {
    if (!name.trim()) return
    setSaving(true)
    try {
      const body = {
        name, description,
        rules_json: rules,
        anti_ai_rules: antiAI,
        tone_keywords: keywords,
        sample_passages: samples,
      }
      if (isEdit) {
        await apiFetch(`/api/styles/${style.id}`, { method: 'PUT', body: JSON.stringify(body) })
        if (bindLevel !== style.bind_level || bindTargetId !== (style.bind_target_id || '')) {
          await apiFetch(`/api/styles/${style.id}/bind`, {
            method: 'POST',
            body: JSON.stringify({ bind_level: bindLevel, bind_target_id: bindTargetId || null }),
          })
        }
      } else {
        await apiFetch('/api/styles', { method: 'POST', body: JSON.stringify(body) })
      }
      onSaved()
    } catch (e) {
      alert(e instanceof Error ? e.message : '保存失败')
    } finally {
      setSaving(false)
    }
  }

  const handlePreview = async () => {
    try {
      const data = await apiFetch<{ compiled_prompt: string; char_count: number }>('/api/styles/compile-preview', {
        method: 'POST',
        body: JSON.stringify({ name, description, rules_json: rules, anti_ai_rules: antiAI, tone_keywords: keywords, sample_passages: samples }),
      })
      setCompiledPreview(data.compiled_prompt)
    } catch { /* */ }
  }

  const addRule = () => {
    if (!newRule.trim()) return
    setRules(prev => [...prev, { rule: newRule.trim(), weight: newRuleWeight, category: newRuleCategory }])
    setNewRule('')
    setNewRuleWeight(0.7)
  }

  const removeRule = (idx: number) => setRules(prev => prev.filter((_, i) => i !== idx))

  const addAntiAI = () => {
    if (!newAntiPattern.trim()) return
    setAntiAI(prev => [...prev, { pattern: newAntiPattern.trim(), replacement: newAntiReplacement.trim(), autoRewrite: !!newAntiReplacement.trim() }])
    setNewAntiPattern('')
    setNewAntiReplacement('')
  }

  const addKeyword = () => {
    if (!newKeyword.trim() || keywords.includes(newKeyword.trim())) return
    setKeywords(prev => [...prev, newKeyword.trim()])
    setNewKeyword('')
  }

  return (
    <div className="mb-6 bg-white rounded-xl border border-gray-200 overflow-hidden">
      <div className="px-5 py-4 border-b border-gray-100 flex items-center justify-between bg-gray-50/50">
        <h2 className="text-sm font-semibold text-gray-900">{isEdit ? '编辑写法' : '新建写法'}</h2>
        <button onClick={onClose} className="text-xs text-gray-400 hover:text-gray-600">取消</button>
      </div>

      <div className="p-5 space-y-5">
        {/* Basic info */}
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">名称 *</label>
            <input value={name} onChange={e => setName(e.target.value)}
              placeholder="如：金庸武侠风" className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg" />
          </div>
          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">绑定级别</label>
            <select value={bindLevel} onChange={e => setBindLevel(e.target.value)}
              className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg">
              <option value="global">全局（所有项目）</option>
              <option value="book">整本书</option>
              <option value="chapter">单个章节</option>
            </select>
          </div>
        </div>

        <div>
          <label className="block text-xs font-medium text-gray-600 mb-1">描述</label>
          <input value={description} onChange={e => setDescription(e.target.value)}
            placeholder="简述这个写法的特点" className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg" />
        </div>

        {/* Rules editor */}
        <div>
          <label className="block text-xs font-medium text-gray-600 mb-2">写作规则（{rules.length}）</label>
          {rules.length > 0 && (
            <div className="space-y-1.5 mb-3">
              {rules.map((r, i) => (
                <div key={i} className="flex items-center gap-2 px-3 py-2 bg-gray-50 rounded-lg text-sm">
                  <span className={`text-xs font-semibold w-8 shrink-0 ${WEIGHT_COLORS(r.weight)}`}>
                    {Math.round(r.weight * 100)}%
                  </span>
                  <span className="text-[10px] px-1.5 py-0.5 bg-white border border-gray-200 rounded text-gray-500 shrink-0">
                    {CATEGORY_LABELS[r.category] || r.category}
                  </span>
                  <span className="flex-1 text-gray-700 truncate">{r.rule}</span>
                  <button onClick={() => removeRule(i)} className="text-gray-300 hover:text-red-500 text-xs shrink-0">x</button>
                </div>
              ))}
            </div>
          )}
          {/* Add rule */}
          <div className="flex gap-2 items-end">
            <div className="flex-1">
              <input value={newRule} onChange={e => setNewRule(e.target.value)}
                onKeyDown={e => { if (e.key === 'Enter') addRule() }}
                placeholder="输入规则，如：对话简洁有力"
                className="w-full px-3 py-1.5 text-sm border border-gray-200 rounded-lg" />
            </div>
            <div className="w-20">
              <label className="block text-[10px] text-gray-400 mb-0.5">权重</label>
              <input type="range" min="0.1" max="1" step="0.05" value={newRuleWeight}
                onChange={e => setNewRuleWeight(parseFloat(e.target.value))}
                className="w-full" />
              <div className={`text-center text-[10px] ${WEIGHT_COLORS(newRuleWeight)}`}>
                {Math.round(newRuleWeight * 100)}% {WEIGHT_LABELS(newRuleWeight)}
              </div>
            </div>
            <select value={newRuleCategory} onChange={e => setNewRuleCategory(e.target.value)}
              className="px-2 py-1.5 text-xs border border-gray-200 rounded-lg">
              {CATEGORIES.map(c => <option key={c} value={c}>{CATEGORY_LABELS[c]}</option>)}
            </select>
            <button onClick={addRule} className="px-3 py-1.5 text-xs bg-gray-900 text-white rounded-lg shrink-0">添加</button>
          </div>
        </div>

        {/* Anti-AI rules */}
        <div>
          <label className="block text-xs font-medium text-gray-600 mb-2">Anti-AI 规则（{antiAI.length}）</label>
          {antiAI.length > 0 && (
            <div className="flex flex-wrap gap-1.5 mb-3">
              {antiAI.map((r, i) => (
                <span key={i} className="inline-flex items-center gap-1 text-xs px-2 py-1 bg-red-50 text-red-600 rounded-lg border border-red-100">
                  <span className="line-through opacity-60">{r.pattern}</span>
                  {r.replacement && <><span className="text-gray-300">&#8594;</span><span>{r.replacement}</span></>}
                  <button onClick={() => setAntiAI(prev => prev.filter((_, j) => j !== i))}
                    className="text-red-300 hover:text-red-500 ml-0.5">x</button>
                </span>
              ))}
            </div>
          )}
          <div className="flex gap-2">
            <input value={newAntiPattern} onChange={e => setNewAntiPattern(e.target.value)}
              placeholder="禁用词（如：璀璨）" className="flex-1 px-3 py-1.5 text-sm border border-gray-200 rounded-lg" />
            <input value={newAntiReplacement} onChange={e => setNewAntiReplacement(e.target.value)}
              placeholder="替换词（选填）" className="flex-1 px-3 py-1.5 text-sm border border-gray-200 rounded-lg" />
            <button onClick={addAntiAI} className="px-3 py-1.5 text-xs bg-red-600 text-white rounded-lg shrink-0">添加</button>
          </div>
        </div>

        {/* Tone keywords */}
        <div>
          <label className="block text-xs font-medium text-gray-600 mb-2">风格关键词</label>
          <div className="flex flex-wrap gap-1.5 mb-2">
            {keywords.map((kw, i) => (
              <span key={i} className="inline-flex items-center gap-1 text-xs px-2 py-1 bg-amber-50 text-amber-700 rounded-lg">
                {kw}
                <button onClick={() => setKeywords(prev => prev.filter((_, j) => j !== i))}
                  className="text-amber-300 hover:text-amber-500">x</button>
              </span>
            ))}
          </div>
          <div className="flex gap-2">
            <input value={newKeyword} onChange={e => setNewKeyword(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); addKeyword() } }}
              placeholder="输入关键词回车添加" className="flex-1 px-3 py-1.5 text-sm border border-gray-200 rounded-lg" />
            <button onClick={addKeyword} className="px-3 py-1.5 text-xs bg-amber-500 text-white rounded-lg">添加</button>
          </div>
        </div>

        {/* Sample passages */}
        <div>
          <label className="block text-xs font-medium text-gray-600 mb-1">风格参考样本（{samples.length}）</label>
          <p className="text-[10px] text-gray-400 mb-2">粘贴目标风格的文本片段作为 few-shot 参考</p>
          {samples.map((s, i) => (
            <div key={i} className="mb-2 relative">
              <textarea value={s} onChange={e => setSamples(prev => prev.map((x, j) => j === i ? e.target.value : x))}
                className="w-full h-20 px-3 py-2 text-xs border border-gray-200 rounded-lg resize-none" />
              <button onClick={() => setSamples(prev => prev.filter((_, j) => j !== i))}
                className="absolute top-1 right-1 text-xs text-gray-300 hover:text-red-500">x</button>
            </div>
          ))}
          <button onClick={() => setSamples(prev => [...prev, ''])}
            className="text-xs text-blue-600 hover:text-blue-700">+ 添加参考样本</button>
        </div>

        {/* Compiled preview */}
        {compiledPreview && (
          <div className="bg-gray-900 rounded-lg p-4 overflow-auto max-h-48">
            <pre className="text-xs text-green-400 whitespace-pre-wrap font-mono">{compiledPreview}</pre>
          </div>
        )}

        {/* Actions */}
        <div className="flex gap-3 pt-3 border-t border-gray-100">
          <button onClick={handleSave} disabled={saving || !name.trim()}
            className="px-5 py-2 text-sm bg-gray-900 text-white rounded-lg hover:bg-gray-800 disabled:opacity-50 transition-colors">
            {saving ? '保存中...' : isEdit ? '更新写法' : '创建写法'}
          </button>
          <button onClick={handlePreview}
            className="px-4 py-2 text-sm border border-gray-300 text-gray-600 rounded-lg hover:bg-gray-50 transition-colors">
            编译预览
          </button>
          <button onClick={onClose}
            className="px-4 py-2 text-sm text-gray-500 hover:text-gray-700 transition-colors">
            取消
          </button>
        </div>
      </div>
    </div>
  )
}

/* ─── Detect from Text Modal ───────────────────────────────── */

function DetectModal({ onClose, onCreated }: { onClose: () => void; onCreated: () => void }) {
  const [text, setText] = useState('')
  const [name, setName] = useState('')
  const [detecting, setDetecting] = useState(false)

  const handleDetect = async () => {
    if (text.length < 200) { alert('文本至少需要200字'); return }
    setDetecting(true)
    try {
      await apiFetch('/api/styles/detect', {
        method: 'POST',
        body: JSON.stringify({ text, name: name || '' }),
      })
      onCreated()
    } catch (e) {
      alert(e instanceof Error ? e.message : '检测失败')
    } finally {
      setDetecting(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div className="bg-white rounded-2xl shadow-xl w-full max-w-lg mx-4 p-6 space-y-4">
        <h3 className="text-lg font-bold text-gray-900">从文本检测写法</h3>
        <p className="text-xs text-gray-500">粘贴一段参考小说文本（至少200字），AI 将自动分析其写作风格并创建写法档案。</p>

        <input value={name} onChange={e => setName(e.target.value)}
          placeholder='写法名称（选填，默认"自动检测的写法"）'
          className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg" />

        <textarea value={text} onChange={e => setText(e.target.value)}
          placeholder="粘贴参考文本..."
          className="w-full h-48 px-3 py-2 text-sm border border-gray-200 rounded-lg resize-none" />

        <div className="flex items-center justify-between">
          <span className="text-xs text-gray-400">{text.length} 字{text.length < 200 ? ' (至少200字)' : ''}</span>
          <div className="flex gap-2">
            <button onClick={onClose} className="px-4 py-2 text-sm text-gray-500 hover:text-gray-700">取消</button>
            <button onClick={handleDetect} disabled={detecting || text.length < 200}
              className="px-5 py-2 text-sm bg-gray-900 text-white rounded-lg disabled:opacity-50">
              {detecting ? '分析中...' : '检测风格'}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
