'use client'

import React, { useState, useEffect } from 'react'
import { apiFetch } from '@/lib/api'

interface Foreshadow {
  id: string
  type: string
  description: string
  planted_chapter: number
  resolve_conditions: string[] | null
  narrative_proximity: number
  status: 'planted' | 'ripening' | 'ready' | 'resolved'
  resolved_chapter: number | null
}

interface ForeshadowResponse { foreshadows: Foreshadow[]; total: number }

interface ForeshadowPanelProps { projectId: string }

const STATUS_CFG: Record<string, { color: string; label: string; help: string }> = {
  planted:  { color: 'bg-emerald-100 text-emerald-700', label: '已埋',     help: '伏笔已埋下，距离收束还早。' },
  ripening: { color: 'bg-amber-100 text-amber-700',     label: '酝酿中', help: '剧情在推进，接近可以收的阶段。' },
  ready:    { color: 'bg-rose-100 text-rose-700',       label: '该收了', help: '已足够接近该伏笔的收线点，建议设法呈现。' },
  resolved: { color: 'bg-stone-100 text-stone-500',     label: '已收',     help: '伏笔已被回收 / 兑现。' },
}

const TYPE_CFG: Record<string, { label: string; color: string }> = {
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

export function ForeshadowPanel({ projectId }: ForeshadowPanelProps) {
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
        <button onClick={() => setShowForm(!showForm)} className="text-xs text-blue-600 hover:text-blue-700">+ 手动添加</button>
      </div>

      <div className="flex items-center gap-1">
        <button onClick={() => setFilter('active')} className={`px-2 py-1 text-xs rounded ${filter === 'active' ? 'bg-blue-100 text-blue-700' : 'text-stone-500 hover:bg-stone-100'}`} title="排除已收的伏笔">未收 Active</button>
        <button onClick={() => setFilter('all')} className={`px-2 py-1 text-xs rounded ${filter === 'all' ? 'bg-blue-100 text-blue-700' : 'text-stone-500 hover:bg-stone-100'}`} title="含已收的全部伏笔">全部 All</button>
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
        <p className="text-xs text-gray-400">暂无伏笔。</p>
      ) : (
        <div className="space-y-3">
          {grouped.ready.length    > 0 && <Section title="该收了·Ready"   items={grouped.ready} />}
          {grouped.ripening.length > 0 && <Section title="酝酿中·Ripening" items={grouped.ripening} />}
          {grouped.planted.length  > 0 && <Section title="已埋·Planted"     items={grouped.planted} />}
          {filter === 'all' && grouped.resolved.length > 0 && <Section title="已收·Resolved" items={grouped.resolved} />}
        </div>
      )}
    </div>
  )
}

function Section({ title, items }: { title: string; items: Foreshadow[] }) {
  return (
    <div>
      <h4 className="text-[10px] font-medium text-stone-500 mb-1 uppercase tracking-wider">{title} <span className="text-stone-300">({items.length})</span></h4>
      <div className="space-y-1.5">
        {items.map(f => <ForeshadowCard key={f.id} foreshadow={f} />)}
      </div>
    </div>
  )
}

function ForeshadowCard({ foreshadow: f }: { foreshadow: Foreshadow }) {
  const statusCfg = STATUS_CFG[f.status] || STATUS_CFG.planted
  const typeCfg = TYPE_CFG[f.type] || { label: f.type, color: 'text-stone-500' }
  const proximity = Number.isFinite(f.narrative_proximity) ? f.narrative_proximity : 0
  const proximityWidth = Math.round(proximity * 100)
  return (
    <div className="bg-white border border-stone-200 rounded-lg p-2.5 text-xs">
      <div className="flex items-center gap-1.5 mb-1">
        <span className={`px-1.5 py-0.5 rounded text-[10px] font-medium ${statusCfg.color}`} title={statusCfg.help}>{statusCfg.label}</span>
        <span className={`text-[10px] ${typeCfg.color}`}>{typeCfg.label}</span>
        <span className="text-stone-300 ml-auto">第 {f.planted_chapter} 章</span>
      </div>
      <p className="text-stone-700 leading-relaxed">{f.description}</p>
      {f.resolve_conditions && f.resolve_conditions.length > 0 && (
        <ul className="mt-1 space-y-0.5 text-[10px] text-stone-500">
          {f.resolve_conditions.slice(0, 3).map((c, i) => <li key={i}>· {c}</li>)}
        </ul>
      )}
      {f.status !== 'resolved' && (
        <div className="mt-1.5 flex items-center gap-1.5" title={`叙事接近度 ${proximityWidth}%，指当前剧情走到多近该收线点。`}>
          <span className="text-[10px] text-stone-400 w-12 flex-shrink-0">接近度</span>
          <div className="flex-1 h-1 bg-stone-100 rounded-full overflow-hidden">
            <div className={`h-full rounded-full ${proximity > 0.9 ? 'bg-rose-500' : proximity > 0.7 ? 'bg-amber-500' : 'bg-blue-400'}`} style={{ width: `${proximityWidth}%` }} />
          </div>
          <span className="text-stone-400 w-9 text-right">{proximityWidth}%</span>
        </div>
      )}
      {f.resolved_chapter && (
        <p className="text-stone-400 mt-1">已于第 {f.resolved_chapter} 章收线</p>
      )}
    </div>
  )
}

function ForeshadowForm({ projectId, onCreated }: { projectId: string; onCreated: () => void }) {
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
      <select value={type} onChange={e => setType(e.target.value)} className="w-full px-2 py-1 text-xs border border-stone-200 rounded">
        <option value="plot">主线 plot</option>
        <option value="character">人物 character</option>
        <option value="worldbuilding">设定 worldbuilding</option>
        <option value="mystery">谜团 mystery</option>
      </select>
      <textarea value={desc} onChange={e => setDesc(e.target.value)} placeholder="描述伏笔..." className="w-full px-2 py-1 text-xs border border-stone-200 rounded resize-none h-16" />
      <textarea value={conditions} onChange={e => setConditions(e.target.value)} placeholder="收线条件 (一行一条)..." className="w-full px-2 py-1 text-xs border border-stone-200 rounded resize-none h-12" />
      <button onClick={handleSubmit} disabled={submitting || !desc.trim()} className="w-full px-2 py-1.5 text-xs bg-blue-600 text-white rounded hover:bg-blue-700 disabled:opacity-50">{submitting ? '创建中...' : '创建伏笔'}</button>
    </div>
  )
}
