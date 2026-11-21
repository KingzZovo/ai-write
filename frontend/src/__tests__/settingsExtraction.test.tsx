import { act, cleanup, fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { SettingsExtractionStatus, useSettingsExtraction } from '@/components/workspace/SettingsExtractionStatus'
import { apiFetch } from '@/lib/api'
vi.mock('@/lib/api', () => ({ apiFetch: vi.fn() }))
const api = vi.mocked(apiFetch)
const post = vi.fn()
const get = vi.fn()
beforeEach(() => {
  get.mockResolvedValue({ status: 'not_started' })
  api.mockImplementation((path, options) => options?.method === 'POST' ? post(path, options) : get(path, options))
})
const counts = { characters_created: 2, world_rules_created: 1, relationships_created: 3 }
afterEach(() => { cleanup(); vi.useRealTimers(); vi.resetAllMocks() })
function Harness() {
  const { state, start } = useSettingsExtraction('p', 'book', { raw_text: '已确认书纲' })
  return <SettingsExtractionStatus state={state} onStart={() => { void start() }} />
}

it('does not generate on load and displays actual results after an explicit action', async () => {
  post.mockResolvedValue(counts)
  render(<Harness />)
  expect(post).not.toHaveBeenCalled()
  fireEvent.click(screen.getByText('提取设定'))
  await waitFor(() => expect(screen.getByRole('status').textContent).toContain('新增 2 个人物、1 条世界规则、3 条关系'))
  expect(post.mock.calls[0][0]).toBe('/api/projects/p/outlines/book/extract-settings')
})

it('shows failure and retries only on request', async () => {
  post.mockRejectedValueOnce(new Error('502')).mockResolvedValueOnce(counts)
  render(<Harness />)
  fireEvent.click(screen.getByText('提取设定'))
  expect(await screen.findByRole('alert')).toHaveTextContent('已确认的书纲仍保留')
  expect(post).toHaveBeenCalledTimes(1)
  fireEvent.click(screen.getByText('重试提取'))
  await screen.findByText('核对结果')
  expect(post).toHaveBeenCalledTimes(2)
})

it('deduplicates synchronous starts while a request is pending', async () => {
  let resolve!: (value: unknown) => void
  post.mockImplementationOnce(() => new Promise(r => { resolve = r }))
  const { result } = renderHook(() => useSettingsExtraction('p', 'book'))
  act(() => { void result.current.start(); void result.current.start() })
  expect(result.current.state.status).toBe('running')
  expect(post).toHaveBeenCalledTimes(1)
  await act(async () => { resolve(counts) })
  expect(result.current.state.status).toBe('completed')
})

it.each(['project', 'outline', 'revision'])('ignores an old response after changing %s', async change => {
  let resolve!: (value: unknown) => void
  post.mockImplementationOnce(() => new Promise(r => { resolve = r }))
  const original = { project: 'p', outline: 'book', source: { raw_text: '原文' } }
  const { result, rerender } = renderHook(({ project, outline, source }) => useSettingsExtraction(project, outline, source), { initialProps: original })
  act(() => { void result.current.start() })
  rerender({ project: change === 'project' ? 'other' : 'p', outline: change === 'outline' ? 'other' : 'book',
    source: { raw_text: change === 'revision' ? '修订稿' : '原文' } })
  expect((post.mock.calls[0][1]?.signal as AbortSignal).aborted).toBe(true)
  await act(async () => { resolve(counts) })
  expect(result.current.state.status).toBe('idle')
})

it.each([{}, { ...counts, characters_created: -1 }, { ...counts, world_rules_created: '1' }])('rejects incomplete success responses', async value => {
  post.mockResolvedValue(value)
  const { result } = renderHook(() => useSettingsExtraction('p', 'book'))
  await act(async () => { await result.current.start() })
  expect(result.current.state.status).toBe('failed')
})

it('accepts a verified zero count as a completed result', async () => {
  post.mockResolvedValue({ characters_created: 0, world_rules_created: 0, relationships_created: 0 })
  const { result } = renderHook(() => useSettingsExtraction('p', 'book'))
  await act(async () => { await result.current.start() })
  expect(result.current.state.status).toBe('completed')
})

it('aborts the browser request on unmount without retrying', async () => {
  let reject!: (error: Error) => void
  post.mockImplementationOnce(() => new Promise((_resolve, r) => { reject = r }))
  const { result, unmount } = renderHook(() => useSettingsExtraction('p', 'book'))
  act(() => { void result.current.start() })
  unmount()
  expect((post.mock.calls[0][1]?.signal as AbortSignal).aborted).toBe(true)
  await act(async () => { reject(new Error('aborted')) })
  expect(post).toHaveBeenCalledTimes(1)
})

it('restores completed counts on reload without calling the model', async () => {
  get.mockResolvedValue({ status: 'completed', counts })
  render(<Harness />)
  await screen.findByText('核对结果')
  expect(screen.getByRole('status')).toHaveTextContent('新增 2 个人物')
  expect(post).not.toHaveBeenCalled()
})

it.each(['pending', 'failed'])('polls a persisted %s receipt until delivery completes', async status => {
  vi.useFakeTimers()
  get.mockResolvedValueOnce({ status }).mockResolvedValue({ status: 'completed', counts })
  const { result } = renderHook(() => useSettingsExtraction('p', 'book'))
  await act(async () => {})
  expect(result.current.state.status).toBe('recovering')
  await act(async () => { await vi.advanceTimersByTimeAsync(5000) })
  expect(result.current.state.status).toBe('completed')
  expect(post).not.toHaveBeenCalled()
})

it('discards a late status read after switching books', async () => {
  let resolve!: (value: unknown) => void
  get.mockImplementationOnce(() => new Promise(r => { resolve = r }))
  const { result, rerender } = renderHook(({ book }) => useSettingsExtraction('p', book),
    { initialProps: { book: 'old' } })
  rerender({ book: 'new' })
  await act(async () => { resolve({ status: 'completed', counts }) })
  expect(result.current.state.status).toBe('idle')
  expect(post).not.toHaveBeenCalled()
})
