'use client'

import { useEffect, useRef, useState } from 'react'
import { apiFetch, ApiError } from '@/lib/api'

export interface EditableCharacter {
  id: string
  name: string
  profile_json: Record<string, unknown> | null
}

const labels = { identity: '身份', personality: '性格', appearance: '外貌', abilities: '能力', biography: '小传', current_status: '当前状态' }
function normalized(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(normalized).join(',')}]`
  if (value && typeof value === 'object') return `{${Object.entries(value).sort(([a], [b]) => a.localeCompare(b))
    .map(([key, item]) => `${JSON.stringify(key)}:${normalized(item)}`).join(',')}}`
  return JSON.stringify(value)
}
function object(text: string): Record<string, unknown> {
  const value = JSON.parse(text)
  if (!value || Array.isArray(value) || typeof value !== 'object') throw new Error('人物档案必须是 JSON 对象。')
  return value
}

type Props = {
  projectId: string; character: EditableCharacter; json?: boolean; onSaved: (character: EditableCharacter) => void
}
export function CharacterProfileEditor(props: Props) {
  return <CharacterProfileEditorSession key={JSON.stringify([props.projectId, props.character.name])} {...props} />
}
function CharacterProfileEditorSession({ projectId, character, json = false, onSaved }: Props) {
  const storageKey = `aiwrite:character-draft:${JSON.stringify([projectId, character.name])}`
  const baseline = useRef(character.profile_json ?? {})
  const [restored] = useState(() => {
    try {
      const item = JSON.parse(sessionStorage.getItem(storageKey) || 'null')
      if (item && typeof item.text === 'string') {
        const submitted = typeof item.submitted === 'string' ? item.submitted : null
        let recovered = false
        try { recovered = !!submitted && normalized(object(submitted)) === normalized(baseline.current) } catch { /* Invalid old receipt. */ }
        return { text: item.text, submitted, conflict: !recovered && normalized(item.base) !== normalized(baseline.current) }
      }
    } catch { /* Storage is optional; editing still works without it. */ }
    return { text: JSON.stringify(baseline.current, null, 2), submitted: null as string | null, conflict: false }
  })
  const [draft, setDraft] = useState(restored.text)
  const latest = useRef(draft)
  const [conflict, setConflict] = useState(restored.conflict)
  const [currentProfile, setCurrentProfile] = useState<Record<string, unknown> | null>(null)
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState('')
  const [error, setError] = useState('')
  const [verification, setVerification] = useState(restored.submitted)
  const submitted = useRef(restored.submitted)
  const onSavedRef = useRef(onSaved)
  onSavedRef.current = onSaved
  const active = useRef(true)
  const request = useRef<AbortController | null>(null)
  useEffect(() => {
    active.current = true
    return () => { active.current = false; request.current?.abort() }
  }, [])
  function remember(text: string) {
    try { sessionStorage.setItem(storageKey, JSON.stringify({ base: baseline.current, text, submitted: submitted.current })) } catch { /* Optional storage. */ }
  }
  useEffect(() => {
    if (!verification) return
    let stopped = false
    let timer: ReturnType<typeof setTimeout> | undefined
    const controller = new AbortController()
    const poll = async () => {
      let again = false
      try {
        const target = object(verification)
        const graph = await apiFetch<{ profile_json: Record<string, unknown>; pending: boolean }>(
          `/api/projects/${projectId}/neo4j-settings/characters/profile?name=${encodeURIComponent(character.name)}`, { signal: controller.signal })
        if (stopped) return
        if (!graph.profile_json || typeof graph.profile_json !== 'object' || Array.isArray(graph.profile_json) || typeof graph.pending !== 'boolean') throw new Error('恢复状态响应不完整。')
        if (normalized(graph.profile_json) !== normalized(target)) {
          setCurrentProfile(graph.profile_json)
          submitted.current = null; setVerification(null); setConflict(true); remember(latest.current)
          setError('当前人物档案已有其他修改，旧草稿已保留，请核对后再保存。'); setNotice('')
          return
        }
        if (graph.pending) { setNotice('已提交，后台正在恢复同步。当前草稿会保留。'); setError(''); again = true; return }
        setNotice('正在核对页面档案，当前草稿会保留。')
        const data = await apiFetch<EditableCharacter[] | { characters: EditableCharacter[] }>(`/api/projects/${projectId}/characters`, { signal: controller.signal })
        if (stopped) return
        const rows = Array.isArray(data) ? data : data.characters
        const saved = Array.isArray(rows) ? rows.find(item => item.name === character.name) : undefined
        if (!saved || normalized(saved.profile_json ?? {}) !== normalized(target)) {
          setVerification(null)
          setError('后台状态已结束，但页面档案尚未核对一致。草稿已保留，请刷新核对。')
          setNotice('')
          return
        }
        baseline.current = target; submitted.current = null; setVerification(null); setConflict(false); setError('')
        if (latest.current === verification) {
          try { sessionStorage.removeItem(storageKey) } catch { /* Optional storage. */ }
          setNotice('后台同步已完成并核对。')
        } else { remember(latest.current); setNotice('后台同步已完成，当前仍有未保存的修改。') }
        onSavedRef.current(saved)
      } catch (e) {
        if (stopped) return
        setError(e instanceof Error ? e.message : '恢复状态读取失败，草稿已保留。')
        again = !(e instanceof ApiError && e.status >= 400 && e.status < 500)
        if (!again) { setVerification(null); setNotice('') }
      } finally {
        if (again && !stopped) timer = setTimeout(() => { void poll() }, 5000)
      }
    }
    void poll()
    return () => { stopped = true; controller.abort(); if (timer) clearTimeout(timer) }
  // All mutable editor state is read from refs; only a new submitted snapshot restarts verification.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [verification, projectId, character.name])
  function change(text: string) {
    latest.current = text
    setDraft(text)
    setNotice('有未保存的修改。')
    remember(text)
  }
  async function save() {
    if (request.current || conflict || verification) return
    let profile: Record<string, unknown>
    try { profile = object(latest.current) } catch (e) { setError(e instanceof Error ? e.message : '档案格式无效。'); return }
    const snapshot = latest.current
    const controller = new AbortController()
    request.current = controller
    setBusy(true); setError(''); setNotice('正在保存并核对…')
    try {
      const result = await apiFetch<{ status: string; entity: string; name: string }>(`/api/projects/${projectId}/neo4j-settings/characters`, {
        method: 'POST', signal: controller.signal, body: JSON.stringify({ name: character.name, profile_json: profile, expected_profile_json: baseline.current }),
      })
      if (!active.current || controller.signal.aborted) return
      if (result.status !== 'accepted' || result.entity !== 'character' || result.name !== character.name) throw new Error('保存响应不完整，草稿已保留。')
      const data = await apiFetch<EditableCharacter[] | { characters: EditableCharacter[] }>(`/api/projects/${projectId}/characters`, { signal: controller.signal })
      const rows = Array.isArray(data) ? data : data.characters
      if (!Array.isArray(rows)) throw new Error('人物列表响应不完整，草稿已保留。')
      const saved = rows.find(item => item.name === character.name)
      if (!saved || normalized(saved.profile_json ?? {}) !== normalized(profile)) throw new Error('提交结果尚未同步确认，草稿已保留，请核对后重试。')
      if (!active.current) return
      baseline.current = profile
      submitted.current = null
      if (latest.current === snapshot) {
        try { sessionStorage.removeItem(storageKey) } catch { /* Optional storage. */ }
        setNotice('已保存并核对。')
      } else {
        remember(latest.current)
        setNotice('上一版已保存，当前仍有未保存的修改。')
      }
      onSaved(saved)
    } catch (e) {
      if (active.current) {
        if (e instanceof ApiError && e.code === 'character_projection_pending') {
          submitted.current = snapshot; remember(latest.current); setVerification(snapshot)
          setNotice('已提交，后台正在恢复同步。当前草稿会保留。'); setError('')
        } else { setError(e instanceof Error ? e.message : '保存未获确认，草稿已保留。'); setNotice('') }
      }
    } finally {
      request.current = null
      if (active.current) setBusy(false)
    }
  }
  let parsed: Record<string, unknown> | null = null
  try { parsed = object(draft) } catch { /* JSON mode keeps invalid draft text. */ }
  const showJson = json || !parsed || conflict
  return <div className="mt-2 space-y-2">
    {conflict && <p role="alert" className="text-xs text-amber-700">恢复的草稿与当前档案版本不同，暂不能保存。请先复制草稿，再载入当前档案核对。</p>}
    {showJson ? <label className="block text-xs text-stone-500">人物档案 JSON
      <textarea aria-label="人物档案 JSON" value={draft} onChange={e => change(e.target.value)} className="mt-1 h-64 w-full rounded border p-2 font-mono text-xs" />
    </label> : Object.entries(labels).map(([field, label]) => <label key={field} className="block text-xs text-stone-500">{label}
      <textarea aria-label={label} value={typeof parsed![field] === 'string' ? parsed![field] as string : parsed![field] == null ? '' : JSON.stringify(parsed![field])}
        onChange={e => change(JSON.stringify({ ...object(latest.current), [field]: e.target.value }, null, 2))}
        className="mt-1 w-full rounded border border-stone-200 px-2 py-1 text-xs" rows={field === 'biography' ? 3 : 1} />
    </label>)}
    {error && <p role="alert" className="text-xs text-rose-600">{error}</p>}
    {notice && <p role="status" className="text-xs text-stone-600">{notice}</p>}
    <button type="button" disabled={busy || conflict || !!verification} onClick={() => { void save() }} className="rounded bg-blue-600 px-3 py-1.5 text-xs text-white disabled:opacity-50">{busy ? '保存中…' : verification ? '等待同步确认…' : '保存人物'}</button>
    {conflict && <button type="button" onClick={() => {
      if (currentProfile) baseline.current = currentProfile
      submitted.current = null; setVerification(null); setConflict(false); setError('')
      change(JSON.stringify(baseline.current, null, 2))
    }} className="ml-2 text-xs text-blue-600">载入当前档案</button>}
    <p className="text-xs text-stone-400">修改后请点击保存。未保存草稿会尽可能保留在当前浏览器会话中。</p>
  </div>
}
