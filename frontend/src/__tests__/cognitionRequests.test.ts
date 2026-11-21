import { afterEach, expect, it, vi } from 'vitest'
import {
  acknowledgeCognitionRequest, matchesCognitionAcknowledgment, pendingCognitionRequests, retainCognitionRequest, cognitionSubmission,
} from '@/lib/cognitionRequests'

afterEach(() => { vi.restoreAllMocks(); sessionStorage.clear() })
const chapterId = '00000000-0000-4000-8000-000000000001'
const intent = { operation: 'retry' as const, chapter_id: chapterId, source_hash: 'a'.repeat(64), version: 'b'.repeat(64) }
let number = 0
const project = () => `cognition-storage-${++number}`

it('retains the exact original source and request across a changed chapter version', () => {
  const pid = project()
  const first = retainCognitionRequest(pid, intent)
  expect(retainCognitionRequest(pid, { ...intent, source_hash: 'c'.repeat(64), version: 'd'.repeat(64) })).toEqual(first)
  acknowledgeCognitionRequest(pid, first)
  expect(retainCognitionRequest(pid, intent).id).not.toBe(first.id)
})

it('retains distinct chapters and the backfill separately and clears only an acknowledged id', () => {
  const pid = project()
  const first = retainCognitionRequest(pid, intent)
  const second = retainCognitionRequest(pid, { ...intent, chapter_id: '00000000-0000-4000-8000-000000000002' })
  const backfill = retainCognitionRequest(pid, { operation: 'backfill' })
  acknowledgeCognitionRequest(pid, first)
  const newer = retainCognitionRequest(pid, intent)
  acknowledgeCognitionRequest(pid, first)
  expect(pendingCognitionRequests(pid)).toEqual([second, backfill, newer])
})

it('reads a persisted unknown request without assigning another id', () => {
  const pid = project()
  const stored = { id: chapterId, intent }
  sessionStorage.setItem(`aiwrite:cognition:requests:${pid}`, JSON.stringify([stored]))
  expect(retainCognitionRequest(pid, intent)).toEqual(stored)
})

it('keeps the latest in-page request if session storage cannot write or delete', () => {
  const pid = project()
  const first = retainCognitionRequest(pid, intent)
  vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(() => { throw new Error('blocked') })
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('blocked') })
  acknowledgeCognitionRequest(pid, first)
  const second = retainCognitionRequest(pid, intent)
  expect(second.id).not.toBe(first.id)
  expect(pendingCognitionRequests(pid)).toEqual([second])
})

it('does not silently replace corrupt pending data with a new paid request', () => {
  const pid = project()
  sessionStorage.setItem(`aiwrite:cognition:requests:${pid}`, '{broken')
  expect(() => retainCognitionRequest(pid, intent)).toThrow()
})

it('requires a receipt bound to the original source, chapter and request', () => {
  const attempt = retainCognitionRequest(project(), intent)
  const accepted = { request_id: attempt.id, operation: 'retry', outcome: 'scheduled', chapter_id: chapterId,
    source_hash: intent.source_hash, record_id: chapterId, cycle_id: chapterId }
  expect(matchesCognitionAcknowledgment(attempt, accepted)).toBe(true)
  for (const changed of [{ request_id: 'different' }, { chapter_id: 'different' }, { source_hash: 'different' },
    { record_id: null }, { cycle_id: null }, { outcome: 'completed' }, { operation: 'backfill' }]) {
    expect(matchesCognitionAcknowledgment(attempt, { ...accepted, ...changed })).toBe(false)
  }
})

it('accepts a backfill receipt only with its identity and finite chapter count', () => {
  const attempt = retainCognitionRequest(project(), { operation: 'backfill' })
  const accepted = { request_id: attempt.id, operation: 'backfill', outcome: 'scheduled', backfill_id: chapterId, chapter_count: 0 }
  expect(matchesCognitionAcknowledgment(attempt, accepted)).toBe(true)
  expect(matchesCognitionAcknowledgment(attempt, { ...accepted, chapter_count: -1 })).toBe(false)
  expect(matchesCognitionAcknowledgment(attempt, { ...accepted, chapter_count: 1.5 })).toBe(false)
  expect(matchesCognitionAcknowledgment(attempt, { ...accepted, backfill_id: null })).toBe(false)
})

it('freezes an edit payload and retains its original operation during unknown acknowledgment', () => {
  const pid = project()
  const changes = [{ character: '沈青', believes: '以为信已寄出', evidence: '沈青以为信已寄出。' }]
  const attempt = retainCognitionRequest(pid, { ...intent, operation: 'edit', changes })
  changes[0].believes = '后来输入'
  const repeat = retainCognitionRequest(pid, { ...intent, operation: 'restore_auto' })
  expect(repeat).toEqual(attempt)
  expect(cognitionSubmission('/base', repeat)).toEqual({ path: `/base/chapters/${chapterId}/edit`, payload: {
    request_id: attempt.id, source_hash: intent.source_hash, version: intent.version,
    changes: [{ character: '沈青', believes: '以为信已寄出', evidence: '沈青以为信已寄出。' }],
  } })
})

it('requires the author revision and mode in edit and restore acknowledgments', () => {
  for (const operation of ['edit', 'restore_auto'] as const) {
    const attempt = retainCognitionRequest(project(), operation === 'edit'
      ? { ...intent, operation, changes: [] } : { ...intent, operation })
    const accepted = { request_id: attempt.id, operation, outcome: operation === 'edit' ? 'saved' : 'scheduled',
      chapter_id: chapterId, record_id: chapterId, cycle_id: chapterId, source_hash: intent.source_hash,
      revision_id: chapterId, source_mode: operation === 'edit' ? 'author' : 'auto' }
    expect(matchesCognitionAcknowledgment(attempt, accepted)).toBe(true)
    for (const changed of [{ revision_id: null }, { source_mode: null }, { outcome: 'already_active' }, { source_hash: 'wrong' }]) {
      expect(matchesCognitionAcknowledgment(attempt, { ...accepted, ...changed })).toBe(false)
    }
  }
})
