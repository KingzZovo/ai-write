'use client'

import React, { useState, useEffect, useRef } from 'react'
import { CharacterProfileEditor } from './CharacterProfileEditor'
import { apiFetch, ApiError } from '@/lib/api'
import { useT } from '@/lib/i18n/I18nProvider'

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
  version: string
  source_status?: string
  source_message?: string
}

interface CharResp { characters?: Character[]; total?: number }
interface RuleResp { rules?: WorldRule[]; world_rules?: WorldRule[]; total?: number }

interface SettingsPanelProps { projectId: string }

export function SettingsPanel({ projectId }: SettingsPanelProps) {
  return <SettingsPanelSession key={projectId} projectId={projectId} />
}

function SettingsPanelSession({ projectId }: SettingsPanelProps) {
  const t = useT()
  const [tab, setTab] = useState<'characters' | 'world'>('characters')
  const [characters, setCharacters] = useState<Character[]>([])
  const [worldRules, setWorldRules] = useState<WorldRule[]>([])
  const [loading, setLoading] = useState(true)
  const [editingChar, setEditingChar] = useState<string | null>(null)
  const [showAddChar, setShowAddChar] = useState(false)
  const [showAddRule, setShowAddRule] = useState(false)
  const [editingRule, setEditingRule] = useState<WorldRule | null>(null)
  const [ruleWriteBusy, setRuleWriteBusy] = useState(false)
  const [ruleDeleteNotice, setRuleDeleteNotice] = useState('')
  const [deletingRuleId, setDeletingRuleId] = useState<string | null>(null)
  const ruleDeletion = useRef<string | null>(null)
  const [loadError, setLoadError] = useState('')
  const [deleteNotice, setDeleteNotice] = useState('')
  const deleteTokens = useRef(new Map<string, string>())
  const deleting = useRef(new Set<string>())
  const alive = useRef(true)
  const charRead = useRef(0)
  const ruleRead = useRef(0)
  const timers = useRef<ReturnType<typeof setTimeout>[]>([])
  const controller = useRef<AbortController | null>(null)

  const fetchCharacters = () => {
    if (!projectId || !alive.current) return Promise.resolve()
    const read = ++charRead.current
    return apiFetch<Character[] | CharResp>(`/api/projects/${projectId}/characters`, { signal: controller.current?.signal })
      .then((data) => {
        const arr = Array.isArray(data) ? data : data.characters
        if (!Array.isArray(arr)) throw new Error('Invalid character list')
        if (alive.current && read === charRead.current) {
          setCharacters(arr)
          setLoadError(current => current === '人物列表读取失败，请重试。' ? '' : current)
        }
        return arr
      })
      .catch(() => { if (alive.current && read === charRead.current) setLoadError('人物列表读取失败，请重试。') })
  }

  const fetchWorldRules = () => {
    if (!projectId || !alive.current) return Promise.resolve()
    const read = ++ruleRead.current
    return apiFetch<WorldRule[] | RuleResp>(`/api/projects/${projectId}/world-rules`, { signal: controller.current?.signal })
      .then((data) => {
        const arr = Array.isArray(data) ? data : (data.world_rules || data.rules)
        if (!Array.isArray(arr) || arr.some(rule => typeof rule.id !== 'string'
          || typeof rule.category !== 'string' || typeof rule.rule_text !== 'string'
          || typeof rule.version !== 'string' || !/^[0-9a-f]{64}$/.test(rule.version))) throw new Error('Invalid world rule source')
        if (alive.current && read === ruleRead.current) {
          setWorldRules(arr)
          setLoadError(current => current === '世界规则读取失败，请重试。' ? '' : current)
          return arr
        }
      })
      .catch(() => { if (alive.current && read === ruleRead.current) setLoadError('世界规则读取失败，请重试。') })
  }

  useEffect(() => {
    alive.current = true
    let stopped = false
    controller.current = new AbortController()
    Promise.all([fetchCharacters(), fetchWorldRules()]).finally(() => { if (!stopped) setLoading(false) })
    return () => { stopped = true; alive.current = false; controller.current?.abort(); timers.current.forEach(clearTimeout) }
  }, [projectId]) // eslint-disable-line react-hooks/exhaustive-deps

  const handleDeleteChar = async (char: Character) => {
    if (deleting.current.has(char.id) || !window.confirm(t('character.deleteConfirm'))) return
    const token = deleteTokens.current.get(char.id) || crypto.randomUUID()
    deleteTokens.current.set(char.id, token)
    deleting.current.add(char.id)
    setDeleteNotice('正在删除并核对…')
    const verify = async () => {
      if (!alive.current) return
      const rows = await fetchCharacters()
      if (!alive.current) return
      if (rows && !rows.some(item => item.id === char.id)) {
        setDeleteNotice('删除已完成并核对。')
        deleteTokens.current.delete(char.id)
        deleting.current.delete(char.id)
      } else {
        setDeleteNotice('删除已提交，后台正在清理页面资料。')
        timers.current.push(setTimeout(() => { void verify() }, 5000))
      }
    }
    try {
      const result = await apiFetch<{ status: string }>(`/api/projects/${projectId}/neo4j-settings/characters`, {
        method: 'DELETE', body: JSON.stringify({ name: char.name, expected_profile_json: char.profile_json ?? {}, deletion_token: token }),
      })
      if (result.status !== 'accepted') throw new Error('删除结果未获确认，请核对后重试。')
      await verify()
    } catch (err) {
      if (!alive.current) return
      if (err instanceof ApiError && err.code === 'character_deletion_pending') {
        setDeleteNotice('删除已提交，后台正在清理页面资料。')
        timers.current.push(setTimeout(() => { void verify() }, 5000))
      } else {
        setDeleteNotice('')
        deleting.current.delete(char.id)
        setLoadError(err instanceof Error ? err.message : '删除失败，原列表已保留。')
      }
    }
  }

  const handleDeleteRule = async (rule: WorldRule) => {
    if (ruleDeletion.current || ruleWriteBusy || !window.confirm(t('worldRule.deleteConfirm'))) return
    ruleDeletion.current = rule.id
    setDeletingRuleId(rule.id)
    setRuleDeleteNotice('正在删除并核对当前规则…')
    try {
      await apiFetch(`/api/projects/${projectId}/neo4j-settings/world-rules`, {
        method: 'DELETE',
        body: JSON.stringify({ category: rule.category, text: rule.rule_text, expected_version: rule.version }),
      })
    } catch (err) {
      if (alive.current) setRuleDeleteNotice(err instanceof Error ? err.message : '删除结果未确认，原列表已保留。')
    } finally {
      const rows = await fetchWorldRules()
      if (alive.current) {
        if (rows && !rows.some(item => item.id === rule.id)) setRuleDeleteNotice('已核对，原规则已删除。')
        else if (!rows) setRuleDeleteNotice('删除结果未确认，请重新读取核对；原列表已保留。')
        else setRuleDeleteNotice('当前仍能读取原规则，删除尚未确认，请核对后重试。')
        setDeletingRuleId(null)
      }
      ruleDeletion.current = null
    }
  }

  return (
    <div className="space-y-3">
      <h3 className="text-sm font-semibold text-gray-900">设定集</h3>
      <div className="flex border-b border-stone-200">
        {(['characters', 'world'] as const).map(t => (
          <button key={t} disabled={ruleWriteBusy || deletingRuleId !== null} onClick={() => setTab(t)} className={`-mb-px px-3 py-1.5 text-xs font-medium border-b-2 transition-colors duration-150 disabled:opacity-50 ${tab === t ? 'border-blue-600 text-blue-600' : 'border-transparent text-stone-500 hover:border-stone-300 hover:text-stone-700'}`}>
            {t === 'characters' ? `人物 (${characters.length})` : `世界规则 (${worldRules.length})`}
          </button>
        ))}
      </div>

      {deleteNotice && <p role="status" className="text-xs text-stone-600">{deleteNotice}</p>}
      {loadError && <div role="alert" className="text-xs text-rose-600">{loadError}
        <button onClick={() => { setLoadError(''); void Promise.all([fetchCharacters(), fetchWorldRules()]) }} className="ml-2 text-blue-600">重试读取</button>
      </div>}
      {loading ? (
        <p className="text-xs text-gray-400">加载中...</p>
      ) : tab === 'characters' ? (
        <div className="space-y-2">
          {characters.map(char => {
            const profile = char.profile_json || {}
            return (
              <div key={char.name} className="bg-white border border-stone-200 rounded-lg p-2.5 transition-colors duration-150 hover:border-stone-300">
                <div className="flex items-center justify-between mb-1">
                  <span className="text-xs font-medium text-stone-800">{char.name}</span>
                  <span className="flex items-center gap-1.5">
                    <button onClick={() => setEditingChar(editingChar === char.name ? null : char.name)} className="text-[10px] text-blue-500 hover:text-blue-600">
                      {editingChar === char.name ? '收起' : '编辑'}
                    </button>
                    <button onClick={() => handleDeleteChar(char)} className="text-[10px] text-rose-400 hover:text-rose-500">
                      {t('common.delete')}
                    </button>
                  </span>
                </div>
                {typeof profile.identity === 'string' && profile.identity && (
                  <p className="text-[11px] text-stone-500">{profile.identity}</p>
                )}
                {editingChar === char.name && (
                  <CharacterProfileEditor projectId={projectId} character={char} onSaved={saved => {
                    ++charRead.current
                    setCharacters(previous => previous.map(item => item.name === saved.name ? saved : item))
                  }} />
                )}
              </div>
            )
          })}
          {characters.length === 0 && !showAddChar && !loadError && (
            <div className="flex flex-col items-center gap-1 py-4 text-center">
              <span className="text-lg" aria-hidden>👤</span>
              <p className="text-xs text-gray-400">还没有人物。</p>
            </div>
          )}
          {showAddChar ? (
            <AddCharacterForm projectId={projectId} onDone={() => { setShowAddChar(false); fetchCharacters() }} />
          ) : (
            <button onClick={() => setShowAddChar(true)} className="w-full px-2 py-1.5 text-xs border border-dashed border-stone-300 rounded-lg text-stone-500 transition-colors duration-150 hover:border-stone-400 hover:bg-stone-50 hover:text-stone-700">+ 添加人物</button>
          )}
        </div>
      ) : (
        <div className="space-y-2">
          {/* PUT/DELETE /world-rules/{id} are 410-disabled (Neo4j is source of
              truth); edits/deletes go through the neo4j-settings routes, which
              match the node by (category, text) and re-materialize PG. */}
          {ruleDeleteNotice && <p role="status" className="text-xs text-stone-600">{ruleDeleteNotice}</p>}
          {editingRule && <div className="rounded-lg border border-blue-200 bg-white p-2.5">
            <p className="mb-2 text-xs text-stone-600">编辑世界规则</p>
            <EditWorldRuleForm key={editingRule.id} projectId={projectId} rule={editingRule}
              onBusy={setRuleWriteBusy} onReadCurrent={fetchWorldRules}
              onDone={() => { setEditingRule(null); void fetchWorldRules() }} onCancel={() => setEditingRule(null)} />
          </div>}
          {worldRules.map(rule => (
            <div key={rule.id} className="bg-white border border-stone-200 rounded-lg p-2.5 transition-colors duration-150 hover:border-stone-300">
              <div className="flex items-center justify-between mb-1">
                <span className="text-[10px] font-medium text-blue-600 uppercase">{rule.category}</span>
                <span className="flex items-center gap-1.5">
                  <button disabled={ruleWriteBusy || deletingRuleId !== null || showAddRule || (editingRule !== null && editingRule.id !== rule.id)} onClick={() => setEditingRule(editingRule?.id === rule.id ? null : rule)} className="text-[10px] text-blue-500 hover:text-blue-600 disabled:opacity-50">
                    {editingRule?.id === rule.id ? '收起' : rule.source_status === 'needs_review' ? '核对并编辑' : t('common.edit')}
                  </button>
                  <button disabled={ruleWriteBusy || deletingRuleId !== null || editingRule !== null || showAddRule} onClick={() => handleDeleteRule(rule)} className="text-[10px] text-rose-400 hover:text-rose-500 disabled:opacity-50">
                    {t('common.delete')}
                  </button>
                </span>
              </div>
              {rule.source_status === 'needs_review' && <p role="status" className="mb-1 text-xs text-amber-700">{rule.source_message}</p>}
              <p className="text-xs text-stone-700 leading-relaxed whitespace-pre-wrap">{rule.rule_text}</p>
            </div>
          ))}
          {worldRules.length === 0 && !showAddRule && !loadError && (
            <div className="flex flex-col items-center gap-1 py-4 text-center">
              <span className="text-lg" aria-hidden>🌍</span>
              <p className="text-xs text-gray-400">还没有世界规则。</p>
            </div>
          )}
          {showAddRule ? (
            <AddWorldRuleForm projectId={projectId} onBusy={setRuleWriteBusy} onDone={() => { setShowAddRule(false); fetchWorldRules() }} />
          ) : (
            <button disabled={ruleWriteBusy || deletingRuleId !== null || editingRule !== null} onClick={() => setShowAddRule(true)} className="w-full px-2 py-1.5 text-xs border border-dashed border-stone-300 rounded-lg text-stone-500 transition-colors duration-150 hover:border-stone-400 hover:bg-stone-50 hover:text-stone-700 disabled:opacity-50">+ 添加规则</button>
          )}
        </div>
      )}
    </div>
  )
}

