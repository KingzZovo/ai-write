import { clearForeshadowDraft, newForeshadowRequestId, readForeshadowDraft, storeForeshadowDraft } from './foreshadowWrites'

type Attempt = { id: string; signature: string }
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

function validAttempt(value: unknown): value is Attempt {
  if (!value || typeof value !== 'object') return false
  const row = value as Partial<Attempt>
  return typeof row.id === 'string' && UUID.test(row.id) && typeof row.signature === 'string'
}

function pendingAttempts(key: string): Attempt[] {
  const value = readForeshadowDraft(key)
  // Read the earlier single-attempt shape without changing its identity.
  if (validAttempt(value)) return [value]
  const entries = value && typeof value === 'object' ? (value as { attempts?: unknown }).attempts : undefined
  return Array.isArray(entries) ? entries.filter(validAttempt) : []
}

export function reviewAttempt(key: string, signature: string): Attempt {
  const attempts = pendingAttempts(key)
  const existing = attempts.find(attempt => attempt.signature === signature)
  if (existing) return existing
  const created = { id: newForeshadowRequestId(), signature }
  // A later edit must not replace the unknown outcome of an earlier draft.
  storeForeshadowDraft(key, { attempts: [...attempts, created] })
  return created
}

export function acknowledgeReviewAttempt(key: string, attempt: Attempt) {
  const before = pendingAttempts(key)
  const after = before.filter(item => item.id !== attempt.id || item.signature !== attempt.signature)
  if (after.length === before.length) return
  if (after.length) storeForeshadowDraft(key, { attempts: after })
  else clearForeshadowDraft(key)
}
