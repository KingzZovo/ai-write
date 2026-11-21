import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { CognitionPanel } from '@/components/panels/CognitionPanel'
import { apiFetch, ApiError } from '@/lib/api'
import { pendingCognitionRequests } from '@/lib/cognitionRequests'

vi.mock('@/lib/api', async (original) => ({ ...await original<typeof import('@/lib/api')>(), apiFetch: vi.fn() }))
afterEach(() => { cleanup(); vi.resetAllMocks(); vi.useRealTimers(); sessionStorage.clear() })
const cid = '00000000-0000-4000-8000-000000000001'
const chapter = { chapter_id: cid, chapter_title: '迟来的信', global_idx: 1, status: 'failed',
  version: 'b'.repeat(64), source_hash: 'a'.repeat(64), attempts: 4, changes_count: null as number | null,
  can_retry: true, message: '本轮提取未完成。可重试当前保存稿。' }
let number = 0
const project = () => `cognition-panel-${++number}`
const listing = (row = chapter) => ({ chapters: [row], total: 1, has_more: false })
const ack = (body: { request_id: string; source_hash: string }) => ({ request_id: body.request_id,
  operation: 'retry', outcome: 'scheduled', source_hash: body.source_hash, chapter_id: cid, record_id: cid, cycle_id: cid })

it('keeps failure visible until the exact request is acknowledged', async () => {
  const pid = project()
  let row = chapter
  let finish!: (value: unknown) => void
  let submitted!: { request_id: string; source_hash: string }
  vi.mocked(apiFetch).mockImplementation(async (path, options) => {
    if (options?.method === 'POST') {
      submitted = JSON.parse(String(options.body))
      return new Promise(resolve => { finish = resolve })
    }
    return path.endsWith('/backfills') ? { backfills: [] } : listing(row)
  })
  render(<CognitionPanel projectId={pid} />)
  await screen.findByText('提取未完成')
  fireEvent.click(screen.getByText('重试本章提取'))
  expect(screen.getByText('提取未完成')).toBeTruthy()
  expect(screen.queryByText(/已确认这次提交/)).toBeNull()
  row = { ...chapter, status: 'pending', can_retry: false, attempts: 0 }
  await act(async () => { finish(ack(submitted)) })
  await screen.findByText('等待提取')
  expect(pendingCognitionRequests(pid)).toEqual([])
})

it('replays the original request after an unknown response and later source refresh', async () => {
  const pid = project()
  let row = chapter
  const bodies: { request_id: string; source_hash: string; version: string }[] = []
  vi.mocked(apiFetch).mockImplementation(async (path, options) => {
    if (options?.method === 'POST') {
      const body = JSON.parse(String(options.body)); bodies.push(body)
      if (bodies.length === 1) throw new Error('response lost')
      return ack(body)
    }
    return path.endsWith('/backfills') ? { backfills: [] } : listing(row)
  })
  render(<CognitionPanel projectId={pid} />)
  await screen.findByText('重试本章提取')
  fireEvent.click(screen.getByText('重试本章提取'))
  await screen.findByRole('alert')
  row = { ...chapter, version: 'c'.repeat(64), source_hash: 'd'.repeat(64) }
  fireEvent.click(screen.getByText('刷新认知进度'))
  await screen.findByText('提取未完成')
  fireEvent.click(screen.getByText('确认上次提交'))
  await screen.findByText(/已确认这次提交/)
  expect(bodies[1]).toEqual(bodies[0])
})

it('rejects a mismatched acknowledgement and retains the original request', async () => {
  const pid = project()
  vi.mocked(apiFetch).mockImplementation(async (path, options) => options?.method === 'POST'
    ? { ...ack(JSON.parse(String(options.body))), chapter_id: 'another-chapter' }
    : path.endsWith('/backfills') ? { backfills: [] } : listing())
  render(<CognitionPanel projectId={pid} />)
  await screen.findByText('重试本章提取')
  fireEvent.click(screen.getByText('重试本章提取'))
  await screen.findByRole('alert')
  expect(pendingCognitionRequests(pid)).toHaveLength(1)
  expect(screen.queryByText(/已确认这次提交/)).toBeNull()
})

it('clears definitively refused requests and refreshes instead of claiming acceptance', async () => {
  const pid = project()
  vi.mocked(apiFetch).mockImplementation(async (path, options) => {
    if (options?.method === 'POST') throw new ApiError('source changed', 409)
    return path.endsWith('/backfills') ? { backfills: [] } : listing()
  })
  render(<CognitionPanel projectId={pid} />)
  await screen.findByText('重试本章提取')
  fireEvent.click(screen.getByText('重试本章提取'))
  await screen.findByText(/保存稿、章序或提取状态已变化/)
  expect(pendingCognitionRequests(pid)).toEqual([])
  expect(screen.queryByText(/已确认这次提交/)).toBeNull()
})

