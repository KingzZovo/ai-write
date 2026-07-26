'use client'

import React, { useState, useEffect } from 'react'
import { apiFetch } from '@/lib/api'
import { useT } from '@/lib/i18n/I18nProvider'

interface Foreshadow {
  id: string
  type: string
  description: string
  planted_chapter: number
  resolve_conditions_json: unknown
  narrative_proximity: number | null
  status: string
  resolved_chapter: number | null
}

interface ForeshadowResponse { foreshadows: Foreshadow[]; total: number }

interface ForeshadowPanelProps { projectId: string }

const STATUS_CFG: Record<string, { color: string; label: string; help: string }> = {
  pending:  { color: 'bg-stone-100 text-stone-600',     label: '待启动', help: '已抽取但尚未启动状态机。' },
  planted:  { color: 'bg-emerald-100 text-emerald-700', label: '已埋',     help: '伏笔已埋下，距离收束还早。' },
  ripening: { color: 'bg-amber-100 text-amber-700',     label: '酝酿中', help: '剧情在推进，接近可以收的阶段。' },
  ready:    { color: 'bg-rose-100 text-rose-700',       label: '该收了', help: '已足够接近该伏笔的收线点，建议设法呈现。' },
  resolved: { color: 'bg-stone-100 text-stone-500',     label: '已收',     help: '伏笔已被回收 / 兑现。' },
}

const TYPE_CFG: Record<string, { label: string; color: string }> = {
  // 中文分类（LLM 抽取输出）
  '明伏笔':       { label: '明', color: 'text-amber-700' },
  '暗伏笔':       { label: '暗', color: 'text-stone-600' },
  '锻造':           { label: '锻', color: 'text-rose-600' },
  '伏笔':           { label: '伏', color: 'text-amber-700' },
  // 新分类 (后端抽取实际使用)
  plot:         { label: '主线', color: 'text-amber-600' },
  character:    { label: '人物', color: 'text-rose-600' },
  worldbuilding:{ label: '设定', color: 'text-indigo-600' },
  mystery:      { label: '谜团', color: 'text-purple-600' },
  // 旧分类 (手动录入)
  major:        { label: '主',     color: 'text-amber-700' },
  minor:        { label: '次',     color: 'text-stone-600' },
  hint:         { label: '暗示', color: 'text-stone-500' },
}

/** Build the PUT /foreshadows/{id} body from the inline edit form.
 *  Exported for tests. Conditions are one-per-line, trimmed, blanks dropped. */
export function buildForeshadowUpdatePayload(input: { description: string; type: string; conditionsText: string }) {
  return {
    description: input.description.trim(),
    type: input.type,
    resolve_conditions: input.conditionsText.split('\n').map(c => c.trim()).filter(Boolean),
  }
}

/** Parse the window.prompt answer for 标记回收. Returns null when cancelled or
 *  not a non-negative integer. Exported for tests. */
export function parseResolvedChapter(raw: string | null): number | null {
  if (raw === null) return null
  const trimmed = raw.trim()
  if (!/^\d+$/.test(trimmed)) return null
  return Number(trimmed)
}

/** Normalize resolve_conditions_json (list or object) into a display list. */
function conditionsToList(rc: unknown): string[] {
  if (Array.isArray(rc)) return rc.map(v => String(v))
  if (rc && typeof rc === 'object') return Object.values(rc).map(v => String(v))
  return []
}

