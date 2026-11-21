import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { ForeshadowReviewJobs } from '@/components/panels/ForeshadowReviewJobs'
import { apiFetch } from '@/lib/api'
vi.mock('@/lib/api', async (importOriginal) => ({ ...await importOriginal<typeof import('@/lib/api')>(), apiFetch: vi.fn() }))
afterEach(() => { cleanup(); vi.resetAllMocks(); vi.useRealTimers() })
const job = { id: 'j', chapter_title: '寄信人', status: 'failed', attempts: 3 }

it('keeps failure visible until retry is acknowledged and refetches task state', async () => {
  let resolve!: (value: unknown) => void
  vi.mocked(apiFetch).mockResolvedValueOnce({ jobs: [job], has_more: false })
    .mockImplementationOnce(() => new Promise(r => { resolve = r }))
    .mockResolvedValueOnce({ jobs: [{ ...job, status: 'pending' }], has_more: false })
  render(<ForeshadowReviewJobs projectId="p" refreshVersion={0} onCompleted={vi.fn()} />)
  await screen.findByText('核查未完成')
  fireEvent.click(screen.getByText('重试核查'))
  expect(screen.getByText('核查未完成')).toBeTruthy()
  resolve({ id: 'j', status: 'pending' })
  await screen.findByText('等待核查')
  expect(vi.mocked(apiFetch).mock.calls[1]?.[0]).toContain('/j/retry')
})

it('keeps failed job visible after a retry conflict', async () => {
  vi.mocked(apiFetch).mockResolvedValueOnce({ jobs: [job], has_more: false }).mockRejectedValueOnce(new Error('409'))
  render(<ForeshadowReviewJobs projectId="p" refreshVersion={0} onCompleted={vi.fn()} />)
  await screen.findByText('核查未完成')
  fireEvent.click(screen.getByText('重试核查'))
  await screen.findByRole('alert')
  expect(screen.getByText('核查未完成')).toBeTruthy()
})

it('polls active work and refreshes suggestions when it completes', async () => {
  vi.useFakeTimers()
  const onCompleted = vi.fn()
  vi.mocked(apiFetch).mockResolvedValueOnce({ jobs: [{ ...job, status: 'running' }], has_more: false })
    .mockResolvedValueOnce({ jobs: [{ ...job, status: 'completed' }], has_more: false })
  await act(async () => { render(<ForeshadowReviewJobs projectId="p" refreshVersion={0} onCompleted={onCompleted} />) })
  expect(screen.getByText('正在核查')).toBeTruthy()
  await act(async () => { await vi.advanceTimersByTimeAsync(5000) })
  expect(screen.getByText('核查已结束')).toBeTruthy()
  expect(onCompleted).toHaveBeenCalledOnce()
  await act(async () => { await vi.advanceTimersByTimeAsync(10000) })
  expect(apiFetch).toHaveBeenCalledTimes(2)
})


it('refreshes evidence when a newly observed task has already completed', async () => {
  const onCompleted = vi.fn()
  vi.mocked(apiFetch).mockResolvedValue({ jobs: [{ ...job, status: 'completed' }], has_more: false })
  render(<ForeshadowReviewJobs projectId="p" refreshVersion={0} onCompleted={onCompleted} />)
  await screen.findByText('核查已结束')
  expect(onCompleted).toHaveBeenCalledOnce()
})

it('submits exact editor text and waits for chapter acknowledgement', async () => {
  let resolve!: (value: unknown) => void
  vi.mocked(apiFetch).mockResolvedValueOnce({ jobs: [], has_more: false })
    .mockImplementationOnce(() => new Promise(r => { resolve = r }))
    .mockResolvedValueOnce({ jobs: [{ ...job, status: 'pending' }], has_more: false })
  render(<ForeshadowReviewJobs projectId="p" chapterId="c" editorContent={' 保存的正文。\n'} refreshVersion={0} onCompleted={vi.fn()} />)
  await screen.findByText('暂无核查任务。')
  fireEvent.click(screen.getByText('核查当前已保存章节'))
  expect(screen.queryByText(/已提交当前保存稿/)).toBeNull()
  const body = JSON.parse(String(vi.mocked(apiFetch).mock.calls[1][1]?.body))
  expect(body).toEqual({ chapter_id: 'c', expected_content: ' 保存的正文。\n', rescan_discovery: false, request_id: expect.any(String) })
  resolve({ id: 'j', chapter_id: 'c', status: 'pending', request_id: body.request_id, rescan_discovery: false })
  await screen.findByText(/已提交当前保存稿/)
  await screen.findByText('等待核查')
})

it('does not acknowledge another chapter or stale submission after switching chapter', async () => {
  let resolve!: (value: unknown) => void
  vi.mocked(apiFetch).mockResolvedValueOnce({ jobs: [], has_more: false })
    .mockImplementationOnce(() => new Promise(r => { resolve = r }))
  const { rerender } = render(<ForeshadowReviewJobs projectId="p" chapterId="c" editorContent="旧章" refreshVersion={0} onCompleted={vi.fn()} />)
  await screen.findByText('暂无核查任务。')
  fireEvent.click(screen.getByText('核查当前已保存章节'))
  rerender(<ForeshadowReviewJobs projectId="p" chapterId="d" editorContent="新章" refreshVersion={0} onCompleted={vi.fn()} />)
  await act(async () => { resolve({ id: 'j', chapter_id: 'c', status: 'pending' }) })
  expect(screen.queryByText(/已提交当前保存稿/)).toBeNull()
  expect(apiFetch).toHaveBeenCalledTimes(2)
})

it('shows save conflict and never claims a review was submitted', async () => {
  const { ApiError } = await vi.importActual<typeof import('@/lib/api')>('@/lib/api')
  vi.mocked(apiFetch).mockResolvedValueOnce({ jobs: [], has_more: false })
    .mockRejectedValueOnce(new ApiError('请先保存并重新核对', 409))
  render(<ForeshadowReviewJobs projectId="p" chapterId="c" editorContent="未保存" refreshVersion={0} onCompleted={vi.fn()} />)
  await screen.findByText('暂无核查任务。')
  fireEvent.click(screen.getByText('核查当前已保存章节'))
  expect(await screen.findByRole('alert')).toHaveTextContent('请先保存并重新核对')
  expect(screen.queryByText(/已提交当前保存稿/)).toBeNull()
})