function AddCharacterForm({ projectId, onDone }: { projectId: string; onDone: () => void }) {
  const [name, setName] = useState('')
  const [identity, setIdentity] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const pending = useRef(false)
  const active = useRef(true)
  useEffect(() => { active.current = true; return () => { active.current = false } }, [])
  const handleSubmit = async () => {
    if (!name.trim() || pending.current) return
    pending.current = true
    setBusy(true); setError('')
    try {
      const result = await apiFetch<{ status: string; entity: string; name: string }>(`/api/projects/${projectId}/neo4j-settings/characters`, {
        method: 'POST', body: JSON.stringify({ name: name.trim(), profile_json: { identity }, expected_profile_json: null }),
      })
      if (result.status !== 'accepted' || result.entity !== 'character' || result.name !== name.trim()) throw new Error('添加结果未获确认，请核对后重试。')
      if (active.current) onDone()
    } catch (e) {
      if (active.current) setError(e instanceof Error ? e.message : '添加失败，输入内容已保留。')
    } finally {
      pending.current = false
      if (active.current) setBusy(false)
    }
  }
  return (
    <div className="bg-stone-50 rounded-lg p-2.5 space-y-1.5">
      <input disabled={busy} value={name} onChange={e => setName(e.target.value)} placeholder="姓名" className="w-full px-2 py-1 text-xs border border-stone-200 rounded transition-shadow duration-150 focus:outline-none focus:ring-2 focus:ring-blue-400/40 focus:border-blue-300" />
      <input disabled={busy} value={identity} onChange={e => setIdentity(e.target.value)} placeholder="身份 / 角色" className="w-full px-2 py-1 text-xs border border-stone-200 rounded transition-shadow duration-150 focus:outline-none focus:ring-2 focus:ring-blue-400/40 focus:border-blue-300" />
      {error && <p role="alert" className="text-xs text-rose-600">{error}</p>}
      <div className="flex gap-1.5">
        <button disabled={busy} onClick={handleSubmit} className="flex-1 px-2 py-1 text-xs font-medium bg-blue-600 text-white rounded transition-colors duration-150 hover:bg-blue-700 disabled:opacity-50">{busy ? '添加中…' : '添加'}</button>
        <button disabled={busy} onClick={onDone} className="flex-1 px-2 py-1 text-xs bg-white border border-stone-200 text-stone-600 rounded transition-colors duration-150 hover:bg-stone-100">取消</button>
      </div>
    </div>
  )
}

