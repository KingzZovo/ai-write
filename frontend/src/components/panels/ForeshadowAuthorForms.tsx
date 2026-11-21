'use client'

import { useEffect, useRef, useState } from 'react'
import { apiFetch, ApiError } from '@/lib/api'
import {
  Foreshadow, buildForeshadowUpdatePayload, clearForeshadowDraft, clearForeshadowDraftIfCurrent, conditionsToList,
  draftKey, foreshadowAttempt, foreshadowHeaders, matchesForeshadowEdit,
  readForeshadowDraft, storeForeshadowDraft,
} from '@/lib/foreshadowWrites'

type Draft = { description: string; type: string; conditionsText: string; baseline?: Foreshadow }

function readDraft(key: string, baseline?: Foreshadow): Draft {
  const stored = readForeshadowDraft(key) as Partial<Draft> | undefined
  if (stored && typeof stored.description === 'string' && typeof stored.type === 'string' &&
      typeof stored.conditionsText === 'string') {
    return { description: stored.description, type: stored.type, conditionsText: stored.conditionsText,
      baseline: stored.baseline || baseline }
  }
  return { description: baseline?.description || '', type: baseline?.type || 'plot',
    conditionsText: conditionsToList(baseline?.resolve_conditions_json).join('\n'), baseline }
}

function DraftFields({ draft, disabled, onChange }: {
  draft: Draft; disabled: boolean; onChange: (changes: Partial<Draft>) => void
}) {
  const types = ['plot', 'character', 'worldbuilding', 'mystery']
  return <>
    <label className="block text-xs text-stone-500">类型
      <select aria-label="伏笔类型" disabled={disabled} value={draft.type} onChange={event => onChange({ type: event.target.value })} className="mt-1 w-full rounded border border-stone-200 px-2 py-1 text-xs">
        {!types.includes(draft.type) && <option value={draft.type}>{draft.type}</option>}
        <option value="plot">主线</option><option value="character">人物</option>
        <option value="worldbuilding">设定</option><option value="mystery">谜团</option>
      </select>
    </label>
    <label className="block text-xs text-stone-500">伏笔描述
      <textarea aria-label="伏笔描述" readOnly={disabled} value={draft.description} onChange={event => onChange({ description: event.target.value })} placeholder="描述伏笔..." className="mt-1 h-20 w-full rounded border border-stone-200 px-2 py-1 text-xs" />
    </label>
    <label className="block text-xs text-stone-500">收线条件（一行一条）
      <textarea aria-label="收线条件" readOnly={disabled} value={draft.conditionsText} onChange={event => onChange({ conditionsText: event.target.value })} className="mt-1 h-16 w-full rounded border border-stone-200 px-2 py-1 text-xs" />
    </label>
  </>
}

export function ForeshadowEditForm({ foreshadow, current, projectId, onDone, onCancel, onRefresh }: {
  foreshadow: Foreshadow; current?: Foreshadow; projectId: string;
  onDone: () => void; onCancel: () => void; onRefresh: () => void;
}) {
  const key = draftKey(projectId, `edit:${foreshadow.id}`)
  const attemptKey = `${key}:attempt`
  const [draft, setDraft] = useState(() => readDraft(key, foreshadow))
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const active = useRef(true)
  const inFlight = useRef(false)
  useEffect(() => { active.current = true; return () => { active.current = false } }, [])
  const change = (patch: Partial<Draft>) => {
    const next = { ...draft, ...patch }
    storeForeshadowDraft(key, next)
    setDraft(next)
  }
  const changed = Boolean(current?.version && current.version !== draft.baseline?.version)
  const save = async () => {
    if (inFlight.current || !draft.description.trim()) return
    inFlight.current = true
    setSaving(true); setError('')
    const payload: { description: string; type: string; resolve_conditions?: string[] } = buildForeshadowUpdatePayload(draft)
    const originalConditions = draft.baseline?.resolve_conditions_json
    if ((!Array.isArray(originalConditions) || originalConditions.some(value => typeof value !== 'string')) &&
        draft.conditionsText === conditionsToList(originalConditions).join('\n')) {
      delete payload.resolve_conditions
    }
    storeForeshadowDraft(key, draft)
    try {
      const attempt = foreshadowAttempt(attemptKey, JSON.stringify({ payload, version: draft.baseline?.version }), draft.baseline?.version)
      const saved = await apiFetch<Foreshadow>(`/api/projects/${projectId}/foreshadows/${foreshadow.id}`, {
        method: 'PUT', headers: foreshadowHeaders(attempt), body: JSON.stringify(payload),
      })
      if (!saved?.version || saved.id !== foreshadow.id || !matchesForeshadowEdit(saved, payload)) {
        throw new Error('操作已处理，但当前记录与提交内容不同。草稿已保留，请刷新核对。')
      }
      clearForeshadowDraftIfCurrent(key, draft); clearForeshadowDraftIfCurrent(attemptKey, attempt)
      if (active.current) onDone()
    } catch (error) {
      if (active.current) setError(error instanceof Error ? error.message : '保存尚未确认，草稿已保留。')
    } finally {
      inFlight.current = false
      if (active.current) setSaving(false)
    }
  }
  return <div className="space-y-2 rounded-lg border border-blue-200 bg-blue-50/30 p-3">
    <p className="text-xs font-medium text-stone-700">编辑伏笔</p>
    <DraftFields draft={draft} disabled={saving} onChange={change} />
    {changed && <div className="space-y-1 rounded bg-white p-2 text-xs text-stone-600">
      <p>当前记录已有其他修改。你的草稿仍保留，请先核对：</p>
      <p>类型：{current?.type}</p>
      <p className="whitespace-pre-wrap">{current?.description}</p>
      <p className="whitespace-pre-wrap">{conditionsToList(current?.resolve_conditions_json).join('\n')}</p>
      <button disabled={saving} onClick={() => { change({ baseline: current }); clearForeshadowDraft(attemptKey); setError('') }} className="text-blue-600 disabled:opacity-50">已核对，保留我的草稿继续编辑</button>
    </div>}
    {error && <p role="alert" className="text-xs text-rose-600">{error}</p>}
    <div className="flex gap-2 text-xs">
      <button disabled={saving || changed || !draft.description.trim()} onClick={save} className="rounded bg-blue-600 px-3 py-1 text-white disabled:opacity-50">{saving ? '保存中...' : '保存'}</button>
      <button disabled={saving} onClick={onRefresh} className="text-blue-600 disabled:opacity-50">核对最新记录</button>
      <button disabled={saving} onClick={onCancel} className="ml-auto text-stone-500 disabled:opacity-50">收起（保留草稿）</button>
    </div>
  </div>
}

