import { describe, it, expect } from 'vitest'
import {
  getDossierStatus,
  getDossierStatusInfo,
  parseDossierStatus,
  parseDossierResponse,
  isDossierRunning,
  isDossierError,
  hasDossierContent,
  formatElapsed,
  sectionError,
  formatSourceCounts,
  sourceBooksCount,
} from '@/lib/dossier'

describe('parseDossierStatus', () => {
  it('normalizes the object marker the backend actually writes', () => {
    expect(parseDossierStatus({ state: 'done', updated_at: '2026-07-26T10:00:00+00:00', llm_calls: 16 }))
      .toEqual({ state: 'done', updatedAt: '2026-07-26T10:00:00+00:00', message: null })
    expect(parseDossierStatus({ state: 'running', updated_at: '2026-07-26T10:00:00+00:00' }))
      .toEqual({ state: 'running', updatedAt: '2026-07-26T10:00:00+00:00', message: null })
  })

  it('carries the error message on error markers', () => {
    expect(parseDossierStatus({ state: 'error', updated_at: null, error: 'LLM timeout' }))
      .toEqual({ state: 'error', updatedAt: null, message: 'LLM timeout' })
    expect(parseDossierStatus({ state: 'error', error: { code: 500 } })?.message).toBe('{"code":500}')
  })

  it('accepts legacy plain-string markers and a `status` key', () => {
    expect(parseDossierStatus('running')).toEqual({ state: 'running', updatedAt: null, message: null })
    expect(parseDossierStatus({ status: 'queued' })?.state).toBe('queued')
  })

  it('returns null for absent / malformed markers', () => {
    expect(parseDossierStatus(null)).toBeNull()
    expect(parseDossierStatus(undefined)).toBeNull()
    expect(parseDossierStatus('')).toBeNull()
    expect(parseDossierStatus(42)).toBeNull()
    expect(parseDossierStatus([])).toBeNull()
    expect(parseDossierStatus({})).toBeNull()
    expect(parseDossierStatus({ state: 7 })).toBeNull()
  })
})

describe('getDossierStatus', () => {
  it('reads the object marker from metadata_json', () => {
    expect(getDossierStatus({ dossier_status: { state: 'done', updated_at: 'x', llm_calls: 16 } })).toBe('done')
    expect(getDossierStatus({ dossier_status: { state: 'running' } })).toBe('running')
  })

  it('still reads legacy string markers', () => {
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

  it('getDossierStatusInfo exposes the full marker', () => {
    expect(getDossierStatusInfo({ dossier_status: { state: 'running', updated_at: 't1' } }))
      .toEqual({ state: 'running', updatedAt: 't1', message: null })
    expect(getDossierStatusInfo({})).toBeNull()
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

describe('isDossierError', () => {
  it('flags terminal failure states only', () => {
    expect(isDossierError('error')).toBe(true)
    expect(isDossierError('failed')).toBe(true)
    expect(isDossierError('done')).toBe(false)
    expect(isDossierError('running')).toBe(false)
    expect(isDossierError(null)).toBe(false)
  })
})

describe('parseDossierResponse', () => {
  it('parses the {book_id, status, dossier} envelope', () => {
    const parsed = parseDossierResponse({
      book_id: 'b1',
      status: { state: 'done', updated_at: 't', llm_calls: 16 },
      dossier: { style_block: '文风…', consolidated_at: 't' },
    })
    expect(parsed.status?.state).toBe('done')
    expect(parsed.dossier?.style_block).toBe('文风…')
  })

  it('yields a null dossier while consolidation is running', () => {
    const parsed = parseDossierResponse({
      book_id: 'b1',
      status: { state: 'running', updated_at: 't' },
      dossier: null,
    })
    expect(parsed.status?.state).toBe('running')
    expect(parsed.dossier).toBeNull()
  })

  it('accepts a bare legacy dossier payload', () => {
    const parsed = parseDossierResponse({ style_block: '文风…' })
    expect(parsed.status).toBeNull()
    expect(parsed.dossier?.style_block).toBe('文风…')
  })

  it('rejects empty dossiers and malformed payloads', () => {
    expect(parseDossierResponse({ book_id: 'b1', status: null, dossier: {} }).dossier).toBeNull()
    expect(parseDossierResponse(null)).toEqual({ status: null, dossier: null })
    expect(parseDossierResponse('x')).toEqual({ status: null, dossier: null })
    expect(parseDossierResponse([])).toEqual({ status: null, dossier: null })
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

describe('formatElapsed', () => {
  const now = Date.parse('2026-07-26T10:01:05Z')

  it('formats seconds and minutes since the timestamp', () => {
    expect(formatElapsed('2026-07-26T10:00:20Z', now)).toBe('45s')
    expect(formatElapsed('2026-07-26T09:59:00Z', now)).toBe('2m 5s')
    expect(formatElapsed('2026-07-26T09:59:05Z', now)).toBe('2m')
  })

  it('clamps future timestamps to 0s', () => {
    expect(formatElapsed('2026-07-26T10:05:00Z', now)).toBe('0s')
  })

  it('returns empty for missing / unparsable input', () => {
    expect(formatElapsed(null, now)).toBe('')
    expect(formatElapsed(undefined, now)).toBe('')
    expect(formatElapsed('not-a-date', now)).toBe('')
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

describe('sourceBooksCount', () => {
  it('reads the books count from an author dossier', () => {
    expect(sourceBooksCount({ source_counts: { books: 3, style_cards: 9 } })).toBe(3)
  })

  it('returns null when absent or malformed', () => {
    expect(sourceBooksCount(null)).toBeNull()
    expect(sourceBooksCount({})).toBeNull()
    expect(sourceBooksCount({ source_counts: { books: 'x' } })).toBeNull()
    expect(sourceBooksCount({ source_counts: null })).toBeNull()
  })
})
