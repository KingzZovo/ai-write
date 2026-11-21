'use client'

import React, { useState, useEffect, useRef, useCallback } from 'react'
import { apiFetch, ApiError } from '@/lib/api'
import { useT } from '@/lib/i18n/I18nProvider'
import { ForeshadowReviewList } from './ForeshadowReviewList'
import { ForeshadowPlanList } from './ForeshadowPlanList'
import { ForeshadowCreateForm, ForeshadowEditForm } from './ForeshadowAuthorForms'
import { ForeshadowSourceForm } from './ForeshadowSourceForm'
import {
  Foreshadow, clearForeshadowDraftIfCurrent, conditionsToList, draftKey, foreshadowAttempt, foreshadowHeaders,
} from '@/lib/foreshadowWrites'
export { buildForeshadowUpdatePayload } from '@/lib/foreshadowWrites'

interface ForeshadowResponse { foreshadows: Foreshadow[]; total: number }

interface ForeshadowPanelProps { projectId: string; chapterId?: string; editorContent?: string }

const STATUS_CFG: Record<string, { color: string; label: string; help: string }> = {
  planned: { color: 'bg-blue-100 text-blue-700', label: '计划中', help: '大纲安排，实际正文是否埋设仍待核查。' },
  pending:  { color: 'bg-stone-100 text-stone-600',     label: '待启动', help: '已抽取但尚未启动状态机。' },
  planted:  { color: 'bg-emerald-100 text-emerald-700', label: '已埋',     help: '已登记埋设，可结合正文核查是否已有回应。' },
  ripening: { color: 'bg-amber-100 text-amber-700',     label: '推进记录', help: '推进状态记录，实际进展需对照正文。' },
  ready:    { color: 'bg-rose-100 text-rose-700',       label: '待核对收线', help: '候选收线记录，时机与兑现情况由作者结合正文判断。' },
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

/** Parse the window.prompt answer for 标记回收. Returns null when cancelled or
 *  not a non-negative integer. Exported for tests. */
export function parseResolvedChapter(raw: string | null): number | null {
  if (raw === null) return null
  const trimmed = raw.trim()
  if (!/^\d+$/.test(trimmed)) return null
  const value = Number(trimmed)
  return Number.isSafeInteger(value) ? value : null
}

export function ForeshadowPanel(props: ForeshadowPanelProps) {
  return <ForeshadowPanelSession key={props.projectId} {...props} />
}

function ForeshadowPanelSession({ projectId, chapterId, editorContent }: ForeshadowPanelProps) {
  const t = useT()
  const [foreshadows, setForeshadows] = useState<Foreshadow[]>([])
  const [reviewVersion, setReviewVersion] = useState(0)
  const [loading, setLoading] = useState(false)
  const [showForm, setShowForm] = useState(false)
  const [filter, setFilter] = useState<'active' | 'all'>('active')
  const [typeFilter, setTypeFilter] = useState<string>('all')
  const [loadError, setLoadError] = useState('')
  const [editing, setEditing] = useState<Foreshadow | null>(null)
  const [associating, setAssociating] = useState<Foreshadow | null>(null)
  const requestVersion = useRef(0)
  const active = useRef(true)

  const fetchForeshadows = useCallback(async () => {
    if (!projectId || !active.current) return
    const request = ++requestVersion.current
    setLoading(true); setLoadError('')
    try {
      const data = await apiFetch<ForeshadowResponse | Foreshadow[]>(`/api/projects/${projectId}/foreshadows`)
      if (!active.current || request !== requestVersion.current) return
      const all = Array.isArray(data) ? data : data.foreshadows
      if (!Array.isArray(all)) throw new Error('invalid foreshadow response')
      setForeshadows(all)
      setReviewVersion(version => version + 1)
    } catch {
      if (active.current && request === requestVersion.current) setLoadError('伏笔列表读取失败，已显示的记录和草稿仍保留。')
    } finally {
      if (active.current && request === requestVersion.current) setLoading(false)
    }
  }, [projectId])

  useEffect(() => {
    active.current = true
    fetchForeshadows()
    return () => { active.current = false; requestVersion.current += 1 }
  }, [fetchForeshadows])

  const visible = foreshadows.filter(f => {
    if (filter === 'active' && f.status === 'resolved') return false
    if (typeFilter !== 'all' && f.type !== typeFilter) return false
    return true
  })

  const grouped = {
    planned: visible.filter(f => f.status === 'planned'),
    pending: visible.filter(f => f.status === 'pending'),
    ready:    visible.filter(f => f.status === 'ready'),
    ripening: visible.filter(f => f.status === 'ripening'),
    planted:  visible.filter(f => f.status === 'planted'),
    resolved: visible.filter(f => f.status === 'resolved'),
  }

  const typeOptions = Array.from(new Set(foreshadows.map(f => f.type).filter(Boolean)))
  const edit = (item: Foreshadow) => { setAssociating(null); setEditing(item) }
  const associate = (item: Foreshadow) => { setEditing(null); setAssociating(item) }

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

      <button onClick={fetchForeshadows} disabled={loading} className="text-xs text-blue-600 disabled:opacity-50">刷新伏笔列表</button>
      {loadError && <p role="alert" className="text-xs text-rose-600">{loadError}</p>}
      {showForm && <ForeshadowCreateForm projectId={projectId} onCreated={() => { setShowForm(false); fetchForeshadows() }} />}
      {editing && <ForeshadowEditForm key={editing.id} foreshadow={editing}
        current={foreshadows.find(item => item.id === editing.id)} projectId={projectId}
        onRefresh={fetchForeshadows} onDone={() => { setEditing(null); fetchForeshadows() }} onCancel={() => setEditing(null)} />}
      {associating && <ForeshadowSourceForm projectId={projectId} foreshadow={associating} defaultChapterId={chapterId}
        onDone={() => { setAssociating(null); fetchForeshadows() }} onClose={() => setAssociating(null)} />}

      {loading && foreshadows.length === 0 ? (
        <p className="text-xs text-gray-400">加载中...</p>
      ) : loadError && foreshadows.length === 0 ? null : visible.length === 0 ? (
        <div className="flex flex-col items-center gap-1 py-4 text-center">
          <span className="text-lg" aria-hidden>🌱</span>
          <p className="text-xs text-gray-400">暂无伏笔。核对线索候选后登记，或手动添加。</p>
        </div>
      ) : (
        <div className="space-y-3">
          {grouped.planned.length > 0 && <Section title="计划埋设（待正文落实）" items={grouped.planned} projectId={projectId} onChanged={fetchForeshadows} onEdit={edit} onSource={associate} />}
          {grouped.pending.length > 0 && <Section title="待启动" items={grouped.pending} projectId={projectId} onChanged={fetchForeshadows} onEdit={edit} onSource={associate} />}
          {grouped.ready.length    > 0 && <Section title={t('foreshadow.section.ready')}   items={grouped.ready} projectId={projectId} onChanged={fetchForeshadows} onEdit={edit} onSource={associate} />}
          {grouped.ripening.length > 0 && <Section title={t('foreshadow.section.ripening')} items={grouped.ripening} projectId={projectId} onChanged={fetchForeshadows} onEdit={edit} onSource={associate} />}
          {grouped.planted.length  > 0 && <Section title={t('foreshadow.section.planted')}     items={grouped.planted} projectId={projectId} onChanged={fetchForeshadows} onEdit={edit} onSource={associate} />}
          {filter === 'all' && grouped.resolved.length > 0 && <Section title={t('foreshadow.section.resolved')} items={grouped.resolved} projectId={projectId} onChanged={fetchForeshadows} onEdit={edit} onSource={associate} />}
        </div>
      )}
      {projectId && <ForeshadowPlanList projectId={projectId} refreshVersion={reviewVersion} />}
      {projectId && <ForeshadowReviewList chapterId={chapterId} editorContent={editorContent} key={projectId} projectId={projectId} refreshVersion={reviewVersion} onAccepted={fetchForeshadows} />}
    </div>
  )
}

