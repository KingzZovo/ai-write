import { describe, it, expect } from 'vitest'
import { styleReferenceBookId } from '@/lib/styleReference'

describe('styleReferenceBookId', () => {
  it('returns the bound book id for book-level profiles', () => {
    expect(styleReferenceBookId({ bind_level: 'book', bind_target_id: 'book-1' })).toBe('book-1')
  })

  it('returns null for non-book bind levels', () => {
    expect(styleReferenceBookId({ bind_level: 'global', bind_target_id: 'book-1' })).toBeNull()
    expect(styleReferenceBookId({ bind_level: 'chapter', bind_target_id: 'ch-1' })).toBeNull()
  })

  it('returns null when the profile or bind fields are missing', () => {
    expect(styleReferenceBookId(null)).toBeNull()
    expect(styleReferenceBookId(undefined)).toBeNull()
    expect(styleReferenceBookId({})).toBeNull()
    expect(styleReferenceBookId({ bind_level: 'book' })).toBeNull()
    expect(styleReferenceBookId({ bind_level: 'book', bind_target_id: null })).toBeNull()
    expect(styleReferenceBookId({ bind_level: 'book', bind_target_id: '' })).toBeNull()
  })
})