function EditWorldRuleForm({ projectId, rule, onDone, onCancel, onBusy, onReadCurrent }: { projectId: string; rule: WorldRule; onDone: () => void; onCancel: () => void; onBusy: (busy: boolean) => void; onReadCurrent: () => Promise<WorldRule[] | void> }) {
  const t = useT()
  const [base, setBase] = useState(rule)
  const [category, setCategory] = useState(base.category)
  const [text, setText] = useState(base.rule_text)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [current, setCurrent] = useState<WorldRule | null>(null)
  const [readNotice, setReadNotice] = useState('')
  const [reading, setReading] = useState(false)
  const saving = useRef(false)
  const readCurrent = async () => {
    if (saving.current) return
    saving.current = true; setReading(true); onBusy(true)
    try {
      const rows = await onReadCurrent()
      const saved = rows?.find(item => item.id === base.id)
      setCurrent(saved || null)
      setReadNotice(!rows ? '当前规则读取失败，编辑稿已保留。' : saved
        ? '请对照当前规则，再决定是否以这个版本继续编辑。'
        : '当前列表已没有原规则，编辑稿仍保留；不会把它写入后来创建的规则。')
    } finally { saving.current = false; setReading(false); onBusy(false) }
  }
  const handleSave = async () => {
    if (saving.current || !category.trim() || !text.trim()) return
    saving.current = true
    setBusy(true); onBusy(true); setError('')
    try {
      // The Neo4j node is keyed by (category, text): the PUT matches it by
      // (category, old_text) and rewrites in place, then re-materializes PG.
      const result = await apiFetch<{ status?: string; entity?: string; category?: string; text?: string; deduplicated?: boolean } | null>(`/api/projects/${projectId}/neo4j-settings/world-rules`, {
        method: 'PUT',
        body: JSON.stringify({
          category: base.category,
          old_text: base.rule_text,
          new_text: text.trim(),
          new_category: category.trim(),
          expected_version: base.version,
        }),
      })
      if (result?.status !== 'accepted' || result.entity !== 'world_rule' || result.category !== category.trim()
        || result.text !== text.trim() || typeof result.deduplicated !== 'boolean') {
        throw new Error('保存回执不完整，编辑稿已保留，请重新读取核对。')
      }
      onDone()
    } catch (err) {
      setError(err instanceof Error ? err.message : '保存未确认，输入已保留，请重新读取核对。')
    } finally { saving.current = false; setBusy(false); onBusy(false) }
  }
  return (
    <div className="space-y-1.5">
      <input disabled={busy} aria-label="规则分类" value={category} onChange={e => setCategory(e.target.value)} className="w-full px-2 py-1 text-xs border border-stone-200 rounded transition-shadow duration-150 focus:outline-none focus:ring-2 focus:ring-blue-400/40 focus:border-blue-300" />
      <textarea disabled={busy} aria-label="规则全文" value={text} onChange={e => setText(e.target.value)} className="w-full px-2 py-1 text-xs border border-stone-200 rounded resize-none h-16 transition-shadow duration-150 focus:outline-none focus:ring-2 focus:ring-blue-400/40 focus:border-blue-300" />
      {error && <p role="alert" className="text-xs text-rose-600">{error}</p>}
      <button disabled={busy || reading} onClick={() => void readCurrent()} className="text-xs text-blue-600 disabled:opacity-50">{reading ? '正在读取…' : '重新读取当前规则'}</button>
      {readNotice && <p role="status" className="text-xs text-stone-600">{readNotice}</p>}
      {current && <div className="rounded border border-stone-200 p-2 text-xs">
        <p>当前分类：{current.category}</p>
        <pre className="my-1 whitespace-pre-wrap font-sans">{current.rule_text}</pre>
        <button disabled={busy || reading} onClick={() => { setBase(current); setError(''); setCurrent(null); setReadNotice('已采用读取版本，编辑稿保持不变。') }} className="text-blue-600 disabled:opacity-50">采用读取版本，保留编辑稿</button>
      </div>}
      <div className="flex gap-1.5">
        <button onClick={handleSave} disabled={busy || reading} className="flex-1 px-2 py-1 text-xs font-medium bg-blue-600 text-white rounded transition-colors duration-150 hover:bg-blue-700 disabled:opacity-50">{busy ? t('common.saving') : t('common.save')}</button>
        <button onClick={onCancel} disabled={busy || reading} className="flex-1 px-2 py-1 text-xs bg-white border border-stone-200 text-stone-600 rounded transition-colors duration-150 hover:bg-stone-100 disabled:opacity-50">{t('common.cancel')}</button>
      </div>
    </div>
  )
}

