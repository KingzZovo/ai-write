/** Unsaved edits keep their original source and version until explicit review. */
import { cognitionFact, cognitionKind, cognitionKinds, validCognitionChange } from '@/lib/cognitionTypes'
import type { CognitionChange, CognitionDetail, CognitionKind } from '@/lib/cognitionTypes'
import type { CognitionAttempt } from '@/lib/cognitionRequests'

export type CognitionDraftRow = { id: number; character: string; kind: CognitionKind; fact: string; evidence: string }
export type CognitionDraft = { source_hash: string; version: string; rows: CognitionDraftRow[] }
const memory = new Map<string, CognitionDraft | null>()
const key = (projectId: string, chapterId: string) =>
  `aiwrite:cognition:draft:v1:${encodeURIComponent(projectId)}:${encodeURIComponent(chapterId)}`
const hash = /^[0-9a-f]{64}$/

export function rowsFromChanges(changes: CognitionChange[]): CognitionDraftRow[] {
  return changes.map((item, id) => ({ id, character: item.character, kind: cognitionKind(item),
    fact: cognitionFact(item), evidence: item.evidence }))
}

export function changesFromRows(rows: CognitionDraftRow[]): CognitionChange[] {
  return rows.map(({ character, kind, fact, evidence }) => ({ character, [kind]: fact, evidence } as CognitionChange))
}

export function newCognitionDraft(detail: CognitionDetail): CognitionDraft {
  if (!detail.source_hash || !detail.version) throw new Error('unavailable cognition source')
  return { source_hash: detail.source_hash, version: detail.version, rows: rowsFromChanges(detail.changes || []) }
}

function validDraft(value: unknown): value is CognitionDraft {
  if (!value || typeof value !== 'object') return false
  const draft = value as CognitionDraft
  return hash.test(draft.source_hash) && hash.test(draft.version) && Array.isArray(draft.rows) &&
    draft.rows.every(row => row && Number.isSafeInteger(row.id) && row.id >= 0 && cognitionKinds.includes(row.kind) &&
      [row.character, row.fact, row.evidence].every(item => typeof item === 'string')) &&
    new Set(draft.rows.map(row => row.id)).size === draft.rows.length
}

export function readCognitionDraft(projectId: string, chapterId: string): CognitionDraft | null {
  const storageKey = key(projectId, chapterId)
  if (memory.has(storageKey)) return memory.get(storageKey) || null
  let raw: string | null
  try { raw = sessionStorage.getItem(storageKey) } catch { return null }
  if (!raw) return null
  const value: unknown = JSON.parse(raw)
  if (!validDraft(value)) throw new Error('invalid cognition draft')
  memory.set(storageKey, value)
  return value
}

export function writeCognitionDraft(projectId: string, chapterId: string, draft: CognitionDraft | null): boolean {
  const storageKey = key(projectId, chapterId)
  memory.set(storageKey, draft)
  try {
    if (draft) sessionStorage.setItem(storageKey, JSON.stringify(draft))
    else sessionStorage.removeItem(storageKey)
    return true
  } catch { return false }
}

export function validateCognitionDraft(draft: CognitionDraft, source: string): string | null {
  for (const [index, change] of changesFromRows(draft.rows).entries()) {
    if (!validCognitionChange(change)) return `第 ${index + 1} 条需填写人物、认知内容和原句；读者只记录正文明确提供的信息。`
    if (!source.includes(change.evidence)) return `第 ${index + 1} 条依据无法在当前保存稿逐字定位，请重新选取。`
  }
  return null
}

export function acknowledgeCognitionDraft(projectId: string, attempt: CognitionAttempt) {
  const intent = attempt.intent
  if (intent.operation !== 'edit') return
  const current = readCognitionDraft(projectId, intent.chapter_id)
  if (current && current.source_hash === intent.source_hash && current.version === intent.version &&
      JSON.stringify(changesFromRows(current.rows)) === JSON.stringify(changesFromRows(rowsFromChanges(intent.changes)))) {
    writeCognitionDraft(projectId, intent.chapter_id, null)
  }
}
