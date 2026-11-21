'use client'

import { useEffect, useRef, useState } from 'react'
import { apiFetch, ApiError } from '@/lib/api'
import {
  clearRelationshipDraftIfCurrent, readRelationshipDraft, relationshipAttempt, relationshipDraftKey,
  storeRelationshipDraft, subscribeRelationshipDraft,
  type RelationshipDraft, type RelationshipFields, type RelationshipSnapshot,
} from '@/lib/relationshipWrites'

type Props = {
  projectId: string; id: string; from: string; to: string; relType: string; mode: 'edit' | 'delete'
  onDone: () => void; onCancel: () => void
}

export function RelationshipEditor(props: Props) {
  // Reusing this component for another project/record cannot carry its draft,
  // callbacks or a late source response into the new editing session.
  return <RelationshipEditorSession key={`${props.projectId}:${props.id}`} {...props} />
}

function RelationshipEditorSession({ projectId, id, from, to, relType, mode, onDone, onCancel }: Props) {
  const key = relationshipDraftKey(projectId, id)
  const [draft, setDraft] = useState<RelationshipDraft | undefined>(() => readRelationshipDraft(key))
  const current = useRef(draft)
  const [snapshot, setSnapshot] = useState<RelationshipSnapshot | null>(null)
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)
  const [loading, setLoading] = useState(true)
  const [reload, setReload] = useState(0)
  const live = useRef(true)
  const completed = useRef(false)
  const done = useRef(onDone)
  done.current = onDone
  const base = `/api/projects/${projectId}/neo4j-settings/relationships`
  function publish(value: RelationshipDraft) {
    current.current = value
    storeRelationshipDraft(key, value)
    setDraft(value)
  }
  useEffect(() => {
    live.current = true
    const unsubscribe = subscribeRelationshipDraft(key, saved => {
      const value = readRelationshipDraft(key)
      current.current = value
      setDraft(value)
      if (saved) { completed.current = true; done.current() }
    })
    return () => { live.current = false; unsubscribe() }
  }, [key])
  useEffect(() => {
    if (completed.current) return
    const abort = new AbortController()
    let active = true
    const identity = current.current?.identity ?? { source: from, target: to, rel_type: relType }
    setLoading(true)
    setError('')
    setSnapshot(null)
    apiFetch<RelationshipSnapshot>(`${base}/record?${new URLSearchParams(identity)}`, { signal: abort.signal })
      .then(value => {
        if (!active || completed.current) return
        if (!/^[a-f0-9]{64}$/.test(value.version) || !Array.isArray(value.entries) || !Array.isArray(value.mixed_fields) ||
          !value.fields || ['label', 'note', 'sentiment'].some(field =>
            typeof value.fields[field as keyof RelationshipSnapshot['fields']] !== 'string')) throw new Error('关系来源读取不完整，请重试。')
        setSnapshot(value)
        if (!current.current) publish({ schema: 1, identity, version: value.version,
          fields: { rel_type: relType, ...value.fields }, changed: [] })
      })
      .catch(cause => { if (active && !completed.current) setError(cause instanceof Error ? cause.message : '关系来源读取失败。') })
      .finally(() => { if (active && !completed.current) setLoading(false) })
    return () => { active = false; abort.abort() }
    // A source refresh must never replace an existing draft or pending request.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [base, from, to, relType, reload])

  const pending = !!draft?.attempt
  const effectiveMode = draft?.attempt ? (draft.attempt.method === 'DELETE' ? 'delete' : 'edit') : mode
  const stale = !!draft && !!snapshot && draft.version !== snapshot.version
  const canSave = !!draft && (pending || (!!snapshot?.entries.length && !stale &&
    (mode === 'delete' || draft.changed.length > 0)))
  function change(field: keyof RelationshipFields, value: string) {
    const previous = readRelationshipDraft(key)
    if (!previous || previous.attempt || saving) return
    publish({ ...previous, fields: { ...previous.fields, [field]: value }, changed: [...new Set([...previous.changed, field])] })
  }
  async function save() {
    const previous = readRelationshipDraft(key)
    if (!previous || saving || !canSave) return
    const attempt = relationshipAttempt(previous, mode)
    const sent = { ...previous, attempt }
    publish(sent)
    setSaving(true)
    setError('')
    try {
      const result = await apiFetch<{ status: string; request_id: string }>(base, {
        method: attempt.method, headers: { 'Idempotency-Key': attempt.id, 'If-Match': attempt.version },
        body: JSON.stringify(attempt.body),
      })
      if (result.status !== 'saved' || result.request_id !== attempt.id) throw new Error('尚未确认保存结果，请重试原请求。')
      clearRelationshipDraftIfCurrent(key, sent)
    } catch (cause) {
      // These responses reject the write definitively. Unknown responses and
      // pending projections keep the original operation, version and ID.
      if (cause instanceof ApiError && (cause.status === 422 || (cause.status === 409 && cause.code !== 'sync_busy'))) {
        const stored = readRelationshipDraft(key)
        if (stored?.attempt?.id === attempt.id) {
          const remaining = { ...stored, attempt: undefined }
          storeRelationshipDraft(key, remaining)
          if (live.current) { current.current = remaining; setDraft(remaining) }
        }
      }
      if (live.current) setError(cause instanceof Error ? cause.message : '尚未确认操作结果，请重试原请求。')
    } finally { if (live.current) setSaving(false) }
  }
  return <div className="mt-2 space-y-2 rounded border border-stone-200 bg-stone-50 p-3 text-xs">
    <p>作者修改会作为关系设定保存，不代表正文已经写出这段关系。</p>
    {loading ? <p role="status">正在读取完整关系来源…</p> : snapshot && <>
      <p>本次涉及 {snapshot.entries.length} 条关系记录。</p>
      <details><summary className="cursor-pointer text-blue-600">查看当前来源记录</summary>
        <ul className="mt-2 space-y-2">{snapshot.entries.map(entry => <li key={entry.edge_id} className="whitespace-pre-wrap">
          {entry.properties.origin === 'author' ? '作者设定' : entry.properties.source_chapter_id ? `抽取记录（登记第 ${String(entry.properties.chapter_start)} 章，正文来源待核对）` : '历史登记（来源未核实）'}
          ：{String(entry.properties.raw_type || entry.properties.type)}
          {typeof entry.properties.label === 'string' && entry.properties.label ? `；标签：${entry.properties.label}` : ''}
          {typeof entry.properties.note === 'string' && entry.properties.note ? `；备注：${entry.properties.note}` : ''}
        </li>)}</ul>
      </details>
    </>}
    {effectiveMode === 'edit' && draft ? <fieldset disabled={loading || saving || pending} className="space-y-2 disabled:opacity-60">
      {(['rel_type', 'label', 'sentiment', 'note'] as const).map(field => <label key={field} className="block">
        <span className="mb-1 block">{{ rel_type: '关系类型', label: '标签', sentiment: '情感倾向', note: '备注' }[field]}</span>
        {snapshot?.mixed_fields.includes(field) && <span className="mb-1 block text-amber-700">原记录内容不一致；不修改此项将保留各自内容。</span>}
        {field === 'note' ? <textarea aria-label="关系备注" rows={3} value={draft.fields[field]} onChange={event => change(field, event.target.value)} className="w-full rounded border border-stone-300 p-1.5" /> :
          field === 'sentiment' ? <select aria-label="关系情感倾向" value={draft.fields[field]} onChange={event => change(field, event.target.value)} className="w-full rounded border border-stone-300 p-1.5">
            <option value="">未指定</option><option value="positive">正向</option><option value="neutral">中性</option><option value="negative">负向</option>
            {!['', 'positive', 'neutral', 'negative'].includes(draft.fields.sentiment) && <option value={draft.fields.sentiment}>{draft.fields.sentiment}</option>}
          </select> : <input aria-label={{ rel_type: '关系类型', label: '关系标签' }[field]} maxLength={field === 'rel_type' ? 100 : 200} value={draft.fields[field]} onChange={event => change(field, event.target.value)} className="w-full rounded border border-stone-300 p-1.5" />}
      </label>)}
    </fieldset> : effectiveMode === 'delete' ? <p className="text-rose-700">确认删除本次读取的关系记录？同一来源版本的旧抽取不会把它们恢复。</p> : null}
    {pending && <p role="status">原{effectiveMode === 'delete' ? '删除' : '修改'}请求尚待确认，输入已保留。请先重试确认原请求。</p>}
    {stale && !pending && <p className="text-amber-700">关系来源与草稿的原版本不同，请查看当前记录后重新核对。</p>}
    {error && <p role="alert" className="text-rose-700">{error}</p>}
    <div className="flex flex-wrap gap-3">
      <button type="button" disabled={saving || (!pending && loading) || !canSave} onClick={save} className="text-blue-700 disabled:opacity-40">{saving ? '正在确认…' : pending ? '重试确认' : mode === 'delete' ? '确认删除' : '保存关系'}</button>
      <button type="button" disabled={saving} onClick={() => setReload(value => value + 1)} className="text-stone-600 disabled:opacity-40">刷新来源</button>
      {stale && !pending && snapshot?.entries.length ? <button type="button" onClick={() => { if (current.current) publish({ ...current.current, version: snapshot.version }) }} className="text-amber-700">已核对当前记录，保留输入</button> : null}
      <button type="button" onClick={onCancel} className="text-stone-600">关闭并保留输入</button>
    </div>
  </div>
}
