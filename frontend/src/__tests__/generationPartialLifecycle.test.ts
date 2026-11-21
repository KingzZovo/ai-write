import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { createChapterGeneration } from '@/components/editor/chapterGeneration'
import { ChapterDraftSession } from '@/components/editor/chapterDraftSession'
import { ManuscriptSource } from '@/lib/chapterManuscripts'

const detached: (() => void)[] = []
beforeEach(() => vi.useFakeTimers())
afterEach(() => { for (const detach of detached.splice(0)) detach(); vi.clearAllTimers(); vi.useRealTimers() })
async function harness() {
  let saved: ManuscriptSource = { project_id: 'p', chapter_id: 'a', title: '归来', content_text: '原来的正文。', word_count: 6, status: 'completed' }
  let active = true
  const read = vi.fn(async () => saved), save = vi.fn()
  const session = new ChapterDraftSession('p', 'a', { read, save, storage: null })
  detached.push(session.attach())
  await Promise.resolve()
  const ui = { active: () => active, phase: vi.fn(), warning: vi.fn(), error: vi.fn(), settled: vi.fn(), blocked: vi.fn() }
  const generation = createChapterGeneration(session, ui)!
  return { session, read, save, ui, generation,
    leave: () => { active = false; generation.detach() },
    persisted: (text: string, status = 'completed') => { saved = { ...saved, content_text: text, word_count: [...text].length, status } } }
}

it('does not treat saved events as confirmation of the current body', async () => {
  const h = await harness()
  h.generation.chunk('收到的生成稿。')
  h.generation.event({ status: 'saved' })
  expect(h.session.getSnapshot().source?.content_text).toBe('原来的正文。')
  h.generation.done()
  await Promise.resolve()
  expect(h.session.getSnapshot().text).toBe('收到的生成稿。')
  expect(h.session.getSnapshot().comparison?.content_text).toBe('原来的正文。')
  expect(h.session.getSnapshot().blocked).toBe(true)
  expect(h.save).not.toHaveBeenCalled()
})

it('keeps interrupted prose in its own session and verifies a partial saved body', async () => {
  const h = await harness()
  h.generation.chunk('他推开门。')
  h.generation.event({ event: 'generation_interrupted' })
  h.generation.event({ event: 'partial_saved', status: 'draft' })
  h.persisted('他推开门。', 'draft')
  h.generation.done()
  await Promise.resolve()
  expect(h.session.getSnapshot().source?.status).toBe('draft')
  expect(h.session.isDirty()).toBe(false)
  expect(h.ui.warning.mock.calls.flat().join()).toContain('本章尚未完成')
  expect(h.save).not.toHaveBeenCalled()
})

it.each([false, true])('retains unconfirmed fragments without inferring a save, failure=%s', async (failed) => {
  const h = await harness()
  h.generation.chunk('他推开门。')
  h.generation.event({ event: 'generation_interrupted' })
  if (failed) h.generation.event({ status: 'save_failed', reason: 'no_target_row' })
  h.generation.done()
  await Promise.resolve()
  expect(h.session.getSnapshot().text).toBe('他推开门。')
  expect(h.session.getSnapshot().source?.content_text).toBe('原来的正文。')
  expect(h.session.getSnapshot().statusToSave).toBe('draft')
  expect(h.save).not.toHaveBeenCalled()
})

it('never bypasses a quality failure with a client-side save', async () => {
  const h = await harness()
  h.generation.event({ event: 'quality_failed', final_text: '需要修改的完整候选。' })
  h.generation.done()
  await Promise.resolve()
  await vi.advanceTimersByTimeAsync(10000)
  expect(h.session.getSnapshot().text).toBe('需要修改的完整候选。')
  expect(h.session.getSnapshot().statusToSave).toBe('needs_review')
  expect(h.save).not.toHaveBeenCalled()
})

it('isolates late events after leaving the source chapter', async () => {
  const h = await harness()
  h.generation.chunk('甲章尚未保存的片段。')
  h.leave()
  h.generation.chunk('不该出现的迟到片段。')
  h.generation.event({ event: 'quality_failed', content_text: '错误的替换。' })
  h.generation.done()
  await Promise.resolve()
  expect(h.session.getSnapshot().text).toBe('甲章尚未保存的片段。')
  expect(h.ui.settled).toHaveBeenCalledTimes(1)
  expect(h.ui.error).not.toHaveBeenCalled()
})

it('does not reuse the initial save for an interrupted revision', async () => {
  const h = await harness()
  h.generation.chunk('首稿。')
  h.generation.event({ status: 'saved' })
  h.persisted('首稿。')
  h.generation.event({ event: 'revise_restart' })
  h.generation.chunk('新的半稿')
  h.generation.error(new Error('connection lost'))
  h.generation.done()
  await Promise.resolve()
  expect(h.session.getSnapshot().text).toBe('新的半稿')
  expect(h.session.getSnapshot().comparison?.content_text).toBe('首稿。')
  expect(h.session.getSnapshot().statusToSave).toBe('draft')
})

it('retains the original manuscript when cancellation happens before any prose', async () => {
  const h = await harness()
  h.generation.cancel()
  h.generation.done()
  await Promise.resolve()
  expect(h.session.getSnapshot().text).toBe('原来的正文。')
  expect(h.session.isDirty()).toBe(false)
  expect(h.save).not.toHaveBeenCalled()
})

it('verifies an actually saved review draft without converting it to a completed chapter', async () => {
  const h = await harness()
  h.generation.chunk('待审阅正文。')
  h.generation.event({ event: 'chapter_acceptance', status: 'needs_review' })
  h.persisted('待审阅正文。', 'needs_review')
  h.generation.done()
  await Promise.resolve()
  expect(h.session.getSnapshot().source?.status).toBe('needs_review')
  expect(h.session.isDirty()).toBe(false)
  expect(h.ui.warning.mock.calls.flat().join()).toContain('作者审读')
})

it('does not carry a previous candidates interrupted flag onto a fresh complete revision', async () => {
  const h = await harness()
  h.generation.event({ event: 'generation_interrupted' })
  h.generation.event({ event: 'revise_restart' })
  h.generation.chunk('完整的修订稿。')
  h.persisted('完整的修订稿。')
  h.generation.event({ status: 'saved' })
  h.generation.done()
  await Promise.resolve()
  expect(h.session.getSnapshot().statusToSave).toBeNull()
  expect(h.session.isDirty()).toBe(false)
})