export function ForeshadowPanel({ projectId }: ForeshadowPanelProps) {
  const t = useT()
  const [foreshadows, setForeshadows] = useState<Foreshadow[]>([])
  const [loading, setLoading] = useState(false)
  const [showForm, setShowForm] = useState(false)
  const [filter, setFilter] = useState<'active' | 'all'>('active')
  const [typeFilter, setTypeFilter] = useState<string>('all')

  const fetchForeshadows = async () => {
    if (!projectId) return
    setLoading(true)
    try {
      const data = await apiFetch<ForeshadowResponse | Foreshadow[]>(`/api/projects/${projectId}/foreshadows`)
      const all = Array.isArray(data) ? data : (data.foreshadows || [])
      setForeshadows(all)
    } catch { setForeshadows([]) } finally { setLoading(false) }
  }

  useEffect(() => { fetchForeshadows() }, [projectId]) // eslint-disable-line react-hooks/exhaustive-deps

  const visible = foreshadows.filter(f => {
    if (filter === 'active' && f.status === 'resolved') return false
    if (typeFilter !== 'all' && f.type !== typeFilter) return false
    return true
  })

  const grouped = {
    ready:    visible.filter(f => f.status === 'ready'),
    ripening: visible.filter(f => f.status === 'ripening'),
    planted:  visible.filter(f => f.status === 'planted'),
    resolved: visible.filter(f => f.status === 'resolved'),
  }

  const typeOptions = Array.from(new Set(foreshadows.map(f => f.type).filter(Boolean)))

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-1.5">
          <h3 className="text-sm font-semibold text-gray-900">伏笔追踪</h3>
          <span className="text-[10px] text-stone-400">共 {foreshadows.length} / 当前 {visible.length}</span>
        </div>
        <button onClick={() => setShowForm(!showForm)} className="rounded-md px-1.5 py-0.5 text-xs text-blue-600 transition-colors duration-150 hover:bg-blue-50 hover:text-blue-700">+ 手动添加</button>
      </div>

      <div className="flex items-center gap-1">
        <button onClick={() => setFilter('active')} className={`px-2.5 py-1 text-xs rounded-full transition-colors duration-150 ${filter === 'active' ? 'bg-blue-100 font-medium text-blue-700' : 'text-stone-500 hover:bg-stone-100'}`} title="排除已收的伏笔">{t('foreshadow.filter.active')}</button>
        <button onClick={() => setFilter('all')} className={`px-2.5 py-1 text-xs rounded-full transition-colors duration-150 ${filter === 'all' ? 'bg-blue-100 font-medium text-blue-700' : 'text-stone-500 hover:bg-stone-100'}`} title="含已收的全部伏笔">{t('foreshadow.filter.all')}</button>
        {typeOptions.length > 1 && (
          <select value={typeFilter} onChange={e => setTypeFilter(e.target.value)} className="ml-auto text-xs border border-stone-200 rounded px-1.5 py-1">
            <option value="all">全类型</option>
            {typeOptions.map(t => <option key={t} value={t}>{TYPE_CFG[t]?.label || t}</option>)}
          </select>
        )}
      </div>

      {/* legend / help */}
      <div className="flex flex-wrap gap-x-2.5 gap-y-1 text-[10px] text-stone-500 bg-stone-50/60 rounded px-2 py-1.5">
        {Object.entries(STATUS_CFG).map(([k, v]) => (
          <span key={k} title={v.help} className="flex items-center gap-1">
            <span className={`inline-block px-1 rounded ${v.color}`} style={{fontSize: 9}} >{v.label}</span>
            <span className="text-stone-400">{v.help.length > 18 ? v.help.slice(0, 18) + '…' : v.help}</span>
          </span>
        ))}
      </div>

      {showForm && <ForeshadowForm projectId={projectId} onCreated={() => { setShowForm(false); fetchForeshadows() }} />}

      {loading ? (
        <p className="text-xs text-gray-400">加载中...</p>
      ) : visible.length === 0 ? (
        <div className="flex flex-col items-center gap-1 py-4 text-center">
          <span className="text-lg" aria-hidden>🌱</span>
          <p className="text-xs text-gray-400">暂无伏笔。生成正文或手动添加后会出现在这里。</p>
        </div>
      ) : (
        <div className="space-y-3">
          {grouped.ready.length    > 0 && <Section title={t('foreshadow.section.ready')}   items={grouped.ready} projectId={projectId} onChanged={fetchForeshadows} />}
          {grouped.ripening.length > 0 && <Section title={t('foreshadow.section.ripening')} items={grouped.ripening} projectId={projectId} onChanged={fetchForeshadows} />}
          {grouped.planted.length  > 0 && <Section title={t('foreshadow.section.planted')}     items={grouped.planted} projectId={projectId} onChanged={fetchForeshadows} />}
          {filter === 'all' && grouped.resolved.length > 0 && <Section title={t('foreshadow.section.resolved')} items={grouped.resolved} projectId={projectId} onChanged={fetchForeshadows} />}
        </div>
      )}
    </div>
  )
}

function Section({ title, items, projectId, onChanged }: { title: string; items: Foreshadow[]; projectId: string; onChanged: () => void }) {
  return (
    <div>
      <h4 className="text-[10px] font-medium text-stone-500 mb-1 uppercase tracking-wider">{title} <span className="text-stone-300">({items.length})</span></h4>
      <div className="space-y-1.5">
        {items.map(f => <ForeshadowCard key={f.id} foreshadow={f} projectId={projectId} onChanged={onChanged} />)}
      </div>
    </div>
  )
}

