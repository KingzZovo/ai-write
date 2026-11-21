import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { ForeshadowReviewJobs } from '@/components/panels/ForeshadowReviewJobs'
import { ForeshadowDiscoveryList } from '@/components/panels/ForeshadowDiscoveryList'
import { apiFetch, ApiError } from '@/lib/api'
import { clearForeshadowDraft, draftKey, foreshadowAttempt, storeForeshadowDraft } from '@/lib/foreshadowWrites'
import { reviewAttempt, acknowledgeReviewAttempt } from '@/lib/foreshadowReviewRequests'

vi.mock('@/lib/api', async importOriginal => ({ ...await importOriginal<typeof import('@/lib/api')>(), apiFetch: vi.fn() }))
afterEach(() => {
  cleanup(); vi.resetAllMocks(); vi.restoreAllMocks()
  for (const project of ['rescan-project', 'other-project']) for (const mode of ['normal', 'rescan']) {
    clearForeshadowDraft(draftKey(project, `review:c:${mode}`))
  }
})

const jobs = { jobs: [], has_more: false }
const props = { projectId: 'rescan-project', chapterId: 'c', editorContent: ' 保存的稿件。\n', refreshVersion: 0, onCompleted: vi.fn() }
function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (error: Error) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
function submissions() {
  return vi.mocked(apiFetch).mock.calls.filter(([, options]) => options?.method === 'POST')
    .map(([, options]) => JSON.parse(String(options?.body)))
}
function ack(body: ReturnType<typeof submissions>[number], status = 'pending') {
  return { id: 'job', chapter_id: body.chapter_id, request_id: body.request_id, rescan_discovery: body.rescan_discovery, status }
}

it('an old stored value cannot replace the current request when session storage is full', () => {
  const key = draftKey('rescan-project', 'review:c:rescan')
  storeForeshadowDraft(key, { id: 'old', signature: 'previous manuscript' })
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('quota exceeded') })
  const created = foreshadowAttempt(key, 'current manuscript')
  expect(foreshadowAttempt(key, 'current manuscript')).toEqual(created)
  expect(created.id).not.toBe('old')
})

it('retains the unknown original request when an edited draft is refused and the author restores the old draft', async () => {
  let count = 0
  vi.mocked(apiFetch).mockImplementation(async (_url, options) => {
    if (options?.method !== 'POST') return jobs
    count += 1
    if (count === 1) throw new Error('old response lost')
    if (count === 2) throw new ApiError('新稿尚未保存', 409)
    return ack(JSON.parse(String(options.body)), 'completed')
  })
  const view = render(<ForeshadowReviewJobs {...props} />)
  await screen.findByText('暂无核查任务。')
  fireEvent.click(screen.getByText('重新识别线索'))
  await screen.findByRole('alert')
  view.rerender(<ForeshadowReviewJobs {...props} editorContent="尚未保存的新稿" />)
  fireEvent.click(screen.getByText('重新识别线索'))
  await screen.findByText('新稿尚未保存')
  view.rerender(<ForeshadowReviewJobs {...props} />)
  fireEvent.click(screen.getByText('重新识别线索'))
  await screen.findByText(/该请求的核查已结束/)
  expect(submissions()[2]).toEqual(submissions()[0])
  expect(submissions()[1].request_id).not.toBe(submissions()[0].request_id)
  view.rerender(<ForeshadowReviewJobs {...props} editorContent="尚未保存的新稿" />)
  fireEvent.click(screen.getByText('重新识别线索'))
  await screen.findByText(/该请求的核查已结束/)
  expect(submissions()[3].request_id).not.toBe(submissions()[1].request_id)
})

