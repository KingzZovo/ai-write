import { describe, it, expect } from 'vitest'
import { routeGenerationEvent } from '@/lib/generationEvents'

// Payload shapes mirror backend app/api/generate.py SSE emissions.
describe('routeGenerationEvent', () => {
  it('maps the new scene event to a 写作中 phase with idx/total/title', () => {
    expect(
      routeGenerationEvent({ event: 'scene', scene_idx: 2, total: 5, title: '雨夜追踪' }),
    ).toEqual({ progress: '写作中 · 场景 2/5：雨夜追踪' })
  })

  it('renders the scene index when total is null (current backend emitter)', () => {
    expect(
      routeGenerationEvent({ event: 'scene', scene_idx: 3, total: null, title: '对峙' }),
    ).toEqual({ progress: '写作中 · 场景 3：对峙' })
  })

  it('tolerates a scene event without idx/total', () => {
    expect(routeGenerationEvent({ event: 'scene', title: '开场' })).toEqual({
      progress: '写作中：开场',
    })
    expect(routeGenerationEvent({ event: 'scene', scene_idx: 0, total: null, title: '' })).toEqual(
      { progress: '写作中' },
    )
  })

  it('maps quality_check by passed flag', () => {
    expect(
      routeGenerationEvent({ event: 'quality_check', status: 'passed', passed: true })?.progress,
    ).toBe('输出检查通过，内容质量待评审')
    expect(
      routeGenerationEvent({ event: 'quality_check', status: 'needs_rewrite', passed: false })
        ?.progress,
    ).toBe('输出检查发现问题，正在处理')
  })

  it('maps evaluating / scored with round and score', () => {
    expect(routeGenerationEvent({ event: 'evaluating', round: 1 })?.progress).toBe(
      '评分中（第 1 轮）',
    )
    expect(
      routeGenerationEvent({ event: 'scored', round: 1, overall: 7.85, issues: 3 })?.progress,
    ).toBe(`评分 ${(7.85).toFixed(1)}（第 1 轮）`)
    expect(
      routeGenerationEvent({ event: 'scored', round: 2, overall: 8, issues: 0 })?.progress,
    ).toBe('评分 8.0（第 2 轮）')
  })

  it('maps revising with targeted vs full mode', () => {
    expect(
      routeGenerationEvent({ event: 'revising', round: 2, overall: 7.1, mode: 'targeted' })
        ?.progress,
    ).toBe('修订第 2 轮（定点）')
    expect(
      routeGenerationEvent({ event: 'revising', round: 1, overall: 7.1, mode: 'full' })?.progress,
    ).toBe('修订第 1 轮（整章）')
  })

  it('maps revise_skipped by reason', () => {
    expect(
      routeGenerationEvent({
        event: 'revise_skipped',
        reason: 'score_above_threshold',
        overall: 8.5,
        threshold: 8.2,
      })?.progress,
    ).toBe('评分达标（8.5），无需修订')
    expect(
      routeGenerationEvent({ event: 'revise_skipped', reason: 'evaluation_parse_failed' })
        ?.progress,
    ).toBe('评分解析失败，跳过修订')
  })

  it('flags truncated as a saved-as-draft warning', () => {
    const r = routeGenerationEvent({
      event: 'truncated',
      status: 'draft',
      reason: 'midsentence_ending',
      chapter_id: 'c1',
      word_count: 1200,
    })
    expect(r?.savedAsDraft).toBe(true)
    expect(r?.warning).toContain('草稿')
    expect(r?.error).toBeUndefined()
  })

  it('flags refusal as a saved-as-draft warning', () => {
    const r = routeGenerationEvent({ event: 'refusal', status: 'draft', reason: 'image_model_refusal' })
    expect(r?.savedAsDraft).toBe(true)
    expect(r?.warning).toContain('草稿')
  })

  it('routes save_failed (status key, no event key) to an error', () => {
    const r = routeGenerationEvent({ status: 'save_failed', kind: 'chapter', reason: 'no_target_row' })
    expect(r?.error).toContain('no_target_row')
    expect(r?.savedAsDraft).toBeUndefined()
    // Variant with error/error_class instead of reason:
    const r2 = routeGenerationEvent({ status: 'save_failed', kind: 'chapter', error: 'boom' })
    expect(r2?.error).toContain('boom')
  })

  it('marks a revise-loop saved event with truncated:true as draft', () => {
    const r = routeGenerationEvent({ status: 'saved', chapter_id: 'c1', truncated: true })
    expect(r?.savedAsDraft).toBe(true)
    expect(r?.warning).toBeTruthy()
  })

  it('maps a plain saved event to progress only', () => {
    expect(routeGenerationEvent({ status: 'saved', chapter_id: 'c1', word_count: 3000 })).toEqual({
      progress: '已保存',
    })
  })

  it('returns null for unrelated events', () => {
    expect(routeGenerationEvent({ event: 'generation_blocked' })).toBeNull()
    expect(routeGenerationEvent({ event: 'quality_failed' })).toBeNull()
    expect(routeGenerationEvent({ event: 'revise_restart', revise_round: 1 })).toBeNull()
    expect(routeGenerationEvent({})).toBeNull()
  })
})


