import { describe, it, expect } from 'vitest'
import {
  getDossierStatus,
  isDossierRunning,
  hasDossierContent,
  sectionError,
  formatSourceCounts,
} from '@/lib/dossier'

describe('getDossierStatus', () => {
  it('reads dossier_status from metadata_json', () => {
    expect(getDossierStatus({ dossier_status: 'running' })).toBe('running')
    expect(getDossierStatus({ dossier_status: 'done', other: 1 })).toBe('done')
  })

  it('returns null for absent / malformed metadata', () => {
    expect(getDossierStatus(null)).toBeNull()
    expect(getDossierStatus(undefined)).toBeNull()
    expect(getDossierStatus('running')).toBeNull()
    expect(getDossierStatus({})).toBeNull()
    expect(getDossierStatus({ dossier_status: 42 })).toBeNull()
    expect(getDossierStatus({ dossier_status: '' })).toBeNull()
  })
})

describe('isDossierRunning', () => {
  it('treats in-flight statuses as running', () => {
    for (const s of ['pending', 'queued', 'running', 'consolidating']) {
      expect(isDossierRunning(s)).toBe(true)
    }
  })

  it('treats terminal / unknown statuses as not running', () => {
    expect(isDossierRunning('done')).toBe(false)
    expect(isDossierRunning('error')).toBe(false)
    expect(isDossierRunning('failed')).toBe(false)
    expect(isDossierRunning(null)).toBe(false)
    expect(isDossierRunning(undefined)).toBe(false)
    expect(isDossierRunning('')).toBe(false)
  })
})

describe('hasDossierContent', () => {
  it('true when any block or the timestamp exists', () => {
    expect(hasDossierContent({ style_block: '文风…' })).toBe(true)
    expect(hasDossierContent({ structure_block: '架构…' })).toBe(true)
    expect(hasDossierContent({ world_block: '世界观…' })).toBe(true)
    expect(hasDossierContent({ consolidated_at: '2026-07-26T00:00:00Z' })).toBe(true)
  })

  it('false for empty / absent dossiers', () => {
    expect(hasDossierContent(null)).toBe(false)
    expect(hasDossierContent(undefined)).toBe(false)
    expect(hasDossierContent({})).toBe(false)
    expect(hasDossierContent({ style_block: '', structure_block: null })).toBe(false)
  })
})

describe('sectionError', () => {
  it('extracts string errors from {error: ...} data', () => {
    expect(sectionError({ error: 'LLM timeout' })).toBe('LLM timeout')
  })

  it('stringifies non-string errors', () => {
    expect(sectionError({ error: { code: 500 } })).toBe('{"code":500}')
  })

  it('returns null when there is no error', () => {
    expect(sectionError(null)).toBeNull()
    expect(sectionError(undefined)).toBeNull()
    expect(sectionError('text')).toBeNull()
    expect(sectionError(['error'])).toBeNull()
    expect(sectionError({ summary: 'ok' })).toBeNull()
    expect(sectionError({ error: null })).toBeNull()
  })
})

describe('formatSourceCounts', () => {
  it('formats numeric entries', () => {
    expect(formatSourceCounts({ style: 12, plot: 8, world: 5 })).toBe('style 12 · plot 8 · world 5')
  })

  it('skips non-numeric entries and handles malformed input', () => {
    expect(formatSourceCounts({ style: 3, note: 'x' })).toBe('style 3')
    expect(formatSourceCounts(null)).toBe('')
    expect(formatSourceCounts(undefined)).toBe('')
    expect(formatSourceCounts([1, 2])).toBe('')
    expect(formatSourceCounts('style: 3')).toBe('')
  })
})