it('ignores a late submit response after switching projects', async () => {
  const pid = project(), next = project()
  let finish!: (value: unknown) => void
  let body!: { request_id: string; source_hash: string }
  vi.mocked(apiFetch).mockImplementation(async (path, options) => {
    if (options?.method === 'POST') {
      body = JSON.parse(String(options.body))
      return new Promise(resolve => { finish = resolve })
    }
    return path.endsWith('/backfills') ? { backfills: [] }
      : path.includes(next) ? { chapters: [], total: 0, has_more: false } : listing()
  })
  const { rerender } = render(<CognitionPanel projectId={pid} />)
  await screen.findByText('重试本章提取')
  fireEvent.click(screen.getByText('重试本章提取'))
  rerender(<CognitionPanel projectId={next} />)
  await screen.findByText('本页暂无章节。')
  await act(async () => { finish(ack(body)) })
  expect(screen.queryByText(/已确认这次提交/)).toBeNull()
  expect(screen.queryByText('等待提取')).toBeNull()
})

it('shows complete evidence and distinguishes a completed empty result', async () => {
  let row = { ...chapter, status: 'completed', can_retry: false, changes_count: 1 }
  const evidence = '陈川不知道信已送到。\n' + '沈青把回执交给他，他才明白父亲已经收信。'.repeat(40)
  let empty = false
  vi.mocked(apiFetch).mockImplementation(async path => path.endsWith('/backfills') ? { backfills: [] }
    : path.endsWith(`/chapters/${cid}`) ? { ...row, changes: empty ? [] : [
      { character: '陈川', learns: '父亲已经收信。', evidence },
    ] } : listing(row))
  render(<CognitionPanel projectId={project()} />)
  await screen.findByText('查看认知与原文依据')
  fireEvent.click(screen.getByText('查看认知与原文依据'))
  await screen.findByText(/以下为本章当时的知情记录/)
  expect(document.querySelector('blockquote')?.textContent).toBe(evidence)
  fireEvent.click(screen.getByText('收起原文依据'))
  empty = true
  row = { ...row, version: 'c'.repeat(64), changes_count: 0 }
  fireEvent.click(screen.getByText('刷新认知进度'))
  await screen.findByText('查看认知与原文依据')
  fireEvent.click(screen.getByText('查看认知与原文依据'))
  await screen.findByText(/这不表示人物什么都不知道/)
})

it('does not show evidence belonging to a changed saved-source version', async () => {
  const row = { ...chapter, status: 'completed', can_retry: false, changes_count: 0 }
  vi.mocked(apiFetch).mockImplementation(async path => path.endsWith('/backfills') ? { backfills: [] }
    : path.endsWith(`/chapters/${cid}`) ? { ...row, version: 'c'.repeat(64), changes: [] } : listing(row))
  render(<CognitionPanel projectId={project()} />)
  await screen.findByText('查看认知与原文依据')
  fireEvent.click(screen.getByText('查看认知与原文依据'))
  await screen.findByText(/保存稿或结果已变化/)
  expect(screen.queryByText(/这不表示人物什么都不知道/)).toBeNull()
})

it('polls active work, stops after completion and distinguishes scheduling from extraction', async () => {
  vi.useFakeTimers()
  let done = false
  vi.mocked(apiFetch).mockImplementation(async path => path.endsWith('/backfills') ? { backfills: [{
    id: cid, status: 'scheduled', chapter_count: 1005, cursor: 1005, added_count: 1005,
    existing_count: 0, attention_count: 0, unavailable_count: 0,
  }] } : listing({ ...chapter, status: done ? 'completed' : 'running', can_retry: false }))
  let unmount!: () => void
  await act(async () => { unmount = render(<CognitionPanel projectId={project()} />).unmount })
  expect(screen.getByText(/安排已结束（不代表提取完成）/)).toBeTruthy()
  expect(screen.getByText('正在提取')).toBeTruthy()
  done = true
  await act(async () => { await vi.advanceTimersByTimeAsync(5000) })
  expect(screen.getByText('提取已结束')).toBeTruthy()
  const count = vi.mocked(apiFetch).mock.calls.length
  await act(async () => { await vi.advanceTimersByTimeAsync(10000) })
  expect(apiFetch).toHaveBeenCalledTimes(count)
  unmount()
  await act(async () => { await vi.advanceTimersByTimeAsync(10000) })
  expect(apiFetch).toHaveBeenCalledTimes(count)
})
