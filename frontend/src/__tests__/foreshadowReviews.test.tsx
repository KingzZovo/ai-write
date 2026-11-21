import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { ForeshadowReviewList } from '@/components/panels/ForeshadowReviewList'
import { apiFetch } from '@/lib/api'
vi.mock('@/lib/api', () => ({ apiFetch: vi.fn() }))
vi.mock('@/components/panels/ForeshadowReviewJobs', () => ({ ForeshadowReviewJobs: () => null }))
vi.mock('@/components/panels/ForeshadowDiscoveryList', () => ({ ForeshadowDiscoveryList: () => null }))
afterEach(() => { cleanup(); vi.resetAllMocks(); vi.useRealTimers() })
const item = { id: 'r', status: 'pending', evidence: '陈伯出示了寄信的存根。', reason: '身份有了证据，但需核对原悬念。',
  source_state: 'changed', source_snapshot: { global_idx: 913, foreshadow: { description: '寄信人是谁' } } }

it('shows source changes alongside the quote without claiming payoff confirmed', async () => {
  vi.mocked(apiFetch).mockResolvedValue({ reviews: [item], has_more: false })
  render(<ForeshadowReviewList projectId="p" />)
  expect(await screen.findByText(item.evidence)).toBeTruthy()
  expect(screen.getByText(/来源已修改/)).toBeTruthy()
  expect(screen.getByText(/建议尚未确认/)).toBeTruthy()
  expect(screen.getByText(/全书第 913 章/)).toBeTruthy()
  expect(screen.queryByText('已兑现')).toBeNull()
})

it('paginates and allows recovery from a failed read', async () => {
  vi.mocked(apiFetch).mockRejectedValueOnce(new Error('offline'))
    .mockResolvedValueOnce({ reviews: [item], has_more: true })
    .mockResolvedValueOnce({ reviews: [{ ...item, id: 'next', evidence: '第二页证据' }], has_more: false })
  render(<ForeshadowReviewList projectId="p" />)
  await screen.findByRole('alert')
  fireEvent.click(screen.getByText('重试'))
  await screen.findByText(item.evidence)
  fireEvent.click(screen.getByText('下一页建议'))
  await screen.findByText('第二页证据')
  expect(vi.mocked(apiFetch).mock.calls.at(-1)?.[0]).toContain('offset=20')
  expect(screen.queryByText(item.evidence)).toBeNull()
})

it('does not replace a new project view with a late response for an old project', async () => {
  let resolveOld!: (value: unknown) => void
  vi.mocked(apiFetch).mockImplementationOnce(() => new Promise(resolve => { resolveOld = resolve }))
    .mockResolvedValueOnce({ reviews: [{ ...item, evidence: '新作品证据' }], has_more: false })
  const view = render(<ForeshadowReviewList key="old" projectId="old" />)
  view.rerender(<ForeshadowReviewList key="new" projectId="new" />)
  await screen.findByText('新作品证据')
  resolveOld({ reviews: [item], has_more: false })
  await waitFor(() => expect(screen.queryByText(item.evidence)).toBeNull())
})


it('revalidates source badges when parent reports a completed local change', async () => {
  vi.mocked(apiFetch).mockResolvedValueOnce({ reviews: [{ ...item, source_state: 'current' }], has_more: false })
    .mockResolvedValueOnce({ reviews: [item], has_more: false })
  const view = render(<ForeshadowReviewList projectId="p" refreshVersion={0} />)
  await screen.findByText(/读取时与稿件/)
  view.rerender(<ForeshadowReviewList projectId="p" refreshVersion={1} />)
  await screen.findByText(/来源已修改/)
  expect(screen.queryByText(/读取时与稿件/)).toBeNull()
})

it('keeps evidence visible until dismissal is acknowledged, then refreshes', async () => {
  let acknowledge!: (value: unknown) => void
  vi.mocked(apiFetch).mockResolvedValueOnce({ reviews: [item], has_more: false })
    .mockImplementationOnce(() => new Promise(resolve => { acknowledge = resolve }))
    .mockResolvedValueOnce({ reviews: [], has_more: false })
  render(<ForeshadowReviewList projectId="p" />)
  await screen.findByText(item.evidence)
  fireEvent.click(screen.getByText('忽略此建议'))
  expect(screen.getByText(item.evidence)).toBeTruthy()
  expect(screen.getByText('正在忽略…')).toBeTruthy()
  acknowledge({ id: item.id, status: 'dismissed' })
  await screen.findByText('暂无待审阅建议。')
  expect(screen.queryByText(item.evidence)).toBeNull()
  expect(vi.mocked(apiFetch).mock.calls[1]).toEqual([
    '/api/projects/p/foreshadows/reviews/r/dismiss', { method: 'POST' },
  ])
})

