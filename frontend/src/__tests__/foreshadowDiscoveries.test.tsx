import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { ForeshadowDiscoveryList } from '@/components/panels/ForeshadowDiscoveryList'
import { ForeshadowReviewList } from '@/components/panels/ForeshadowReviewList'
import { ForeshadowReviewJobs } from '@/components/panels/ForeshadowReviewJobs'
import { apiFetch, ApiError } from '@/lib/api'

vi.mock('@/lib/api', async importOriginal => ({ ...await importOriginal<typeof import('@/lib/api')>(), apiFetch: vi.fn() }))
afterEach(() => { cleanup(); vi.resetAllMocks() })

const item = { id: 'candidate', description: '陈伯为什么保留这张存根？', evidence: '陈伯把寄信的存根放在桌上。',
  reason: '是否值得继续铺写需作者对照前文。', status: 'pending', source_state: 'current', source_snapshot: { global_idx: 913 } }
const page = (items = [item], hasMore = false) => ({ discoveries: items, has_more: hasMore })
function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (error: Error) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

it('shows complete evidence as a candidate and blocks stale-source acceptance', async () => {
  vi.mocked(apiFetch).mockResolvedValue(page([{ ...item, source_state: 'changed' }]))
  render(<ForeshadowDiscoveryList projectId="p" refreshToken="0" />)
  await screen.findByText(item.evidence)
  expect(screen.getByText(/全书第 913 章/)).toBeTruthy()
  expect(screen.getByText(/候选不会自动加入/)).toBeTruthy()
  expect((screen.getByText('核对后登记为伏笔') as HTMLButtonElement).disabled).toBe(true)
  expect((screen.getByText('忽略此线索') as HTMLButtonElement).disabled).toBe(false)
})

it('retains evidence until acceptance is acknowledged and sends one request for repeated clicks', async () => {
  const response = deferred<unknown>(); const onAccepted = vi.fn()
  vi.mocked(apiFetch).mockResolvedValueOnce(page()).mockImplementationOnce(() => response.promise).mockResolvedValue(page([]))
  render(<ForeshadowDiscoveryList projectId="p" refreshToken="0" onAccepted={onAccepted} />)
  await screen.findByText(item.evidence)
  fireEvent.click(screen.getByText('核对后登记为伏笔')); fireEvent.click(screen.getByText('核对后登记为伏笔'))
  expect(screen.getByText(item.evidence)).toBeTruthy()
  expect(onAccepted).not.toHaveBeenCalled()
  expect(vi.mocked(apiFetch).mock.calls.filter(([, options]) => options?.method === 'POST')).toHaveLength(1)
  response.resolve({ id: item.id, status: 'accepted', foreshadow_id: item.id })
  await waitFor(() => expect(onAccepted).toHaveBeenCalledTimes(1))
  await screen.findByText(/暂无待核对线索/)
})

it('keeps uncertain results visible and retries the same candidate identity', async () => {
  vi.mocked(apiFetch).mockResolvedValueOnce(page()).mockRejectedValueOnce(new Error('response lost'))
    .mockResolvedValueOnce({ id: item.id, status: 'accepted', foreshadow_id: item.id }).mockResolvedValue(page([]))
  render(<ForeshadowDiscoveryList projectId="p" refreshToken="0" />)
  await screen.findByText(item.evidence)
  fireEvent.click(screen.getByText('核对后登记为伏笔'))
  await screen.findByRole('alert')
  expect(screen.getByText(item.evidence)).toBeTruthy()
  fireEvent.click(screen.getByText('核对后登记为伏笔'))
  await screen.findByText(/暂无待核对线索/)
  const posts = vi.mocked(apiFetch).mock.calls.filter(([, options]) => options?.method === 'POST')
  expect(posts.map(([url]) => url)).toEqual(Array(2).fill('/api/projects/p/foreshadows/discoveries/candidate/accept'))
})

it('rejects a mismatched acknowledgement without removing the candidate', async () => {
  const onAccepted = vi.fn()
  vi.mocked(apiFetch).mockResolvedValueOnce(page()).mockResolvedValueOnce({ id: item.id, status: 'accepted', foreshadow_id: 'wrong' })
  render(<ForeshadowDiscoveryList projectId="p" refreshToken="0" onAccepted={onAccepted} />)
  await screen.findByText(item.evidence)
  fireEvent.click(screen.getByText('核对后登记为伏笔'))
  await screen.findByRole('alert')
  expect(screen.getByText(item.evidence)).toBeTruthy()
  expect(onAccepted).not.toHaveBeenCalled()
})

it('preserves the previous list after a read failure and requires refresh before accepting', async () => {
  vi.mocked(apiFetch).mockResolvedValueOnce(page()).mockRejectedValueOnce(new Error('offline'))
    .mockResolvedValueOnce(page([{ ...item, source_state: 'changed' }]))
  const view = render(<ForeshadowDiscoveryList projectId="p" refreshToken="0" />)
  await screen.findByText(item.evidence)
  view.rerender(<ForeshadowDiscoveryList projectId="p" refreshToken="1" />)
  await screen.findByRole('alert')
  expect(screen.getByText(item.evidence)).toBeTruthy()
  expect(screen.queryByText(/暂无待核对线索/)).toBeNull()
  expect((screen.getByText('核对后登记为伏笔') as HTMLButtonElement).disabled).toBe(true)
  fireEvent.click(screen.getByText('刷新线索'))
  await screen.findByText(/正文或章节位置已变化/)
  expect(screen.queryByRole('alert')).toBeNull()
})