function AddWorldRuleForm({ projectId, onDone, onBusy }: { projectId: string; onDone: () => void; onBusy: (busy: boolean) => void }) {
  const [category, setCategory] = useState('')
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const saving = useRef(false)
  const handleSubmit = async () => {
    if (saving.current || !category.trim() || !text.trim()) return
    saving.current = true; setBusy(true); onBusy(true); setError('')
    // v1.9+: POST /world-rules is 410-disabled; create via Neo4j instead.
    try {
      const result = await apiFetch<{ status?: string; entity?: string; id?: string } | null>(`/api/projects/${projectId}/neo4j-settings/world-rules`, { method: 'POST', body: JSON.stringify({ category, rule_text: text }) })
      if (result?.status !== 'accepted' || result.entity !== 'world_rule' || typeof result.id !== 'string' || !result.id) {
        throw new Error('添加回执不完整，输入已保留，请重新读取核对。')
      }
      onDone()
    } catch (err) {
      setError(err instanceof Error ? err.message : '添加未确认，输入已保留，请重新读取核对。')
    } finally { saving.current = false; setBusy(false); onBusy(false) }
  }
  return (
    <div className="bg-stone-50 rounded-lg p-2.5 space-y-1.5">
      <input disabled={busy} aria-label="规则分类" value={category} onChange={e => setCategory(e.target.value)} placeholder="分类（如能力限制、地理环境）" className="w-full px-2 py-1 text-xs border border-stone-200 rounded transition-shadow duration-150 focus:outline-none focus:ring-2 focus:ring-blue-400/40 focus:border-blue-300" />
      <textarea disabled={busy} aria-label="规则全文" value={text} onChange={e => setText(e.target.value)} placeholder="规则描述..." className="w-full px-2 py-1 text-xs border border-stone-200 rounded resize-none h-16 transition-shadow duration-150 focus:outline-none focus:ring-2 focus:ring-blue-400/40 focus:border-blue-300" />
      {error && <p role="alert" className="text-xs text-rose-600">{error}</p>}
      <div className="flex gap-1.5">
        <button disabled={busy} onClick={handleSubmit} className="flex-1 px-2 py-1 text-xs font-medium bg-blue-600 text-white rounded transition-colors duration-150 hover:bg-blue-700 disabled:opacity-50">{busy ? '添加中…' : '添加'}</button>
        <button disabled={busy} onClick={onDone} className="flex-1 px-2 py-1 text-xs bg-white border border-stone-200 text-stone-600 rounded transition-colors duration-150 hover:bg-stone-100 disabled:opacity-50">取消</button>
      </div>
    </div>
  )
}
