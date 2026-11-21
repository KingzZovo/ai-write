import { afterEach, expect, it, vi } from 'vitest'
import {
  acknowledgeCognitionDraft, changesFromRows, readCognitionDraft, validateCognitionDraft, writeCognitionDraft,
} from '@/lib/cognitionDrafts'
import type { CognitionDraft } from '@/lib/cognitionDrafts'
import { retainCognitionRequest } from '@/lib/cognitionRequests'

afterEach(() => { vi.restoreAllMocks(); sessionStorage.clear() })
const cid = '00000000-0000-4000-8000-000000000001'
let number = 0
const project = () => `cognition-draft-${++number}`
const draft = (): CognitionDraft => ({ source_hash: 'a'.repeat(64), version: 'b'.repeat(64), rows: [
  { id: 0, character: '沈青', kind: 'suspects', fact: '怀疑回信由陈川代写', evidence: '沈青怀疑回信由陈川代写。' },
] })

it('retains exact draft text and original version after a later input or old acknowledgment', () => {
  const pid = project(), original = draft()
  writeCognitionDraft(pid, cid, original)
  const attempt = retainCognitionRequest(pid, { operation: 'edit', chapter_id: cid,
    source_hash: original.source_hash, version: original.version, changes: changesFromRows(original.rows) })
  const newer = { ...original, rows: [{ ...original.rows[0], fact: '后来输入的文字' }] }
  writeCognitionDraft(pid, cid, newer)
  acknowledgeCognitionDraft(pid, attempt)
  expect(readCognitionDraft(pid, cid)).toEqual(newer)
  expect(readCognitionDraft(project(), cid)).toBeNull()
})

it('clears only the exact acknowledged edit, including a deliberately empty correction', () => {
  for (const rows of [draft().rows, []]) {
    const pid = project(), original = { ...draft(), rows }
    writeCognitionDraft(pid, cid, original)
    const attempt = retainCognitionRequest(pid, { operation: 'edit', chapter_id: cid,
      source_hash: original.source_hash, version: original.version, changes: changesFromRows(rows) })
    acknowledgeCognitionDraft(pid, attempt)
    expect(readCognitionDraft(pid, cid)).toBeNull()
  }
})

it('does not discard drafts when restoring automatic extraction', () => {
  const pid = project(), original = draft()
  writeCognitionDraft(pid, cid, original)
  acknowledgeCognitionDraft(pid, retainCognitionRequest(pid, { operation: 'restore_auto', chapter_id: cid,
    source_hash: original.source_hash, version: original.version }))
  expect(readCognitionDraft(pid, cid)).toEqual(original)
})

it('reads persisted blank in-progress fields and rejects corrupted storage', () => {
  const pid = project(), incomplete = { ...draft(), rows: [{ ...draft().rows[0], fact: '' }] }
  sessionStorage.setItem(`aiwrite:cognition:draft:v1:${pid}:${cid}`, JSON.stringify(incomplete))
  expect(readCognitionDraft(pid, cid)).toEqual(incomplete)
  const broken = project()
  sessionStorage.setItem(`aiwrite:cognition:draft:v1:${broken}:${cid}`, JSON.stringify({ ...draft(), rows: [{ kind: 'invented' }] }))
  expect(() => readCognitionDraft(broken, cid)).toThrow()
})

it('retains in-page drafts and reports session storage failure', () => {
  const pid = project(), original = draft()
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('quota') })
  expect(writeCognitionDraft(pid, cid, original)).toBe(false)
  expect(readCognitionDraft(pid, cid)).toEqual(original)
})

it('requires verbatim evidence and never upgrades a reader suspicion to knowledge', () => {
  const original = draft()
  expect(validateCognitionDraft(original, original.rows[0].evidence)).toBeNull()
  expect(validateCognitionDraft(original, '另一篇稿件。')).toContain('逐字定位')
  expect(validateCognitionDraft({ ...original, rows: [{ ...original.rows[0], character: '__reader__' }] },
    original.rows[0].evidence)).toContain('读者')
  expect(validateCognitionDraft({ ...original, rows: [] }, '原文')).toBeNull()
})