function Section({ title, items, projectId, onChanged, onEdit, onSource }: {
  title: string; items: Foreshadow[]; projectId: string; onChanged: () => void; onEdit: (item: Foreshadow) => void
  onSource: (item: Foreshadow) => void
}) {
  return <div>
    <h4 className="mb-1 text-[10px] font-medium text-stone-500">{title} <span className="text-stone-300">({items.length})</span></h4>
    <div className="space-y-1.5">
      {items.map(item => <ForeshadowCard key={item.id} foreshadow={item} projectId={projectId} onChanged={onChanged} onEdit={onEdit} onSource={onSource} />)}
    </div>
  </div>
}

function ForeshadowCard({ foreshadow: f, projectId, onChanged, onEdit, onSource }: {
  foreshadow: Foreshadow; projectId: string; onChanged: () => void; onEdit: (item: Foreshadow) => void
  onSource: (item: Foreshadow) => void
}) {
  const t = useT()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const active = useRef(true)
  const inFlight = useRef(false)
  useEffect(() => { active.current = true; return () => { active.current = false } }, [])
  const statusCfg = STATUS_CFG[f.status] || STATUS_CFG.planted
  const typeCfg = TYPE_CFG[f.type] || { label: f.type, color: 'text-stone-500' }

  const perform = async (operation: 'resolve' | 'delete', chapter?: number) => {
    if (inFlight.current) return
    inFlight.current = true
    setBusy(true); setError('')
    const key = draftKey(projectId, `${operation}:${f.id}`)
    let attempt: ReturnType<typeof foreshadowAttempt> | undefined
    try {
      const payload = operation === 'resolve' ? { resolved_chapter: chapter } : undefined
      attempt = foreshadowAttempt(key, JSON.stringify(payload || {}), f.version)
      const result = await apiFetch<Foreshadow | undefined>(`/api/projects/${projectId}/foreshadows/${f.id}${operation === 'resolve' ? '/resolve' : ''}`, {
        method: operation === 'resolve' ? 'POST' : 'DELETE', headers: foreshadowHeaders(attempt),
        ...(payload ? { body: JSON.stringify(payload) } : {}),
      })
      if (operation === 'resolve' && (!result?.version || result.id !== f.id || result.status !== 'resolved' || result.resolved_chapter !== chapter)) {
        throw new Error('操作已处理，但当前伏笔已有其他修改，请刷新核对。')
      }
      clearForeshadowDraftIfCurrent(key, attempt)
      if (active.current) onChanged()
    } catch (error) {
      if (attempt && error instanceof ApiError && [404, 409, 422].includes(error.status)) {
        clearForeshadowDraftIfCurrent(key, attempt)
      }
      if (active.current) setError(error instanceof Error ? error.message : '操作尚未确认，请保留输入重试。')
    } finally {
      inFlight.current = false
      if (active.current) setBusy(false)
    }
  }
  const resolve = () => {
    const chapter = parseResolvedChapter(window.prompt(t('foreshadow.resolvePrompt')))
    if (chapter !== null) perform('resolve', chapter)
  }
  const remove = () => {
    if (window.confirm(t('foreshadow.deleteConfirm'))) perform('delete')
  }
  const conditions = conditionsToList(f.resolve_conditions_json)
  return <div className={`rounded-lg border border-stone-200 bg-white p-2.5 text-xs ${f.status === 'resolved' ? 'opacity-60' : ''}`}>
    <div className="mb-1 flex items-center gap-1.5">
      <span className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${statusCfg.color}`} title={statusCfg.help}>{statusCfg.label}</span>
      <span className={`text-[10px] ${typeCfg.color}`}>{typeCfg.label}</span>
      <span className="ml-auto text-stone-400">{f.source_state === 'current' ? f.source_label : `原记录章号 ${f.planted_chapter}（未核实）`}</span>
    </div>
    <p className="leading-relaxed text-stone-700">{f.description}</p>
    {f.source_state !== 'current' && <p className="mt-1 text-[10px] text-amber-700">
      {f.source_state === 'changed' ? '正文、位置或登记内容已变化' : f.source_state === 'unavailable' ? '来源章节或位置不可用' : '来源未核实'}，暂不进入自动写作上下文。
    </p>}
    {f.source_evidence && <blockquote className="mt-1 whitespace-pre-wrap border-l-2 border-stone-200 pl-2 text-stone-500">登记时原句：{f.source_evidence}</blockquote>}
    {conditions.length > 0 && <ul className="mt-1 space-y-0.5 text-[10px] text-stone-500">
      {conditions.slice(0, 3).map((condition, index) => <li key={index}>· {condition}</li>)}
    </ul>}
    {f.status === 'resolved' && f.resolved_chapter != null && <p className="mt-1 text-stone-400">登记收线章号：{f.resolved_chapter}（章节对应关系待核对）</p>}
    {error && <p role="alert" className="mt-1 text-rose-600">{error}</p>}
    <div className="mt-1.5 flex items-center gap-2 border-t border-stone-100 pt-1.5">
      <button onClick={() => onEdit(f)} disabled={busy} className="text-[10px] text-blue-500 disabled:opacity-50">{t('common.edit')}</button>
      <button onClick={() => onSource(f)} disabled={busy} className="text-[10px] text-blue-500 disabled:opacity-50">核对正文来源</button>
      {f.status !== 'resolved' && <button onClick={resolve} disabled={busy} className="text-[10px] text-emerald-600 disabled:opacity-50">{t('foreshadow.action.resolve')}</button>}
      <button onClick={remove} disabled={busy} className="ml-auto text-[10px] text-rose-400 disabled:opacity-50">{t('common.delete')}</button>
    </div>
  </div>
}
