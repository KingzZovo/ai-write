import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { CheckerDashboard } from '@/components/panels/CheckerDashboard'
import { apiFetch } from '@/lib/api'
import { CHECKER_ORDER, parseQualityReview } from '@/lib/qualityReview'

vi.mock('@/lib/api', () => ({ apiFetch: vi.fn() }))
const api = vi.mocked(apiFetch)
function response(chapter = 'a') {
  return {
    chapter_id: chapter, review_status: 'complete', overall_score: 8.3, passed: true, errors: [],
    checkers: CHECKER_ORDER.map((name) => ({
      name, review_status: 'complete', score: 9, passed: true, issues: [] as {type: string; severity: string; location: string; description: string; suggestion: string}[],
      errors: [] as {stage: string; code: string}[], observations: [],
    })),
  }
}
afterEach(() => { cleanup(); vi.resetAllMocks() })

it('shows backend names and its aggregate without recalculating an average', async () => {
  api.mockResolvedValue(response())
  render(<CheckerDashboard chapterId="a" />)
  fireEvent.click(screen.getByText('运行质量检查'))
  await screen.findByText('自动检查已完成，仍需作者审读。')
  expect(screen.getByText('8.3')).toBeTruthy()
  expect(screen.getByText('一致性')).toBeTruthy()
  expect(screen.getByText('连续性')).toBeTruthy()
  expect(screen.queryByText('综合质量评分 / 10')).toBeNull()
})

it('keeps findings visible when one check failed without showing a neutral score', async () => {
  const data = response()
  data.review_status = 'incomplete'
  data.checkers[0].review_status = 'incomplete'
  data.checkers[0].errors = [{ stage: 'consistency_llm', code: 'model_review_unavailable' }]
  data.checkers[1].issues = [{type: 'resource', severity: 'critical', location: '交了两千元', description: '核对这一次交付的金额。', suggestion: '结合前文确认。'}]
  api.mockResolvedValue(data)
  render(<CheckerDashboard chapterId="a" />)
  fireEvent.click(screen.getByText('运行质量检查'))
  await screen.findByText('检查未完成，暂不提供综合参考值。')
  expect(screen.queryByText('自动检查参考值 / 10')).toBeNull()
  fireEvent.click(screen.getByText('连续性'))
  expect(screen.getByText('核对这一次交付的金额。')).toBeTruthy()
  expect(screen.getByText('严重')).toBeTruthy()
})

it('treats legacy scores without delivery metadata as unknown', () => {
  const data = response()
  const { review_status: _ignored, ...legacy } = data
  const result = parseQualityReview(legacy, 'a')
  expect(result.review_status).toBe('unknown')
  expect(result.overall_score).toBeNull()
  expect(result.passed).toBe(false)
})

it('does not declare completion when a requested checker is missing', () => {
  const data = response()
  data.checkers.pop()
  expect(parseQualityReview(data, 'a').review_status).toBe('incomplete')
})

it('rejects nonfinite scores and mismatched chapter results', () => {
  const data = response()
  data.checkers[0].score = Number.NaN
  expect(parseQualityReview(data, 'a').overall_score).toBeNull()
  expect(() => parseQualityReview(data, 'b')).toThrow('不匹配')
})

it('does not let a late response from another chapter populate this chapter', async () => {
  let resolveA!: (data: ReturnType<typeof response>) => void
  api.mockImplementationOnce(() => new Promise((resolve) => { resolveA = resolve }))
  const view = render(<CheckerDashboard chapterId="a" />)
  fireEvent.click(screen.getByText('运行质量检查'))
  const signalA = api.mock.calls[0][1]?.signal
  view.rerender(<CheckerDashboard chapterId="b" />)
  expect(signalA?.aborted).toBe(true)
  api.mockResolvedValueOnce(response('b'))
  fireEvent.click(screen.getByText('运行质量检查'))
  await screen.findByText('自动检查已完成，仍需作者审读。')
  await act(async () => { resolveA({ ...response('a'), overall_score: 1.1 }) })
  expect(screen.getByText('8.3')).toBeTruthy()
  expect(screen.queryByText('1.1')).toBeNull()
})

it('clears prior completion on chapter change and on a failed new check', async () => {
  api.mockResolvedValueOnce(response())
  const view = render(<CheckerDashboard chapterId="a" />)
  fireEvent.click(screen.getByText('运行质量检查'))
  await screen.findByText('8.3')
  api.mockRejectedValueOnce(new Error('评审服务暂不可用'))
  fireEvent.click(screen.getByText('运行质量检查'))
  await screen.findByText('评审服务暂不可用')
  expect(screen.queryByText('8.3')).toBeNull()
  view.rerender(<CheckerDashboard chapterId="b" />)
  expect(screen.queryByText('评审服务暂不可用')).toBeNull()
  expect(screen.queryByText('自动检查已完成，仍需作者审读。')).toBeNull()
})

it('submits only one request while a check is active', async () => {
  api.mockImplementation(() => new Promise(() => {}))
  render(<CheckerDashboard chapterId="a" />)
  const button = screen.getByText('运行质量检查')
  fireEvent.click(button)
  fireEvent.click(button)
  expect(api).toHaveBeenCalledTimes(1)
})

it('reports malformed issue data instead of rendering a clean result', async () => {
  const data = response()
  api.mockResolvedValue({ ...data, checkers: [{...data.checkers[0], issues: null}, ...data.checkers.slice(1)] })
  render(<CheckerDashboard chapterId="a" />)
  fireEvent.click(screen.getByText('运行质量检查'))
  await screen.findByText('检查明细不完整，请重新检查。')
  expect(screen.queryByText('自动检查参考值 / 10')).toBeNull()
})
