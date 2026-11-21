import { beforeEach, expect, it, vi } from 'vitest'
import { apiFetch, apiSSE } from '@/lib/api'
import { generateSavedBook, selectBookOutline, type BookOutline } from '@/lib/bookOutline'
vi.mock('@/lib/api', () => ({ apiFetch: vi.fn(), apiSSE: vi.fn() }))
const book: BookOutline = { id: 'current', project_id: 'p', level: 'book', version: 1, is_confirmed: 0,
  content_json: { raw_text: '当前完整书纲', volume_plan: [{ idx: 1 }] }, created_at: '2026-10-07T00:00:00Z' }
beforeEach(() => { vi.resetAllMocks(); vi.mocked(apiSSE).mockReturnValue(new AbortController()) })

it.each(['eof', 'done_only', 'saved_only', 'completed_only', 'save_failed', 'stage_error', 'transport'])('refuses incomplete or failed generation: %s', async mode => {
  const result = generateSavedBook({ project_id: 'p' }, vi.fn(), vi.fn(), vi.fn())
  const [, , , done, event, error] = vi.mocked(apiSSE).mock.calls[0]
  if (mode === 'done_only') event?.({ event: 'done', full_outline: '完整预览' })
  if (['saved_only', 'save_failed', 'stage_error', 'transport'].includes(mode)) event?.({ status: 'saved', outline_id: 'current' })
  if (['completed_only', 'save_failed', 'stage_error', 'transport'].includes(mode)) event?.({ status: 'completed' })
  if (mode === 'save_failed') event?.({ status: 'save_failed' })
  if (mode === 'stage_error') event?.({ event: 'error' })
  if (mode === 'transport') error?.(new Error('断开'))
  done()
  await expect(result).rejects.toThrow()
  expect(apiFetch).not.toHaveBeenCalled()
})

it.each(['valid', 'wrong_id', 'wrong_project', 'wrong_level', 'empty', 'missing'])('reads only the saved record and validates it: %s', async mode => {
  const value = structuredClone(book)
  if (mode === 'wrong_id') value.id = 'unrelated'
  if (mode === 'wrong_project') value.project_id = 'other'
  if (mode === 'wrong_level') value.level = 'volume'
  if (mode === 'empty') value.content_json.raw_text = ''
  if (mode === 'missing') vi.mocked(apiFetch).mockRejectedValue(new Error('404'))
  else vi.mocked(apiFetch).mockResolvedValue(value)
  const result = generateSavedBook({ project_id: 'p' }, vi.fn(), vi.fn(), vi.fn())
  const [, , , done, event] = vi.mocked(apiSSE).mock.calls[0]
  event?.({ status: 'saved', outline_id: 'current' }); event?.({ status: 'completed' }); done()
  if (mode === 'valid') await expect(result).resolves.toEqual(book)
  else await expect(result).rejects.toThrow()
  expect(apiFetch).toHaveBeenCalledExactlyOnceWith('/api/projects/p/outlines/current')
})

it('selects confirmed then recent books without interpreting UUIDs as dates', () => {
  const old = { ...book, id: 'zzzz', created_at: '2026-10-06T00:00:00Z' }
  expect(selectBookOutline([old, book])?.id).toBe('current')
  expect(selectBookOutline([{ ...old, is_confirmed: 1 }, book])?.id).toBe('zzzz')
  expect(selectBookOutline([{ ...book, content_json: { volume_idx: 1 } }])).toBeNull()
  expect(selectBookOutline([book])?.is_confirmed).toBe(0)
})