export function ForeshadowCreateForm({ projectId, onCreated }: { projectId: string; onCreated: () => void }) {
  const key = draftKey(projectId, 'create')
  const attemptKey = `${key}:attempt`
  const [draft, setDraft] = useState(() => readDraft(key))
  const [saving, setSaving] = useState(false)
  const [pending, setPending] = useState(() => Boolean(readForeshadowDraft(attemptKey)))
  const savedAttempt = useRef(readForeshadowDraft(attemptKey) as ReturnType<typeof foreshadowAttempt> | undefined)
  const [error, setError] = useState('')
  const active = useRef(true)
  const inFlight = useRef(false)
  useEffect(() => { active.current = true; return () => { active.current = false } }, [])
  const change = (patch: Partial<Draft>) => {
    const next = { ...draft, ...patch }
    storeForeshadowDraft(key, next); setDraft(next)
  }
  const create = async () => {
    if (inFlight.current || !draft.description.trim()) return
    inFlight.current = true
    setSaving(true); setError('')
    const payload = { ...buildForeshadowUpdatePayload(draft), planted_chapter: 0 }
    storeForeshadowDraft(key, draft)
    let attempt: ReturnType<typeof foreshadowAttempt> | undefined
    try {
      const signature = JSON.stringify(payload)
      attempt = savedAttempt.current?.signature === signature
        ? savedAttempt.current : foreshadowAttempt(attemptKey, signature)
      savedAttempt.current = attempt
      setPending(true)
      const saved = await apiFetch<Foreshadow>(`/api/projects/${projectId}/foreshadows`, {
        method: 'POST', headers: foreshadowHeaders(attempt, false), body: JSON.stringify(payload),
      })
      if (!saved?.version || saved.id !== attempt.id) throw new Error('尚未取得创建确认，请保留输入重试。')
      clearForeshadowDraftIfCurrent(key, draft); clearForeshadowDraftIfCurrent(attemptKey, attempt)
      if (active.current) onCreated()
    } catch (error) {
      // Definitive refusals allow a new intent. Uncertain outcomes keep the
      // original body and identity so a retry cannot create a duplicate.
      if (attempt && error instanceof ApiError && [401, 403, 404, 409, 422].includes(error.status)) {
        clearForeshadowDraftIfCurrent(attemptKey, attempt)
        savedAttempt.current = undefined
        if (active.current) setPending(false)
      }
      if (active.current) setError(error instanceof Error ? error.message : '创建尚未确认，输入已保留。')
    } finally {
      inFlight.current = false
      if (active.current) setSaving(false)
    }
  }
  return <div className="space-y-2 rounded-lg bg-stone-50 p-3">
    <DraftFields draft={draft} disabled={saving || pending} onChange={change} />
    {pending && <p className="text-xs text-stone-500">上次创建尚待确认，请先重试核对；不会重复添加同一请求。</p>}
    {error && <p role="alert" className="text-xs text-rose-600">{error}</p>}
    <button disabled={saving || !draft.description.trim()} onClick={create} className="w-full rounded bg-blue-600 px-2 py-1.5 text-xs text-white disabled:opacity-50">{saving ? '创建中...' : pending ? '重试确认创建' : '创建伏笔'}</button>
  </div>
}