it('acknowledges only one pending manuscript and survives failed storage removal', () => {
  const key = draftKey('rescan-project', 'review:c:rescan')
  const first = reviewAttempt(key, '稿件一')
  const second = reviewAttempt(key, '稿件二')
  acknowledgeReviewAttempt(key, first)
  expect(reviewAttempt(key, '稿件二')).toEqual(second)
  vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(() => { throw new Error('storage disabled') })
  acknowledgeReviewAttempt(key, second)
  expect(reviewAttempt(key, '稿件二').id).not.toBe(second.id)
})

it('explicit rescan sends one fixed intent with exact prose for repeated clicks', async () => {
  const response = deferred<unknown>()
  vi.mocked(apiFetch).mockImplementation(async (_url, options) => options?.method === 'POST' ? response.promise : jobs)
  render(<ForeshadowReviewJobs {...props} />)
  await screen.findByText('暂无核查任务。')
  fireEvent.click(screen.getByText('重新识别线索')); fireEvent.click(screen.getByText('重新识别线索'))
  const sent = submissions()
  expect(sent).toHaveLength(1)
  expect(sent[0]).toEqual({ chapter_id: 'c', expected_content: props.editorContent, rescan_discovery: true, request_id: expect.any(String) })
  expect(sent[0].request_id).toMatch(/^[0-9a-f-]{36}$/)
  expect(screen.queryByText(/已提交重新识别/)).toBeNull()
  response.resolve(ack(sent[0]))
  await screen.findByText(/已提交重新识别/)
})

it('keeps request identity across response loss and component remount, then permits an explicit new scan', async () => {
  let count = 0
  vi.mocked(apiFetch).mockImplementation(async (_url, options) => {
    if (options?.method !== 'POST') return jobs
    if (++count === 1) throw new Error('response lost')
    return ack(JSON.parse(String(options.body)), 'completed')
  })
  const view = render(<ForeshadowReviewJobs {...props} />)
  await screen.findByText('暂无核查任务。')
  fireEvent.click(screen.getByText('重新识别线索'))
  await screen.findByText(/已保留原请求/)
  view.unmount()
  render(<ForeshadowReviewJobs {...props} />)
  await screen.findByText('暂无核查任务。')
  fireEvent.click(screen.getByText('重新识别线索'))
  await screen.findByText(/该请求的核查已结束/)
  expect(submissions()[1]).toEqual(submissions()[0])
  fireEvent.click(screen.getByText('重新识别线索'))
  await waitFor(() => expect(submissions()).toHaveLength(3))
  expect(submissions()[2].request_id).not.toBe(submissions()[0].request_id)
})

it('does not accept a response for another request and retries the original identity', async () => {
  let first = true
  vi.mocked(apiFetch).mockImplementation(async (_url, options) => {
    if (options?.method !== 'POST') return jobs
    const value = ack(JSON.parse(String(options.body)))
    if (first) { first = false; return { ...value, request_id: 'another' } }
    return value
  })
  render(<ForeshadowReviewJobs {...props} />)
  await screen.findByText('暂无核查任务。')
  fireEvent.click(screen.getByText('重新识别线索'))
  await screen.findByRole('alert')
  expect(screen.queryByText(/已提交重新识别/)).toBeNull()
  fireEvent.click(screen.getByText('重新识别线索'))
  await screen.findByText(/已提交重新识别/)
  expect(submissions()[1]).toEqual(submissions()[0])
})

it('a late response for old prose does not erase the new pending request or show a stale success', async () => {
  const old = deferred<unknown>(); const next = deferred<unknown>(); let count = 0
  vi.mocked(apiFetch).mockImplementation(async (_url, options) => {
    if (options?.method !== 'POST') return jobs
    count += 1
    return count === 1 ? old.promise : count === 2 ? next.promise : ack(JSON.parse(String(options.body)))
  })
  const view = render(<ForeshadowReviewJobs {...props} />)
  await screen.findByText('暂无核查任务。')
  fireEvent.click(screen.getByText('重新识别线索'))
  view.rerender(<ForeshadowReviewJobs {...props} editorContent="作者的新稿。" />)
  fireEvent.click(screen.getByText('重新识别线索'))
  await act(async () => { old.resolve(ack(submissions()[0])) })
  expect(screen.queryByText(/已提交重新识别/)).toBeNull()
  await act(async () => { next.reject(new Error('unknown outcome')) })
  await screen.findByRole('alert')
  fireEvent.click(screen.getByText('重新识别线索'))
  await screen.findByText(/已提交重新识别/)
  expect(submissions()[0].request_id).not.toBe(submissions()[1].request_id)
  expect(submissions()[2]).toEqual(submissions()[1])
})

