import { afterEach, expect, it, vi } from 'vitest'
import { MobileTask, mergeMobileTaskLists, parseMobileTaskDetail, parseMobileTasks, watchMobileTask } from '@/components/workspace/mobileTaskProgress'

const task: MobileTask = { taskId: 't', projectId: 'p', chapterId: 'a', volumeIdx: null, taskType: 'chapter', status: 'running' }
const response = (values: Record<string, unknown> = {}) => ({ task_id: 't', project_id: 'p', chapter_id: 'a', task_type: 'chapter',
  status: 'running', volume_idx: null, progress_text: '正在检查章节。', result_text: '', polished_text: '', error_message: null, ...values })
afterEach(() => vi.useRealTimers())

it.each(['completed', 'needs_review', 'needs_repair', 'failed', 'cancelled'])(
  'keeps %s when the parallel active list still contains an earlier state', (status) => {
    const terminal = { ...task, status }
    const other = { ...task, taskId: 'older-active' }
    expect(mergeMobileTaskLists([terminal], [task, other])).toEqual([terminal, other])
    expect(mergeMobileTaskLists([task], [terminal])).toEqual([terminal])
  },
)

it.each([
  { task_id: 'other' }, { project_id: 'other' }, { chapter_id: 'other' }, { task_type: 'outline_book' },
  { chapter_id: undefined }, { project_id: undefined }, { progress_text: null }, { result_text: 17 },
  { volume_idx: 2 },
])('rejects a mismatched or incomplete detail instead of changing its target: %j', (change) => {
  expect(() => parseMobileTaskDetail(response(change), task)).toThrow()
})

it('keeps complete candidate and operational progress as distinct values', () => {
  const text = '沈青把两千元交到弟弟手里。'.repeat(2000)
  const detail = parseMobileTaskDetail(response({ result_text: text }), task)
  expect(detail.resultText).toBe(text)
  expect(detail.progressText).toBe('正在检查章节。')
  expect(detail.projectId).toBe('p')
})

it('does not interpret a failed or foreign project list as an empty queue', () => {
  expect(() => parseMobileTasks({}, 'p')).toThrow()
  expect(() => parseMobileTasks([response({ project_id: 'other' })], 'p')).toThrow()
  expect(() => parseMobileTasks([response(), response()], 'p')).toThrow()
})

it('waits for each read before scheduling the next, avoiding overlapping responses', async () => {
  vi.useFakeTimers()
  let finish!: (value: unknown) => void
  const read = vi.fn((_signal: AbortSignal) => new Promise<unknown>((resolve) => { finish = resolve }))
  const received = vi.fn(), failed = vi.fn()
  const detach = watchMobileTask(task, { received, failed }, read)
  await vi.advanceTimersByTimeAsync(9000)
  expect(read).toHaveBeenCalledTimes(1)
  finish(response())
  await vi.advanceTimersByTimeAsync(2999)
  expect(read).toHaveBeenCalledTimes(1)
  await vi.advanceTimersByTimeAsync(1)
  expect(read).toHaveBeenCalledTimes(2)
  detach()
  finish(response({ result_text: '迟到全文。' }))
  await vi.advanceTimersByTimeAsync(9000)
  expect(received).toHaveBeenCalledTimes(1)
  expect(failed).not.toHaveBeenCalled()
  expect(read.mock.calls[0]?.[0]?.aborted).toBe(true)
})

it.each(['completed', 'needs_review', 'needs_repair', 'failed', 'cancelled'])('stops polling on %s while preserving its complete result', async (status) => {
  vi.useFakeTimers()
  const read = vi.fn(async () => response({ status, result_text: '完整候选。' }))
  const received = vi.fn(), failed = vi.fn()
  const detach = watchMobileTask(task, { received, failed }, read)
  await vi.advanceTimersByTimeAsync(12000)
  expect(read).toHaveBeenCalledTimes(1)
  expect(received.mock.calls[0][0].resultText).toBe('完整候选。')
  expect(failed).not.toHaveBeenCalled()
  detach()
})

it('keeps a read failure visible and waits for explicit retry', async () => {
  vi.useFakeTimers()
  const read = vi.fn(async () => { throw new Error('暂不可读') })
  const received = vi.fn(), failed = vi.fn()
  const detach = watchMobileTask(task, { received, failed }, read)
  await vi.advanceTimersByTimeAsync(12000)
  expect(read).toHaveBeenCalledTimes(1)
  expect(received).not.toHaveBeenCalled()
  expect(failed).toHaveBeenCalledWith('暂不可读')
  detach()
})

it('surfaces an unfamiliar state without starting an endless poll', async () => {
  vi.useFakeTimers()
  const read = vi.fn(async () => response({ status: 'future_state' }))
  const received = vi.fn(), failed = vi.fn()
  const detach = watchMobileTask(task, { received, failed }, read)
  await vi.advanceTimersByTimeAsync(12000)
  expect(received).toHaveBeenCalledTimes(1)
  expect(failed).toHaveBeenCalledTimes(1)
  expect(read).toHaveBeenCalledTimes(1)
  detach()
})
