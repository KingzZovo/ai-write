/** Preserve each unacknowledged author intent, including its saved-source version. */
import { validCognitionChange } from '@/lib/cognitionTypes'
import type { CognitionChange } from '@/lib/cognitionTypes'

export type CognitionIntent =
  | { operation: 'retry'; chapter_id: string; source_hash: string; version: string }
  | { operation: 'edit'; chapter_id: string; source_hash: string; version: string; changes: CognitionChange[] }
  | { operation: 'restore_auto'; chapter_id: string; source_hash: string; version: string }
  | { operation: 'backfill' }
export type CognitionAttempt = { id: string; intent: CognitionIntent }

const memory = new Map<string, CognitionAttempt[]>()
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
const hash = /^[0-9a-f]{64}$/
const key = (projectId: string) => `aiwrite:cognition:requests:${encodeURIComponent(projectId)}`

export function cognitionSlot(intent: CognitionIntent) {
  return intent.operation === 'backfill' ? 'backfill' : intent.chapter_id
}

function validAttempt(value: unknown): value is CognitionAttempt {
  if (!value || typeof value !== 'object') return false
  const attempt = value as Partial<CognitionAttempt>
  const intent = attempt.intent
  return typeof attempt.id === 'string' && uuid.test(attempt.id) && !!intent &&
    (intent.operation === 'backfill' || ['retry', 'edit', 'restore_auto'].includes(intent.operation) &&
      uuid.test(intent.chapter_id) && hash.test(intent.source_hash) && hash.test(intent.version) &&
      (intent.operation !== 'edit' || Array.isArray(intent.changes) && intent.changes.every(validCognitionChange)))
}

export function pendingCognitionRequests(projectId: string): CognitionAttempt[] {
  const storageKey = key(projectId)
  const cached = memory.get(storageKey)
  if (cached) return cached
  let raw: string | null = null
  try { raw = sessionStorage.getItem(storageKey) } catch { return [] }
  if (!raw) return []
  const value: unknown = JSON.parse(raw)
  if (!Array.isArray(value) || !value.every(validAttempt)) throw new Error('invalid pending cognition requests')
  memory.set(storageKey, value)
  return value
}

function persist(projectId: string, attempts: CognitionAttempt[]) {
  const storageKey = key(projectId)
  memory.set(storageKey, attempts)
  try {
    if (attempts.length) sessionStorage.setItem(storageKey, JSON.stringify(attempts))
    else sessionStorage.removeItem(storageKey)
  } catch { /* The latest in-page request still overrides stale disk state. */ }
}

function requestId() {
  if (typeof crypto.randomUUID === 'function') return crypto.randomUUID()
  const bytes = crypto.getRandomValues(new Uint8Array(16))
  bytes[6] = (bytes[6] & 15) | 64
  bytes[8] = (bytes[8] & 63) | 128
  const hex = Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('')
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}

export function retainCognitionRequest(projectId: string, intent: CognitionIntent): CognitionAttempt {
  const attempts = pendingCognitionRequests(projectId)
  const existing = attempts.find(item => cognitionSlot(item.intent) === cognitionSlot(intent))
  if (existing) return existing
  const attempt = { id: requestId(), intent: JSON.parse(JSON.stringify(intent)) as CognitionIntent }
  persist(projectId, [...attempts, attempt])
  return attempt
}

export function acknowledgeCognitionRequest(projectId: string, attempt: CognitionAttempt) {
  persist(projectId, pendingCognitionRequests(projectId).filter(item => item.id !== attempt.id))
}

export function matchesCognitionAcknowledgment(attempt: CognitionAttempt, value: unknown): boolean {
  if (!value || typeof value !== 'object') return false
  const result = value as Record<string, unknown>
  const intent = attempt.intent
  if (result.request_id !== attempt.id || result.operation !== intent.operation) return false
  if (intent.operation === 'backfill') {
    return result.outcome === 'scheduled' && typeof result.backfill_id === 'string' && uuid.test(result.backfill_id) &&
      typeof result.chapter_count === 'number' && Number.isInteger(result.chapter_count) && result.chapter_count >= 0
  }
  if (result.chapter_id !== intent.chapter_id || result.source_hash !== intent.source_hash ||
      typeof result.record_id !== 'string' || !uuid.test(result.record_id) ||
      typeof result.cycle_id !== 'string' || !uuid.test(result.cycle_id)) return false
  if (intent.operation === 'retry') return ['scheduled', 'already_active', 'already_complete'].includes(String(result.outcome))
  return typeof result.revision_id === 'string' && uuid.test(result.revision_id) && (intent.operation === 'edit'
    ? result.outcome === 'saved' && result.source_mode === 'author'
    : ['scheduled', 'already_complete'].includes(String(result.outcome)) && result.source_mode === 'auto')
}

export function cognitionSubmission(base: string, attempt: CognitionAttempt) {
  const intent = attempt.intent
  if (intent.operation === 'backfill') return { path: `${base}/backfills`, payload: { request_id: attempt.id } }
  const suffix = intent.operation === 'restore_auto' ? 'restore-auto' : intent.operation
  return { path: `${base}/chapters/${intent.chapter_id}/${suffix}`, payload: {
    request_id: attempt.id, source_hash: intent.source_hash, version: intent.version,
    ...(intent.operation === 'edit' ? { changes: intent.changes } : {}),
  } }
}
