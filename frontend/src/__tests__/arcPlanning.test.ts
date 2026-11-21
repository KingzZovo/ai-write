import { describe, expect, it } from 'vitest'
import fixture from './fixtures/arcPlanning.contract.json'
import { parseArcPlanDetail, parseArcPlanInput, parseArcPlanPage, parseArcPlanSource, parseArcPlanStatus, sameArcPlanInput, type ArcPlanDetail } from '@/lib/arcPlanning'

const pid = fixture.after.project_id, rid = fixture.input.request_id
describe('actual persisted arc planning contract', () => {
  it('accepts the exact offline ASGI request and complete creation receipt', () => {
    const input = parseArcPlanInput(fixture.input, pid)
    expect(parseArcPlanStatus(fixture.pending, pid, rid).status).toBe('pending')
    const detail = parseArcPlanDetail(fixture.after, pid, rid)
    expect(sameArcPlanInput(detail, input)).toBe(true)
    expect(detail.accepted_outline?.chapter_summaries).toHaveLength(4)
  })

  it.each(['request', 'project', 'count', 'duplicate', 'receipt', 'outline', 'invalid_resume'])(
    'rejects an incomplete or differently scoped %s', change => {
      const value = structuredClone(fixture.after) as ArcPlanDetail
      if (change === 'request') value.request.request_id = '11111111-1111-1111-1111-111111111111'
      if (change === 'project') value.project_id = '11111111-1111-1111-1111-111111111111'
      if (change === 'count') value.result!.chapter_count = 3
      if (change === 'duplicate') value.result!.chapter_ids[1] = value.result!.chapter_ids[0]
      if (change === 'receipt') value.result!.request_hash = 'f'.repeat(64)
      if (change === 'outline') value.accepted_outline!.chapter_summaries.pop()
      if (change === 'invalid_resume') { value.integrity_valid = false; value.can_resume = true }
      expect(() => parseArcPlanDetail(value, pid, rid)).toThrow()
    })

  it('does not confirm a different original background or chapter count', () => {
    const input = parseArcPlanInput(fixture.input, pid), detail = parseArcPlanDetail(fixture.after, pid, rid)
    expect(sameArcPlanInput(detail, { ...input, background: '作者的另一版背景' })).toBe(false)
    expect(sameArcPlanInput(detail, { ...input, target_chapters: 5 })).toBe(false)
  })

  it('rejects history with duplicate requests or a foreign cursor', () => {
    expect(() => parseArcPlanPage({ items: [fixture.pending, fixture.pending], next_cursor: null }, pid)).toThrow()
    expect(() => parseArcPlanPage({ items: [fixture.pending], next_cursor: 'unknown' }, pid)).toThrow()
  })

  it('rejects malformed book material while retaining legacy responses without that material', () => {
    const source = { source: fixture.input.expected_source, mode: 'start', target_volume_idx: 6, previous_material: null }
    expect(parseArcPlanSource(source, pid).book_material_text).toBeUndefined()
    expect(() => parseArcPlanSource({ ...source, book_material_text: {} }, pid)).toThrow()
    expect(() => parseArcPlanDetail({ ...fixture.after, book_material_text: [] }, pid, rid)).toThrow()
  })

  it.each([[], {}, 1, false])('rejects malformed cognition originals: %s', cognition => {
    const source = { source: fixture.input.expected_source, mode: 'start', target_volume_idx: 6, previous_material: null }
    expect(() => parseArcPlanSource({ ...source, cognition_material_text: cognition }, pid)).toThrow()
    expect(() => parseArcPlanDetail({ ...fixture.after, cognition_material_text: cognition }, pid, rid)).toThrow()
  })

  it('retains complete cognition originals and accepts older records without them', () => {
    const original = '原句：林禾当时怀疑船主另有打算。'.repeat(400)
    const detail = parseArcPlanDetail({ ...fixture.after, cognition_material_text: original }, pid, rid)
    expect(detail.cognition_material_text).toBe(original)
    expect(parseArcPlanDetail(fixture.after, pid, rid).cognition_material_text).toBeUndefined()
  })
})
