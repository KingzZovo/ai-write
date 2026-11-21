import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { EvaluationPanel } from '@/components/panels/EvaluationPanel'
import { apiFetch } from '@/lib/api'
vi.mock('@/lib/api', () => ({ apiFetch: vi.fn() }))
const api = vi.mocked(apiFetch)
const completed = (chapter = 'a', extra = {}) => ({ chapter_id: chapter, task_id: 'task', status: 'completed', result: {
  overall: 9, plot_coherence: 9, character_consistency: 9, style_adherence: 9, narrative_pacing: 9, foreshadow_handling: 9,
  issues: [{description: '检查这一句', severity: 'new-severity'}], ...extra,
} })
afterEach(() => { cleanup(); vi.resetAllMocks(); vi.useRealTimers() })
it('loads backend fields and handles missing/unknown severity', async () => {
  api.mockResolvedValue(completed())
  render(<EvaluationPanel chapterId="a" />)
  await screen.findByText('检查这一句')
  expect(api.mock.calls[0][0]).toBe('/api/evaluate/chapters/a/latest')
  expect(screen.getByText('综合评分 / 10')).toBeTruthy()
})
it('does not display failure sentinel as a literary score', async () => {
  api.mockResolvedValue(completed('a', {overall: 0, parse_failed: true, failure_reason: 'call_failed'}))
  render(<EvaluationPanel chapterId="a" />)
  await screen.findByText('评审模型调用失败，未获得有效评分。')
  expect(screen.queryByText('综合评分 / 10')).toBeNull()
})
it('starts once and polls the returned task', async () => {
  vi.useFakeTimers()
  api.mockResolvedValueOnce(null).mockResolvedValueOnce({chapter_id: 'a', task_id: 't1', status: 'pending'}).mockResolvedValueOnce({...completed(), task_id: 't1'})
  render(<EvaluationPanel chapterId="a" />)
  await act(async () => {})
  fireEvent.click(screen.getByText('运行评估'))
  await act(async () => {})
  expect(api.mock.calls[1][0]).toBe('/api/evaluate/start')
  expect(JSON.parse(api.mock.calls[1][1]?.body as string).chapter_id).toBe('a')
  await act(async () => { await vi.advanceTimersByTimeAsync(2000) })
  expect(api.mock.calls[2][0]).toBe('/api/evaluate/tasks/t1')
  expect(screen.getByText('检查这一句')).toBeTruthy()
})
it('discards late results when chapters change', async () => {
  let resolve!: (value: unknown) => void
  api.mockImplementationOnce(() => new Promise(r => { resolve = r })).mockResolvedValueOnce(null)
  const view = render(<EvaluationPanel chapterId="a" />)
  view.rerender(<EvaluationPanel chapterId="b" />)
  await waitFor(() => expect(api).toHaveBeenCalledTimes(2))
  await act(async () => { resolve(completed()) })
  expect(screen.queryByText('检查这一句')).toBeNull()
  expect(api.mock.calls[0][1]?.signal?.aborted).toBe(true)
})
it('rejects a task returned for a different chapter', async () => {
  api.mockResolvedValue(completed('other'))
  render(<EvaluationPanel chapterId="a" />)
  await screen.findByText(/无法获取评审结果/)
  expect(screen.queryByText('综合评分 / 10')).toBeNull()
})

it('requires refreshing after a network failure before another start', async () => {
  api.mockRejectedValueOnce(new Error('network'))
  render(<EvaluationPanel chapterId="a" />)
  await screen.findByText(/无法获取评审结果/)
  expect((screen.getByText('运行评估') as HTMLButtonElement).disabled).toBe(true)
  api.mockResolvedValueOnce(null)
  fireEvent.click(screen.getByText('刷新评审状态'))
  await waitFor(() => expect((screen.getByText('运行评估') as HTMLButtonElement).disabled).toBe(false))
})

it('keeps incomplete evaluations out of the literary score display', async () => {
  api.mockResolvedValue(completed('a', {overall: 0, parse_failed: true, failure_reason: 'incomplete_response'}))
  render(<EvaluationPanel chapterId="a" />)
  await screen.findByText('评审响应未完整结束，未获得有效评分。请保留正文后重试评审。')
  expect(screen.queryByText('综合评分 / 10')).toBeNull()
})

it.each([
  ['changed', '正文或细纲已变化，以下是旧稿评审，请重新评估。'],
  ['unchanged', '本次读取核对：正文与细纲和评审时一致。其他设定仍需核查。'],
  ['unknown', '此评审未记录稿件来源，以下仅作历史参考。'],
])('labels evaluation source status %s', async (source_status, label) => {
  api.mockResolvedValue({...completed(), source_status})
  render(<EvaluationPanel chapterId="a" />)
  await screen.findByText(label)
  expect(screen.getByText('综合评分 / 10')).toBeTruthy()
})