it.each([new Error('failed'), { id: 'wrong', status: 'dismissed' }])('keeps evidence on a failed or invalid dismissal acknowledgment', async result => {
  vi.mocked(apiFetch).mockResolvedValueOnce({ reviews: [item], has_more: false })
  if (result instanceof Error) vi.mocked(apiFetch).mockRejectedValueOnce(result)
  else vi.mocked(apiFetch).mockResolvedValueOnce(result)
  render(<ForeshadowReviewList projectId="p" />)
  await screen.findByText(item.evidence)
  fireEvent.click(screen.getByText('忽略此建议'))
  await screen.findByRole('alert')
  expect(screen.getByText(item.evidence)).toBeTruthy()
})

it('accepts a current-source review only after server acknowledgment and refreshes the parent', async () => {
  let acknowledge!: (value: unknown) => void
  const onAccepted = vi.fn()
  vi.mocked(apiFetch).mockResolvedValueOnce({ reviews: [{ ...item, source_state: 'current' }], has_more: false })
    .mockImplementationOnce(() => new Promise(resolve => { acknowledge = resolve }))
    .mockResolvedValueOnce({ reviews: [], has_more: false })
  render(<ForeshadowReviewList projectId="p" onAccepted={onAccepted} />)
  await screen.findByText(item.evidence)
  fireEvent.click(screen.getByText('核对后确认兑现'))
  expect(onAccepted).not.toHaveBeenCalled()
  expect(screen.getByText(item.evidence)).toBeTruthy()
  acknowledge({ id: item.id, status: 'accepted' })
  await screen.findByText('暂无待审阅建议。')
  expect(onAccepted).toHaveBeenCalledOnce()
  expect(vi.mocked(apiFetch).mock.calls[1]?.[0]).toContain('/r/accept')
})

it.each(['changed', 'unknown', 'projection_current'])('does not allow acceptance when source state is %s', async source_state => {
  vi.mocked(apiFetch).mockResolvedValue({ reviews: [{ ...item, source_state }], has_more: false })
  render(<ForeshadowReviewList projectId="p" />)
  await screen.findByText(item.evidence)
  expect((screen.getByText('核对后确认兑现') as HTMLButtonElement).disabled).toBe(true)
  expect((screen.getByText('忽略此建议') as HTMLButtonElement).disabled).toBe(false)
})

it('preserves the review on an acceptance conflict', async () => {
  const onAccepted = vi.fn()
  vi.mocked(apiFetch).mockResolvedValueOnce({ reviews: [{ ...item, source_state: 'current' }], has_more: false })
    .mockRejectedValueOnce(new Error('409'))
  render(<ForeshadowReviewList projectId="p" onAccepted={onAccepted} />)
  await screen.findByText(item.evidence)
  fireEvent.click(screen.getByText('核对后确认兑现'))
  await screen.findByRole('alert')
  expect(screen.getByText(item.evidence)).toBeTruthy()
  expect(onAccepted).not.toHaveBeenCalled()
})

it('shows accepted history without mutation buttons and resets pagination on filter change', async () => {
  vi.mocked(apiFetch).mockResolvedValueOnce({ reviews: [item], has_more: true })
    .mockResolvedValueOnce({ reviews: [item], has_more: false })
    .mockResolvedValueOnce({ reviews: [{ ...item, status: 'accepted', source_state: 'accepted_current' }], has_more: false })
  render(<ForeshadowReviewList projectId="p" />)
  await screen.findByText(item.evidence)
  fireEvent.click(screen.getByText('下一页建议'))
  await screen.findByText('第 2 页')
  fireEvent.change(screen.getByLabelText('建议处理状态'), { target: { value: 'accepted' } })
  await screen.findByText(/读取时与作者确认的兑现结果一致/)
  expect(vi.mocked(apiFetch).mock.calls.at(-1)?.[0]).toContain('offset=0&status=accepted')
  expect(screen.queryByText('核对后确认兑现')).toBeNull()
  expect(screen.queryByText('忽略此建议')).toBeNull()
  expect(screen.queryByText(/建议尚未确认/)).toBeNull()
})

it('shows dismissed history and its empty state without pending actions', async () => {
  vi.mocked(apiFetch).mockResolvedValueOnce({ reviews: [item], has_more: false })
    .mockResolvedValueOnce({ reviews: [], has_more: false })
  render(<ForeshadowReviewList projectId="p" />)
  await screen.findByText(item.evidence)
  fireEvent.change(screen.getByLabelText('建议处理状态'), { target: { value: 'dismissed' } })
  await screen.findByText('暂无已忽略建议。')
  expect(vi.mocked(apiFetch).mock.calls.at(-1)?.[0]).toContain('status=dismissed')
  expect(screen.queryByText('忽略此建议')).toBeNull()
})