function ForeshadowCard({ foreshadow: f, projectId, onChanged }: { foreshadow: Foreshadow; projectId: string; onChanged: () => void }) {
  const t = useT()
  const [editing, setEditing] = useState(false)
  const [busy, setBusy] = useState(false)
  const statusCfg = STATUS_CFG[f.status] || STATUS_CFG.planted
  const typeCfg = TYPE_CFG[f.type] || { label: f.type, color: 'text-stone-500' }
  const proximity = (typeof f.narrative_proximity === "number" && Number.isFinite(f.narrative_proximity)) ? f.narrative_proximity : 0
  const proximityWidth = Math.round(proximity * 100)

  const handleResolve = async () => {
    const ch = parseResolvedChapter(window.prompt(t('foreshadow.resolvePrompt')))
    if (ch === null) return
    setBusy(true)
    try {
      await apiFetch(`/api/projects/${projectId}/foreshadows/${f.id}/resolve`, {
        method: 'POST',
        body: JSON.stringify({ resolved_chapter: ch }),
      })
      onChanged()
    } catch (e) {
      window.alert(e instanceof Error ? e.message : String(e))
    } finally { setBusy(false) }
  }

  const handleDelete = async () => {
    if (!window.confirm(t('foreshadow.deleteConfirm'))) return
    setBusy(true)
    try {
      await apiFetch(`/api/projects/${projectId}/foreshadows/${f.id}`, { method: 'DELETE' })
      onChanged()
    } catch (e) {
      window.alert(e instanceof Error ? e.message : String(e))
    } finally { setBusy(false) }
  }

  return (
    <div className={`bg-white border border-stone-200 rounded-lg p-2.5 text-xs transition-colors duration-150 hover:border-stone-300 ${f.status === 'resolved' ? 'opacity-60' : ''}`}>
      <div className="flex items-center gap-1.5 mb-1">
        <span className={`px-1.5 py-0.5 rounded text-[10px] font-medium ${statusCfg.color}`} title={statusCfg.help}>{statusCfg.label}</span>
        <span className={`text-[10px] ${typeCfg.color}`}>{typeCfg.label}</span>
        <span className="text-stone-300 ml-auto">第 {f.planted_chapter} 章</span>
      </div>
      {editing ? (
        <ForeshadowEditForm
          foreshadow={f}
          projectId={projectId}
          onDone={() => { setEditing(false); onChanged() }}
          onCancel={() => setEditing(false)}
        />
      ) : (
        <>
      <p className="text-stone-700 leading-relaxed">{f.description}</p>
      {(() => { const list = conditionsToList(f.resolve_conditions_json); return list.length > 0 && (
        <ul className="mt-1 space-y-0.5 text-[10px] text-stone-500">
          {list.slice(0, 3).map((c, i) => <li key={i}>· {String(c)}</li>)}
        </ul>
      ) })()}
      {f.status !== 'resolved' && (
        <div className="mt-1.5 flex items-center gap-1.5" title={`叙事接近度 ${proximityWidth}%，指当前剧情走到多近该收线点。`}>
          <span className="text-[10px] text-stone-400 w-12 flex-shrink-0">接近度</span>
          <div className="flex-1 h-1 bg-stone-100 rounded-full overflow-hidden">
            <div className={`h-full rounded-full transition-[width] duration-300 ${proximity > 0.9 ? 'bg-rose-500' : proximity > 0.7 ? 'bg-amber-500' : 'bg-blue-400'}`} style={{ width: `${proximityWidth}%` }} />
          </div>
          <span className="text-stone-400 w-9 text-right">{proximityWidth}%</span>
        </div>
      )}
      {f.resolved_chapter && (
        <p className="text-stone-400 mt-1">已于第 {f.resolved_chapter} 章收线</p>
      )}
      <div className="mt-1.5 flex items-center gap-2 border-t border-stone-100 pt-1.5">
        <button onClick={() => setEditing(true)} disabled={busy} className="text-[10px] text-blue-500 hover:text-blue-600 disabled:opacity-50">{t('common.edit')}</button>
        {f.status !== 'resolved' && (
          <button onClick={handleResolve} disabled={busy} className="text-[10px] text-emerald-600 hover:text-emerald-700 disabled:opacity-50">{t('foreshadow.action.resolve')}</button>
        )}
        <button onClick={handleDelete} disabled={busy} className="ml-auto text-[10px] text-rose-400 hover:text-rose-500 disabled:opacity-50">{t('common.delete')}</button>
      </div>
        </>
      )}
    </div>
  )
}

