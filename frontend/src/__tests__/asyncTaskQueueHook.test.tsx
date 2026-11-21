import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { apiFetch } from '@/lib/api'
import { useAsyncTaskQueue } from '@/components/workspace/useAsyncTaskQueue'

const state = vi.hoisted(() => ({ submit: vi.fn(), receipt: null as Record<string, unknown> | null }))
vi.mock('@/lib/api', () => ({ apiFetch: vi.fn() }))
vi.mock('@/components/workspace/useGenerationSubmission', () => ({
  useGenerationSubmission: () => ({ session: { submit: state.submit }, state: { initialized: true, receipt: state.receipt } }),
}))
const api = vi.mocked(apiFetch)
function task(projectId: string, taskId = 'task', status = 'running') {
  return { project_id: projectId, task_id: taskId, task_type: 'chapter', chapter_id: 'chapter', volume_idx: null,
    status, char_count: 0, progress_text: '正在写作。', error_message: null }
}
afterEach(() => { cleanup(); state.receipt = null; vi.resetAllMocks(); vi.useRealTimers() })

it('ignores project A task lists that arrive after navigation to project B', async () => {
  const replies: ((value: unknown) => void)[] = []
  api.mockImplementation(async (path) => path.includes('/project/a?')
    ? new Promise((resolve) => { replies.push(resolve) }) : [task('b', 'b-task')])
  const view = renderHook(({ pid }) => useAsyncTaskQueue(pid), { initialProps: { pid: 'a' } })
  view.rerender({ pid: 'b' })
  await waitFor(() => expect(view.result.current.tasks[0]?.taskId).toBe('b-task'))
  await act(async () => { replies.forEach((resolve) => resolve([task('a', 'a-task')])) })
  expect(view.result.current.tasks.map((row) => row.taskId)).toEqual(['b-task'])
})

it('finds older active work from the bounded active list without fetching every candidate', async () => {
  api.mockImplementation(async (path) => path.includes('active_only') ? [task('book', 'old')] : [task('book', 'recent', 'completed')])
  const { result } = renderHook(() => useAsyncTaskQueue('book'))
  await waitFor(() => expect(result.current.tasks).toHaveLength(2))
  expect(api).toHaveBeenCalledTimes(2)
  expect(api.mock.calls.every(([path]) => path.includes('/async/project/'))).toBe(true)
})

it('retains completed when parallel list reads observed different task states', async () => {
  api.mockImplementation(async (path) => path.includes('active_only')
    ? [task('book', 'same', 'running')] : [task('book', 'same', 'completed')])
  const { result } = renderHook(() => useAsyncTaskQueue('book'))
  await waitFor(() => expect(result.current.tasks[0]?.status).toBe('completed'))
  expect(result.current.tasks).toHaveLength(1)
})

it('does not overlap task list reads when an earlier poll is still in flight', async () => {
  vi.useFakeTimers()
  const replies: ((value: unknown) => void)[] = []
  api.mockImplementation(async () => new Promise((resolve) => { replies.push(resolve) }))
  const { result } = renderHook(() => useAsyncTaskQueue('book'))
  await act(async () => { await result.current.refresh(); await result.current.refresh() })
  expect(api).toHaveBeenCalledTimes(2)
  await act(async () => { replies.forEach((resolve) => resolve([])) })
})

it('keeps a newly acknowledged task when an older empty list response arrives', async () => {
  const replies: ((value: unknown) => void)[] = []
  api.mockImplementation(async () => new Promise((resolve) => { replies.push(resolve) }))
  const view = renderHook(() => useAsyncTaskQueue('book'))
  state.receipt = task('book', 'new')
  view.rerender()
  await waitFor(() => expect(view.result.current.tasks[0]?.taskId).toBe('new'))
  await act(async () => { replies.forEach((resolve) => resolve([])) })
  expect(view.result.current.tasks[0]?.taskId).toBe('new')
})

it('sends the full author source and does not project a late submission onto a new book', async () => {
  api.mockResolvedValue([])
  let finish!: (value: unknown) => void
  state.submit.mockImplementation(() => new Promise((resolve) => { finish = resolve }))
  const view = renderHook(({ pid }) => useAsyncTaskQueue(pid), { initialProps: { pid: 'a' } })
  let pending!: Promise<unknown>
  act(() => { pending = view.result.current.submitChapterTask({ chapterId: 'chapter', expectedContentText: '完整原稿。' }) })
  view.rerender({ pid: 'b' })
  await act(async () => { finish(task('a')); await pending })
  expect(state.submit).toHaveBeenCalledWith(expect.objectContaining({ project_id: 'a', expected_content_text: '完整原稿。' }))
  expect(view.result.current.tasks).toEqual([])
})

it('rejects a list with a mismatched project instead of rendering that book', async () => {
  api.mockResolvedValue([task('other')])
  const { result } = renderHook(() => useAsyncTaskQueue('book'))
  await waitFor(() => expect(result.current.error).toContain('来源不一致'))
  expect(result.current.tasks).toEqual([])
})

it('does not let a pre-cancel list response undo a confirmed cancellation', async () => {
  api.mockResolvedValue([task('book')])
  const { result } = renderHook(() => useAsyncTaskQueue('book'))
  await waitFor(() => expect(result.current.tasks[0]?.status).toBe('running'))
  const old: ((value: unknown) => void)[] = []
  let cancelled = false
  api.mockImplementation(async (path) => {
    if (path.endsWith('/cancel')) { cancelled = true; return { task_id: 'task', status: 'cancelled' } }
    if (cancelled) return [task('book', 'task', 'cancelled')]
    return new Promise((resolve) => { old.push(resolve) })
  })
  act(() => { void result.current.refresh() })
  await waitFor(() => expect(old).toHaveLength(2))
  await act(async () => { await result.current.cancelTask('task') })
  await act(async () => { old.forEach((resolve) => resolve([task('book')])) })
  expect(result.current.tasks[0]?.status).toBe('cancelled')
})