it('distinguishes stopped revision from chapter acceptance', () => {
  expect(routeGenerationEvent({event: 'revise_skipped', reason: 'review_required'})?.progress).not.toContain('评分达标')
  expect(routeGenerationEvent({event: 'chapter_acceptance', status: 'needs_review'})?.savedAsDraft).toBe(true)
  expect(routeGenerationEvent({event: 'chapter_acceptance', status: 'completed'})?.progress).toBe('章节验收通过')
})

it.each([
  ['call_failed', '评审模型调用失败'],
  ['incomplete_response', '评审响应未完整结束'],
  ['timeout', '评审超时'],
  ['empty_input', '正文为空，无法评审'],
  ['parse_failed', '评分解析失败'],
])('does not present an invalid score as literary judgement: %s', (reason, label) => {
  expect(routeGenerationEvent({ event: 'scored', overall: 0, evaluation_failure: reason })?.progress)
    .toBe(`${label}，未获得有效评分`)
  expect(routeGenerationEvent({ event: 'revise_skipped', overall: 0, reason: `evaluation_${reason}` })?.progress)
    .toBe(`${label}，跳过修订`)
})

it('does not label unapplied or unknown revision stops as passing scores', () => {
  const stopped = routeGenerationEvent({event: 'revise_skipped', reason: 'targeted_revision_unapplied'})
  expect(stopped?.progress).toContain('局部修订未完成')
  expect(stopped?.warning).toContain('请审阅')
  expect(routeGenerationEvent({event: 'revise_skipped', reason: 'unknown'})?.progress).not.toContain('评分达标')
})

it('distinguishes an interrupted generation from confirmed partial persistence', () => {
  const interrupted = routeGenerationEvent({ event: 'generation_interrupted' })
  expect(interrupted?.interrupted).toBe(true)
  expect(interrupted?.savedAsDraft).toBeUndefined()
  expect(interrupted?.progress).toContain('等待草稿保存确认')
  const saved = routeGenerationEvent({ event: 'partial_saved', status: 'draft' })
  expect(saved?.savedAsDraft).toBe(true)
  expect(saved?.warning).toContain('本章尚未完成')
})

it('describes fallback as starting before prose arrives', () => {
  expect(routeGenerationEvent({ event: 'fallback_restart' })?.progress)
    .toBe('尚未收到正文，改用整章生成...')
})

it.each(['timeout', 'empty_briefs', 'scene_orchestrator_failed'])('warns on revision interruption without claiming a save: %s', reason => {
  const r = routeGenerationEvent({ event: 'revise_error', reason, error: 'private provider details' })
  expect(r?.warning).toContain('本轮修订未完成')
  expect(r?.savedAsDraft).toBeUndefined()
  expect(r?.interrupted).toBeUndefined()
  expect(JSON.stringify(r)).not.toContain('private')
})