it('replays a removed original job as unavailable instead of claiming that a new scan started', async () => {
  vi.mocked(apiFetch).mockImplementation(async (_url, options) => options?.method === 'POST'
    ? ack(JSON.parse(String(options.body)), 'unavailable') : jobs)
  render(<ForeshadowReviewJobs {...props} />)
  await screen.findByText('暂无核查任务。')
  fireEvent.click(screen.getByText('重新识别线索'))
  await screen.findByText(/原章节或任务现已不可用/)
  expect(screen.queryByText(/已提交重新识别/)).toBeNull()
})

it('late retry failure from another project cannot change the current task panel', async () => {
  const old = deferred<unknown>()
  vi.mocked(apiFetch).mockImplementation(async (url, options) => {
    if (options?.method === 'POST') return old.promise
    return url.includes('other-project') ? jobs : { jobs: [{ id: 'job', status: 'failed', chapter_title: '旧作品', attempts: 3 }], has_more: false }
  })
  const view = render(<ForeshadowReviewJobs {...props} />)
  await screen.findByText('核查未完成')
  fireEvent.click(screen.getByText('重试核查'))
  view.rerender(<ForeshadowReviewJobs {...props} projectId="other-project" />)
  await screen.findByText('暂无核查任务。')
  await act(async () => { old.reject(new Error('old failure')) })
  expect(screen.queryByRole('alert')).toBeNull()
  expect(screen.queryByText('旧作品')).toBeNull()
})

const candidate = { id: 'candidate', description: '陈伯为什么保留这张存根？', evidence: '陈伯收起存根。',
  reason: '最初的说明', status: 'pending', source_state: 'current', source_snapshot: { global_idx: 913 } }
const run = { id: 'run', chapter_id: 'c', chapter_title: '存根', created_at: '2026-10-08T01:02:03Z', candidate_count: 1,
  result_state: 'complete', scan_revision: 100, source_state: 'current', source_snapshot: { global_idx: 913 }, model_name: 'gemini-3.1-pro' }
const page = (items: unknown[] = [candidate]) => ({ discoveries: items, has_more: false })

it('loads history on demand and shows this scan explanation separately from the original decision', async () => {
  vi.mocked(apiFetch).mockImplementation(async url => {
    if (url.includes('/discovery-runs')) return { runs: [run], has_more: false }
    if (url.includes('run_id=run')) return page([{ ...candidate, status: 'accepted', scan_reason: '本轮的新说明',
      reused_in_scan: true, handled_by: '原作者', handled_at: '2026-10-08T00:00:00Z' }])
    return page()
  })
  render(<ForeshadowDiscoveryList projectId="rescan-project" chapterId="c" refreshToken="0" />)
  await screen.findByText(candidate.evidence)
  expect(vi.mocked(apiFetch).mock.calls.some(([url]) => url.includes('/discovery-runs'))).toBe(false)
  fireEvent.click(screen.getByText('查看识别历史'))
  await screen.findByText(/调用模型名称：gemini-3.1-pro/)
  expect(vi.mocked(apiFetch).mock.calls.some(([url]) => url.endsWith('offset=0&chapter_id=c'))).toBe(true)
  fireEvent.click(screen.getByText('查看此轮线索'))
  await screen.findByText('本轮说明：本轮的新说明')
  expect(screen.getByText('首次识别说明：最初的说明')).toBeTruthy()
  expect(screen.getByText(/本轮说明未单独确认/)).toBeTruthy()
  expect(screen.getByText('处理状态：已登记')).toBeTruthy()
  expect(screen.queryByText('核对后登记为伏笔')).toBeNull()
  expect(vi.mocked(apiFetch).mock.calls.some(([url]) => url.includes('status=all') && url.endsWith('run_id=run'))).toBe(true)
})