it('displays recorded decision identity and explicitly marks missing legacy history', async () => {
  vi.mocked(apiFetch).mockResolvedValueOnce({ reviews: [], has_more: false })
    .mockResolvedValueOnce({ reviews: [
      { ...item, status: 'accepted', handled_by: 'writer', handled_at: '2026-10-08T12:00:00Z' },
      { ...item, id: 'legacy', status: 'accepted', handled_by: null, handled_at: null },
    ], has_more: false })
  const { container } = render(<ForeshadowReviewList projectId="p" />)
  await screen.findByText('暂无待审阅建议。')
  fireEvent.change(screen.getByLabelText('建议处理状态'), { target: { value: 'accepted' } })
  await screen.findByText(/处理账号：writer/)
  expect(container.querySelector('time')?.getAttribute('datetime')).toBe('2026-10-08T12:00:00Z')
  expect(screen.getByText('处理账号：未记录 · 处理时间：未记录')).toBeTruthy()
})

it('retains unavailable-source evidence while disabling acceptance and allowing dismissal', async () => {
  vi.mocked(apiFetch).mockResolvedValue({ reviews: [{ ...item, source_state: 'unavailable' }], has_more: false })
  render(<ForeshadowReviewList projectId="p" />)
  await screen.findByText(item.evidence)
  expect(screen.getByText(/原章节或伏笔已不可用/)).toBeTruthy()
  expect(screen.getByText('核对后确认兑现')).toBeDisabled()
  expect(screen.getByText('忽略此建议')).not.toBeDisabled()
})

it('requests graph confirmation, shows pending synchronization, and does not claim immediate acceptance', async () => {
  const onAccepted = vi.fn()
  const graphItem = { ...item, source_state: 'projection_current', source_snapshot: { foreshadow: { description: '寄信人', source: 'neo4j' } } }
  vi.mocked(apiFetch).mockResolvedValueOnce({ reviews: [graphItem], has_more: false })
    .mockResolvedValueOnce({ id: 'r', status: 'confirming' })
    .mockResolvedValueOnce({ reviews: [{ ...graphItem, status: 'confirming', confirmation_status: 'pending' }], has_more: false })
  render(<ForeshadowReviewList projectId="p" onAccepted={onAccepted} />)
  await screen.findByText(item.evidence)
  expect(screen.getByText('核对后确认兑现')).not.toBeDisabled()
  fireEvent.click(screen.getByText('核对后确认兑现'))
  await screen.findByText('确认同步中…')
  expect(screen.getByLabelText('建议处理状态')).toHaveValue('confirming')
  expect(onAccepted).not.toHaveBeenCalled()
  expect(screen.queryByText('忽略此建议')).toBeNull()
})

it('allows retry only for failed synchronization and dismissal only after a rejected graph decision', async () => {
  vi.mocked(apiFetch).mockResolvedValueOnce({ reviews: [], has_more: false })
    .mockResolvedValueOnce({ reviews: [{ ...item, status: 'confirming', confirmation_status: 'failed' }], has_more: false })
    .mockResolvedValueOnce({ id: 'r', status: 'confirming' })
    .mockResolvedValueOnce({ reviews: [{ ...item, status: 'confirming', confirmation_status: 'rejected' }], has_more: false })
  render(<ForeshadowReviewList projectId="p" />)
  await screen.findByText('暂无待审阅建议。')
  fireEvent.change(screen.getByLabelText('建议处理状态'), { target: { value: 'confirming' } })
  await screen.findByText('重试同步')
  expect(screen.queryByText('放弃此确认')).toBeNull()
  fireEvent.click(screen.getByText('重试同步'))
  await screen.findByText('放弃此确认')
  expect(screen.queryByText('重试同步')).toBeNull()
  expect(screen.getByText(/本次确认未执行/)).toBeTruthy()
})


it('polls confirmation work and refreshes foreshadows only after it leaves the syncing list', async () => {
  vi.useFakeTimers()
  const onAccepted = vi.fn()
  vi.mocked(apiFetch).mockResolvedValueOnce({ reviews: [], has_more: false })
    .mockResolvedValueOnce({ reviews: [{ ...item, status: 'confirming', confirmation_status: 'running' }], has_more: false })
    .mockResolvedValueOnce({ reviews: [], has_more: false })
  await act(async () => { render(<ForeshadowReviewList projectId="p" onAccepted={onAccepted} />) })
  await act(async () => { fireEvent.change(screen.getByLabelText('建议处理状态'), { target: { value: 'confirming' } }) })
  expect(onAccepted).not.toHaveBeenCalled()
  await act(async () => { await vi.advanceTimersByTimeAsync(5000) })
  expect(onAccepted).toHaveBeenCalledOnce()
  expect(screen.getByText('暂无待同步确认。')).toBeTruthy()
  await act(async () => { await vi.advanceTimersByTimeAsync(10000) })
  expect(apiFetch).toHaveBeenCalledTimes(3)
})
