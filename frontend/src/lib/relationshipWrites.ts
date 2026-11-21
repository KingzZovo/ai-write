export type RelationshipFields = { rel_type: string; label: string; note: string; sentiment: string }
export type RelationshipIdentity = { source: string; target: string; rel_type: string }
export interface RelationshipSnapshot {
  version: string
  entries: { edge_id: string; properties: Record<string, unknown> }[]
  fields: Omit<RelationshipFields, 'rel_type'>
  mixed_fields: string[]
}
export interface RelationshipAttempt {
  id: string
  method: 'PUT' | 'DELETE'
  version: string
  body: Record<string, string>
}
export interface RelationshipDraft {
  schema: 1
  identity: RelationshipIdentity
  version: string
  fields: RelationshipFields
  changed: string[]
  attempt?: RelationshipAttempt
}

const memory = new Map<string, RelationshipDraft | undefined>()
const listeners = new Map<string, Set<(saved: boolean) => void>>()
export function subscribeRelationshipDraft(key: string, listener: (saved: boolean) => void) {
  const group = listeners.get(key) ?? new Set()
  group.add(listener)
  listeners.set(key, group)
  return () => {
    group.delete(listener)
    if (!group.size) listeners.delete(key)
  }
}
export function relationshipDraftKey(projectId: string, id: string) {
  return `aiwrite:relationship:v1:${encodeURIComponent(projectId)}:${encodeURIComponent(id)}`
}
export function readRelationshipDraft(key: string): RelationshipDraft | undefined {
  if (memory.has(key)) return memory.get(key)
  try {
    const raw = sessionStorage.getItem(key)
    const value = raw ? JSON.parse(raw) : undefined
    if (value?.schema === 1 && value.identity && value.fields && Array.isArray(value.changed) &&
      typeof value.version === 'string' && /^[a-f0-9]{64}$/.test(value.version) &&
      ['source', 'target', 'rel_type'].every(field => typeof value.identity[field] === 'string') &&
      ['rel_type', 'label', 'note', 'sentiment'].every(field => typeof value.fields[field] === 'string') &&
      (!value.attempt || (typeof value.attempt.id === 'string' &&
        ['PUT', 'DELETE'].includes(value.attempt.method) && /^[a-f0-9]{64}$/.test(value.attempt.version) &&
        value.attempt.body && Object.values(value.attempt.body).every(item => typeof item === 'string')))) {
      memory.set(key, value)
      return value
    }
  } catch { /* The current page can still retain drafts when storage is unavailable. */ }
  memory.set(key, undefined)
  return undefined
}
export function storeRelationshipDraft(key: string, draft: RelationshipDraft) {
  memory.set(key, draft)
  try { sessionStorage.setItem(key, JSON.stringify(draft)) } catch { /* Retain the current in-memory draft. */ }
  listeners.get(key)?.forEach(listener => listener(false))
}
export function clearRelationshipDraftIfCurrent(key: string, expected: RelationshipDraft) {
  if (JSON.stringify(readRelationshipDraft(key)) !== JSON.stringify(expected)) return
  memory.set(key, undefined)
  try { sessionStorage.removeItem(key) } catch { /* The in-memory value hides a stale disk copy. */ }
  listeners.get(key)?.forEach(listener => listener(true))
}
export function relationshipRequestId() {
  if (typeof crypto.randomUUID === 'function') return crypto.randomUUID()
  const bytes = crypto.getRandomValues(new Uint8Array(16))
  bytes[6] = (bytes[6] & 15) | 64
  bytes[8] = (bytes[8] & 63) | 128
  const hex = Array.from(bytes, value => value.toString(16).padStart(2, '0')).join('')
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}
export function relationshipAttempt(draft: RelationshipDraft, mode: 'edit' | 'delete'): RelationshipAttempt {
  if (draft.attempt) return draft.attempt
  const body: Record<string, string> = { ...draft.identity }
  if (mode === 'edit') {
    if (draft.changed.includes('rel_type')) body.new_rel_type = draft.fields.rel_type.trim()
    for (const key of ['label', 'note', 'sentiment'] as const) {
      if (draft.changed.includes(key)) body[key] = draft.fields[key]
    }
  }
  return { id: relationshipRequestId(), method: mode === 'delete' ? 'DELETE' : 'PUT', version: draft.version, body }
}
