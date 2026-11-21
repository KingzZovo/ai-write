import { describe, it, expect, beforeEach } from 'vitest'
import {
  buildForeshadowUpdatePayload,
  parseResolvedChapter,
} from '@/components/panels/ForeshadowPanel'
import {
  buildRelationshipUpdatePayload,
  getImportanceOverrides,
  setImportanceOverride,
} from '@/components/panels/CharacterCardPanel'

describe('buildForeshadowUpdatePayload', () => {
  it('splits conditions per line, trims and drops blanks', () => {
    expect(
      buildForeshadowUpdatePayload({
        description: ' 神秘玉佩 ',
        type: 'mystery',
        conditionsText: ' 主角发现玉佩来历 \n\n  \n对上暗号',
      }),
    ).toEqual({
      description: '神秘玉佩',
      type: 'mystery',
      resolve_conditions: ['主角发现玉佩来历', '对上暗号'],
    })
  })

  it('produces an empty conditions list for empty text', () => {
    expect(
      buildForeshadowUpdatePayload({ description: 'x', type: 'plot', conditionsText: '' }),
    ).toEqual({ description: 'x', type: 'plot', resolve_conditions: [] })
  })
})

describe('parseResolvedChapter', () => {
  it('accepts non-negative integers', () => {
    expect(parseResolvedChapter('12')).toBe(12)
    expect(parseResolvedChapter(' 0 ')).toBe(0)
  })

  it('rejects cancel, non-numeric, negative and fractional input', () => {
    expect(parseResolvedChapter(null)).toBeNull()
    expect(parseResolvedChapter('')).toBeNull()
    expect(parseResolvedChapter('abc')).toBeNull()
    expect(parseResolvedChapter('-1')).toBeNull()
    expect(parseResolvedChapter('2.5')).toBeNull()
  })
})

describe('buildRelationshipUpdatePayload', () => {
  it('matches by names and includes editable fields', () => {
    expect(
      buildRelationshipUpdatePayload({
        source: '林远',
        target: '苏晚',
        relType: 'friend',
        newRelType: 'friend',
        label: '青梅竹马',
        sentiment: 'positive',
        note: '第 3 章相识',
      }),
    ).toEqual({
      source: '林远',
      target: '苏晚',
      rel_type: 'friend',
      label: '青梅竹马',
      sentiment: 'positive',
      note: '第 3 章相识',
    })
  })

  it('sends new_rel_type only when the type actually changes', () => {
    const changed = buildRelationshipUpdatePayload({
      source: 'a', target: 'b', relType: 'friend', newRelType: 'rival',
    })
    expect(changed.new_rel_type).toBe('rival')
    expect(changed.rel_type).toBe('friend')

    const unchanged = buildRelationshipUpdatePayload({
      source: 'a', target: 'b', relType: 'friend', newRelType: ' friend ',
    })
    expect(unchanged).not.toHaveProperty('new_rel_type')

    const blank = buildRelationshipUpdatePayload({
      source: 'a', target: 'b', relType: 'friend', newRelType: '  ',
    })
    expect(blank).not.toHaveProperty('new_rel_type')
  })

  it('omits optional fields that were not provided', () => {
    expect(
      buildRelationshipUpdatePayload({ source: 'a', target: 'b', relType: 'ally' }),
    ).toEqual({ source: 'a', target: 'b', rel_type: 'ally' })
  })
})

describe('importance overrides (localStorage)', () => {
  const pid = 'proj-1'

  beforeEach(() => {
    localStorage.clear()
  })

  it('round-trips overrides per project and character name', () => {
    setImportanceOverride(pid, '林远', 'protagonist')
    setImportanceOverride(pid, '路人甲', 'minor')
    setImportanceOverride('other-proj', '林远', 'supporting')
    expect(getImportanceOverrides(pid)).toEqual({ 林远: 'protagonist', 路人甲: 'minor' })
    expect(getImportanceOverrides('other-proj')).toEqual({ 林远: 'supporting' })
  })

  it('clears an override with null and removes the key when empty', () => {
    setImportanceOverride(pid, '林远', 'key')
    const next = setImportanceOverride(pid, '林远', null)
    expect(next).toEqual({})
    expect(localStorage.getItem('char_importance_override:' + pid)).toBeNull()
  })

  it('drops corrupted or unknown stored values', () => {
    localStorage.setItem('char_importance_override:' + pid, 'not json')
    expect(getImportanceOverrides(pid)).toEqual({})
    localStorage.setItem(
      'char_importance_override:' + pid,
      JSON.stringify({ 林远: 'protagonist', 苏晚: 'emperor', bad: 42 }),
    )
    expect(getImportanceOverrides(pid)).toEqual({ 林远: 'protagonist' })
  })
})