it('shows source conflicts and allows an unwanted candidate to be dismissed', async () => {
  vi.mocked(apiFetch).mockResolvedValueOnce(page()).mockRejectedValueOnce(new ApiError('正文已改变，请重新核查', 409))
    .mockResolvedValueOnce({ id: item.id, status: 'dismissed', foreshadow_id: null }).mockResolvedValue(page([]))
  render(<ForeshadowDiscoveryList projectId="p" refreshToken="0" />)
  await screen.findByText(item.evidence)
  fireEvent.click(screen.getByText('核对后登记为伏笔'))
  await screen.findByText('正文已改变，请重新核查')
  fireEvent.click(screen.getByText('忽略此线索'))
  await screen.findByText(/暂无待核对线索/)
})

it('does not let an old project response replace new evidence', async () => {
  const old = deferred<unknown>()
  vi.mocked(apiFetch).mockImplementationOnce(() => old.promise).mockResolvedValue(page([{ ...item, evidence: '新作品的证据' }]))
  const view = render(<ForeshadowDiscoveryList projectId="old" refreshToken="0" />)
  view.rerender(<ForeshadowDiscoveryList projectId="new" refreshToken="0" />)
  await screen.findByText('新作品的证据')
  await act(async () => { old.resolve(page()) })
  expect(screen.queryByText(item.evidence)).toBeNull()
})

it('does not refresh a new project when an old acceptance arrives late', async () => {
  const old = deferred<unknown>(); const onAccepted = vi.fn()
  vi.mocked(apiFetch).mockResolvedValueOnce(page()).mockImplementationOnce(() => old.promise)
    .mockResolvedValue(page([{ ...item, id: 'new', evidence: '新作品的证据' }]))
  const view = render(<ForeshadowDiscoveryList projectId="old" refreshToken="0" onAccepted={onAccepted} />)
  await screen.findByText(item.evidence)
  fireEvent.click(screen.getByText('核对后登记为伏笔'))
  view.rerender(<ForeshadowDiscoveryList projectId="new" refreshToken="0" onAccepted={onAccepted} />)
  await screen.findByText('新作品的证据')
  await act(async () => { old.resolve({ id: item.id, status: 'accepted', foreshadow_id: item.id }) })
  expect(onAccepted).not.toHaveBeenCalled()
  expect(screen.getByText('新作品的证据')).toBeTruthy()
})

it('paginates and preserves decision history after the registered clue is deleted', async () => {
  const history = { ...item, status: 'accepted', record_present: false, handled_by: '作者', handled_at: '2026-10-08T12:00:00Z' }
  vi.mocked(apiFetch).mockResolvedValueOnce(page([item], true)).mockResolvedValueOnce(page([{ ...item, evidence: '第二页原句' }]))
    .mockResolvedValueOnce({ discoveries: [history], has_more: false })
  render(<ForeshadowDiscoveryList projectId="p" refreshToken="0" />)
  await screen.findByText(item.evidence)
  fireEvent.click(screen.getByText('下一页线索'))
  await screen.findByText('第二页原句')
  expect(vi.mocked(apiFetch).mock.calls.at(-1)?.[0]).toContain('offset=20')
  fireEvent.change(screen.getByLabelText('线索处理状态'), { target: { value: 'accepted' } })
  await screen.findByText(/登记记录已不可用/)
  expect(screen.getByText(/处理账号：作者/)).toBeTruthy()
  expect(screen.queryByText('核对后登记为伏笔')).toBeNull()
  expect(vi.mocked(apiFetch).mock.calls.at(-1)?.[0]).toContain('offset=0')
})

it('distinguishes real empty discovery from older payoff-only jobs', async () => {
  vi.mocked(apiFetch).mockResolvedValue({ jobs: [
    { id: 'empty', chapter_title: '已核查的正文', status: 'completed', attempts: 1, discovery_count: 0 },
    { id: 'legacy', chapter_title: '旧任务正文', status: 'completed', attempts: 1, discovery_count: null },
  ], has_more: false })
  render(<ForeshadowReviewJobs projectId="p" refreshVersion={0} onCompleted={vi.fn()} />)
  await screen.findByText('本稿识别 0 条线索候选（含已处理）')
  expect(screen.getByText('旧任务仅含兑现核查')).toBeTruthy()
})

it('loads candidate evidence through the existing chapter review panel', async () => {
  vi.mocked(apiFetch).mockImplementation(async url => {
    if (url.includes('/review-jobs')) return { jobs: [{ id: 'job', chapter_title: '信', status: 'completed', discovery_count: 1 }], has_more: false }
    if (url.includes('/discoveries')) return page()
    return { reviews: [], has_more: false }
  })
  render(<ForeshadowReviewList projectId="p" />)
  await screen.findByText(item.evidence)
  expect(screen.getByText('本稿识别 1 条线索候选（含已处理）')).toBeTruthy()
  expect(vi.mocked(apiFetch).mock.calls.some(([url]) => url.includes('/discoveries'))).toBe(true)
})
