export interface Foreshadow {
  id: string
  project_id?: string
  type: string
  description: string
  planted_chapter: number
  resolve_conditions_json: unknown
  narrative_proximity: number | null
  status: string
  resolved_chapter: number | null
  version?: string
  source_state?: 'current' | 'changed' | 'unavailable' | 'unknown'
  source_kind?: 'prose' | 'record'
  source_label?: string
  source_evidence?: string | null
  source_origin?: 'author' | 'candidate' | null
  source_link_id?: string | null
  source_chapter_id?: string | null
  source_global_idx?: number | null
}

type Attempt = { id: string; signature: string; version?: string }
const memory = new Map<string, unknown>()

export function draftKey(projectId: string, item: string) {
  return `aiwrite:foreshadow:${encodeURIComponent(projectId)}:${encodeURIComponent(item)}`
}

export function readForeshadowDraft(key: string): unknown {
  // A failed storage write can leave an older value on disk. The current
  // page's latest draft/request must win, otherwise retries acquire new IDs.
  if (memory.has(key)) return memory.get(key)
  try {
    const raw = sessionStorage.getItem(key)
    if (raw !== null) return JSON.parse(raw)
  } catch { /* The current page can still preserve drafts when storage is unavailable. */ }
  return undefined
}

export function storeForeshadowDraft(key: string, value: unknown) {
  memory.set(key, value)
  try { sessionStorage.setItem(key, JSON.stringify(value)) } catch { /* Keep the in-memory draft. */ }
}

export function clearForeshadowDraft(key: string) {
  memory.delete(key)
  try { sessionStorage.removeItem(key) } catch { memory.set(key, undefined) }
}

export function clearForeshadowDraftIfCurrent(key: string, expected: unknown) {
  if (JSON.stringify(readForeshadowDraft(key)) === JSON.stringify(expected)) clearForeshadowDraft(key)
}

export function newForeshadowRequestId() {
  if (typeof crypto.randomUUID === 'function') return crypto.randomUUID()
  // getRandomValues also works on the existing HTTP installation.
  const bytes = crypto.getRandomValues(new Uint8Array(16))
  bytes[6] = (bytes[6] & 15) | 64
  bytes[8] = (bytes[8] & 63) | 128
  const hex = Array.from(bytes, value => value.toString(16).padStart(2, '0')).join('')
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}

export function foreshadowAttempt(key: string, signature: string, version?: string): Attempt {
  const stored = readForeshadowDraft(key) as Partial<Attempt> | undefined
  // Preserve the original baseline even if a list refresh arrives between retries.
  if (stored?.signature === signature && typeof stored.id === 'string' &&
      (version === undefined || /^[0-9a-f]{64}$/.test(stored.version || ''))) return stored as Attempt
  const attempt = { id: newForeshadowRequestId(), signature, version }
  storeForeshadowDraft(key, attempt)
  return attempt
}

export function foreshadowHeaders(attempt: Attempt, requireVersion = true) {
  if (requireVersion && !/^[0-9a-f]{64}$/.test(attempt.version || '')) {
    throw new Error('尚未取得这条伏笔的完整记录，请刷新后再修改。')
  }
  return {
    'Idempotency-Key': attempt.id,
    ...(attempt.version ? { 'If-Match': attempt.version } : {}),
  }
}

export function conditionsToList(value: unknown): string[] {
  const text = (item: unknown) => typeof item === 'string' ? item : JSON.stringify(item)
  if (Array.isArray(value)) return value.map(text)
  if (value && typeof value === 'object') return Object.entries(value).map(([key, item]) => `${key}：${text(item)}`)
  return []
}

export function buildForeshadowUpdatePayload(input: { description: string; type: string; conditionsText: string }) {
  return {
    description: input.description.trim(),
    type: input.type,
    resolve_conditions: input.conditionsText.split('\n').map(item => item.trim()).filter(Boolean),
  }
}

export function matchesForeshadowEdit(record: Foreshadow, payload: { description: string; type: string; resolve_conditions?: string[] }) {
  return record.description === payload.description && record.type === payload.type &&
    (payload.resolve_conditions === undefined || JSON.stringify(record.resolve_conditions_json) === JSON.stringify(payload.resolve_conditions))
}