it('an empty successful scan does not hide the old candidates when returning to the full list', async () => {
  vi.mocked(apiFetch).mockImplementation(async url => {
    if (url.includes('/discovery-runs')) return { runs: [{ ...run, candidate_count: 0 }], has_more: false }
    return url.includes('run_id=run') ? page([]) : page()
  })
  render(<ForeshadowDiscoveryList projectId="rescan-project" refreshToken="0" />)
  await screen.findByText(candidate.evidence)
  fireEvent.click(screen.getByText('查看识别历史'))
  await screen.findByText('本轮未发现线索候选')
  fireEvent.click(screen.getByText('查看此轮线索'))
  await screen.findByText(/本轮已完成识别，未发现线索候选/)
  expect(screen.queryByText(candidate.evidence)).toBeNull()
  fireEvent.click(screen.getByText('返回全部线索'))
  await screen.findByText(candidate.evidence)
})

it('incomplete history is never shown as a successful empty scan', async () => {
  vi.mocked(apiFetch).mockImplementation(async url => url.includes('/discovery-runs')
    ? { runs: [{ ...run, result_state: 'unknown', candidate_count: null }], has_more: false } : page([]))
  render(<ForeshadowDiscoveryList projectId="rescan-project" refreshToken="0" />)
  fireEvent.click(screen.getByText('查看识别历史'))
  await screen.findByText('本轮清单暂无法完整核验')
  fireEvent.click(screen.getByText('查看此轮线索'))
  await screen.findByText('本轮清单无法完整核验，不能判断是否没有线索。')
  expect(screen.queryByText('本轮未发现线索候选')).toBeNull()
})

it('retains current evidence when the history request fails and supports retry', async () => {
  let failed = true
  vi.mocked(apiFetch).mockImplementation(async url => {
    if (!url.includes('/discovery-runs')) return page()
    if (failed) throw new Error('offline')
    return { runs: [run], has_more: false }
  })
  render(<ForeshadowDiscoveryList projectId="rescan-project" refreshToken="0" />)
  await screen.findByText(candidate.evidence)
  fireEvent.click(screen.getByText('查看识别历史'))
  await screen.findByText('识别历史读取失败，请刷新历史。')
  expect(screen.getByText(candidate.evidence)).toBeTruthy()
  failed = false; fireEvent.click(screen.getByText('刷新历史'))
  await screen.findByText('查看此轮线索')
})

it('late results from another selected scan cannot replace the current scan list', async () => {
  const old = deferred<unknown>()
  vi.mocked(apiFetch).mockImplementation(async url => {
    if (url.includes('/discovery-runs')) return { runs: [run, { ...run, id: 'new' }], has_more: false }
    if (url.includes('run_id=run')) return old.promise
    if (url.includes('run_id=new')) return page([{ ...candidate, evidence: '新一轮的原句' }])
    return page()
  })
  render(<ForeshadowDiscoveryList projectId="rescan-project" refreshToken="0" />)
  await screen.findByText(candidate.evidence)
  fireEvent.click(screen.getByText('查看识别历史'))
  await screen.findAllByText('查看此轮线索')
  const history = within(screen.getByLabelText('线索识别历史'))
  fireEvent.click(history.getAllByText('查看此轮线索')[0])
  fireEvent.click(history.getByText('查看此轮线索'))
  await screen.findByText('新一轮的原句')
  await act(async () => { old.resolve(page()) })
  expect(screen.queryByText(candidate.evidence)).toBeNull()
})