function ForeshadowEditForm({ foreshadow: f, projectId, onDone, onCancel }: { foreshadow: Foreshadow; projectId: string; onDone: () => void; onCancel: () => void }) {
  const t = useT()
  const [desc, setDesc] = useState(f.description)
  const [type, setType] = useState(f.type)
  const [conditions, setConditions] = useState(conditionsToList(f.resolve_conditions_json).join('\n'))
  const [saving, setSaving] = useState(false)
  const knownTypes = ['plot', 'character', 'worldbuilding', 'mystery']
  const handleSave = async () => {
    if (!desc.trim()) return
    setSaving(true)
    try {
      await apiFetch(`/api/projects/${projectId}/foreshadows/${f.id}`, {
        method: 'PUT',
        body: JSON.stringify(buildForeshadowUpdatePayload({ description: desc, type, conditionsText: conditions })),
      })
      onDone()
    } catch (e) {
      window.alert(e instanceof Error ? e.message : String(e))
    } finally { setSaving(false) }
  }
  return (
    <div className="space-y-1.5">
      <select value={type} onChange={e => setType(e.target.value)} className="w-full px-2 py-1 text-xs border border-stone-200 rounded transition-shadow duration-150 focus:outline-none focus:ring-2 focus:ring-blue-400/40 focus:border-blue-300">
        {!knownTypes.includes(type) && <option value={type}>{TYPE_CFG[type]?.label || type}</option>}
        <option value="plot">{t('foreshadow.type.plot')}</option>
        <option value="character">{t('foreshadow.type.character')}</option>
        <option value="worldbuilding">{t('foreshadow.type.worldbuilding')}</option>
        <option value="mystery">{t('foreshadow.type.mystery')}</option>
      </select>
      <textarea value={desc} onChange={e => setDesc(e.target.value)} className="w-full px-2 py-1 text-xs border border-stone-200 rounded resize-none h-16 transition-shadow duration-150 focus:outline-none focus:ring-2 focus:ring-blue-400/40 focus:border-blue-300" />
      <label className="text-[10px] text-stone-400">{t('foreshadow.conditionsLabel')}</label>
      <textarea value={conditions} onChange={e => setConditions(e.target.value)} className="w-full px-2 py-1 text-xs border border-stone-200 rounded resize-none h-12 transition-shadow duration-150 focus:outline-none focus:ring-2 focus:ring-blue-400/40 focus:border-blue-300" />
      <div className="flex gap-1.5">
        <button onClick={handleSave} disabled={saving || !desc.trim()} className="flex-1 px-2 py-1 text-xs font-medium bg-blue-600 text-white rounded transition-colors duration-150 hover:bg-blue-700 disabled:opacity-50">{saving ? t('common.saving') : t('common.save')}</button>
        <button onClick={onCancel} disabled={saving} className="flex-1 px-2 py-1 text-xs bg-white border border-stone-200 text-stone-600 rounded transition-colors duration-150 hover:bg-stone-100 disabled:opacity-50">{t('common.cancel')}</button>
      </div>
    </div>
  )
}

function ForeshadowForm({ projectId, onCreated }: { projectId: string; onCreated: () => void }) {
  const t = useT()
  const [desc, setDesc] = useState('')
  const [type, setType] = useState('plot')
  const [conditions, setConditions] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const handleSubmit = async () => {
    if (!desc.trim()) return
    setSubmitting(true)
    try {
      await apiFetch(`/api/projects/${projectId}/foreshadows`, {
        method: 'POST',
        body: JSON.stringify({ description: desc, type, planted_chapter: 0, resolve_conditions: conditions.split('\n').map(c => c.trim()).filter(Boolean) }),
      })
      onCreated()
    } catch { /* ignore */ } finally { setSubmitting(false) }
  }
  return (
    <div className="bg-stone-50 rounded-lg p-3 space-y-2">
      <select value={type} onChange={e => setType(e.target.value)} className="w-full px-2 py-1 text-xs border border-stone-200 rounded transition-shadow duration-150 focus:outline-none focus:ring-2 focus:ring-blue-400/40 focus:border-blue-300">
        <option value="plot">{t('foreshadow.type.plot')}</option>
        <option value="character">{t('foreshadow.type.character')}</option>
        <option value="worldbuilding">{t('foreshadow.type.worldbuilding')}</option>
        <option value="mystery">{t('foreshadow.type.mystery')}</option>
      </select>
      <textarea value={desc} onChange={e => setDesc(e.target.value)} placeholder="描述伏笔..." className="w-full px-2 py-1 text-xs border border-stone-200 rounded resize-none h-16 transition-shadow duration-150 focus:outline-none focus:ring-2 focus:ring-blue-400/40 focus:border-blue-300" />
      <textarea value={conditions} onChange={e => setConditions(e.target.value)} placeholder="收线条件 (一行一条)..." className="w-full px-2 py-1 text-xs border border-stone-200 rounded resize-none h-12 transition-shadow duration-150 focus:outline-none focus:ring-2 focus:ring-blue-400/40 focus:border-blue-300" />
      <button onClick={handleSubmit} disabled={submitting || !desc.trim()} className="w-full px-2 py-1.5 text-xs font-medium bg-blue-600 text-white rounded transition-colors duration-150 hover:bg-blue-700 disabled:opacity-50">{submitting ? '创建中...' : '创建伏笔'}</button>
    </div>
  )
}
