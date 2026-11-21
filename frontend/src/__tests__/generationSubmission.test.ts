import { afterEach, expect, it, vi } from 'vitest'
import { ApiError } from '@/lib/api'
import { GenerationSubmissionSession, type GenerationInput } from '@/lib/generationSubmission'

const id = '9ca2b64b-6258-4b51-bb0c-e9a7c1d1f001'
const nextId = '9ca2b64b-6258-4b51-bb0c-e9a7c1d1f002'
const input = (): GenerationInput => ({ project_id: 'book', task_type: 'chapter', chapter_id: 'chapter',
  expected_content_text: '作者原稿。', user_input: '交清两千元。', params: { target_words: 2200 } })
function receipt(body: string, changes: Record<string, unknown> = {}) {
  const value = JSON.parse(body)
  return { task_id: value.request_id, project_id: value.project_id, task_type: value.task_type,
    chapter_id: value.chapter_id ?? null, volume_idx: value.volume_idx ?? null, status: 'pending', ...changes }
}
function setup() {
  const data = new Map<string, string>()
  const storage = { getItem: vi.fn((key: string) => data.get(key) ?? null),
    setItem: vi.fn((key: string, value: string) => { data.set(key, value) }),
    removeItem: vi.fn((key: string) => { data.delete(key) }) }
  const send = vi.fn(async (body: string, _signal: AbortSignal): Promise<unknown> => receipt(body))
  const dependencies = { send, storage: () => storage, id: vi.fn(() => id) }
  const session = new GenerationSubmissionSession('book', dependencies)
  return { data, storage, send, dependencies, session }
}
afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers() })

it('persists the complete original request before sending and checks a fixed receipt', async () => {
  const r = setup()
  r.send.mockImplementation(async (body) => {
    expect([...r.data.values()]).toHaveLength(1)
    expect(JSON.parse([...r.data.values()][0]).pending.body).toBe(body)
    return receipt(body)
  })
  expect(await r.session.submit(input())).toMatchObject({ task_id: id, chapter_id: 'chapter' })
  expect(r.session.getSnapshot().pending).toBeNull()
  expect(r.data.size).toBe(0)
})

it('keeps the same UUID, chapter and full source after a lost response and later author edits', async () => {
  const r = setup()
  r.send.mockRejectedValueOnce(new Error('response lost'))
  const request = input()
  await expect(r.session.submit(request)).rejects.toThrow('response lost')
  const body = r.send.mock.calls[0][0]
  request.expected_content_text = '作者后来的稿件。'
  request.chapter_id = 'another'
  request.params!.target_words = 5000
  await expect(r.session.submit(request)).rejects.toThrow('已有')
  await r.session.retry()
  expect(r.send).toHaveBeenCalledTimes(2)
  expect(r.send.mock.calls[1][0]).toBe(body)
  expect(JSON.parse(body)).toMatchObject({ chapter_id: 'chapter', expected_content_text: '作者原稿。', params: { target_words: 2200 }, request_id: id })
})

it('restores an unconfirmed request and retries its exact body after a page reload', async () => {
  const r = setup()
  r.send.mockRejectedValueOnce(new Error('offline'))
  await expect(r.session.submit(input())).rejects.toThrow()
  const next = new GenerationSubmissionSession('book', r.dependencies)
  next.attach()
  expect(next.getSnapshot().pending?.id).toBe(id)
  await next.retry()
  expect(r.send.mock.calls[0][0]).toBe(r.send.mock.calls[1][0])
  expect(r.dependencies.id).toHaveBeenCalledTimes(1)
})

it.each([{ task_id: nextId }, { project_id: 'other' }, { chapter_id: 'other' }, { task_type: 'outline_book' },
  { volume_idx: 2 }, { status: '' }])('retains the request when acknowledgement identity is wrong: %j', async (changes) => {
  const r = setup()
  r.send.mockImplementation(async (body) => receipt(body, changes))
  await expect(r.session.submit(input())).rejects.toThrow('不一致')
  expect(r.session.getSnapshot().pending).toMatchObject({ id, rejected: false, sent: true })
  r.session.dismissRejected()
  expect(r.session.getSnapshot().pending?.id).toBe(id)
})

it('permits explicit clearing after a known source rejection, followed by a new reviewed request', async () => {
  const r = setup()
  r.send.mockRejectedValueOnce(new ApiError('原稿变化', 409, 'chapter_edit_conflict'))
  await expect(r.session.submit(input())).rejects.toThrow('原稿变化')
  expect(r.session.getSnapshot().pending?.rejected).toBe(true)
  r.session.dismissRejected()
  r.dependencies.id.mockReturnValue(nextId)
  await r.session.submit({ ...input(), expected_content_text: '核对后的新稿。' })
  expect(JSON.parse(r.send.mock.calls[1][0])).toMatchObject({ request_id: nextId, expected_content_text: '核对后的新稿。' })
})

it('does not permit clearing an unknown duplicate-ID conflict that may refer to an existing task', async () => {
  const r = setup()
  r.send.mockRejectedValueOnce(new ApiError('编号冲突', 409, 'generation_request_conflict'))
  await expect(r.session.submit(input())).rejects.toThrow()
  r.session.dismissRejected()
  expect(r.session.getSnapshot().pending).toMatchObject({ id, rejected: false })
})

it('does not send a new request if the browser cannot preserve it', async () => {
  const r = setup()
  r.storage.setItem.mockImplementation(() => { throw new Error('quota') })
  await expect(r.session.submit(input())).rejects.toThrow('尚未发送')
  r.send.mock.calls.forEach(() => { throw new Error('must not publish') })
  expect(r.session.getSnapshot().pending).toMatchObject({ sent: false, rejected: true })
})

it('does not mistake a failed retry checkpoint for proof that the original request was never sent', async () => {
  const r = setup()
  r.send.mockRejectedValueOnce(new Error('lost acknowledgement'))
  await expect(r.session.submit(input())).rejects.toThrow()
  r.storage.setItem.mockImplementation(() => { throw new Error('quota') })
  await expect(r.session.retry()).rejects.toThrow('先前提交的结果仍未确认')
  r.session.dismissRejected()
  expect(r.session.getSnapshot().pending).toMatchObject({ id, sent: true, rejected: false })
  expect(r.send).toHaveBeenCalledTimes(1)
})

it('keeps a confirmed receipt when removing stale browser state fails', async () => {
  const r = setup()
  r.storage.removeItem.mockImplementation(() => { throw new Error('storage unavailable') })
  await r.session.submit(input())
  expect(r.session.getSnapshot()).toMatchObject({ pending: null, receipt: { task_id: id }, storageError: expect.any(String) })
  await expect(r.session.submit(input())).rejects.toThrow('暂存')
  expect(r.send).toHaveBeenCalledTimes(1)
})

it('retains corrupt browser records and prevents a fresh request from overwriting them', async () => {
  const r = setup()
  r.data.set('aiwrite:generation-request:v1:book', 'broken record')
  await expect(r.session.submit(input())).rejects.toThrow('读取')
  expect(r.data.get('aiwrite:generation-request:v1:book')).toBe('broken record')
  expect(r.send).not.toHaveBeenCalled()
})

it('deduplicates repeated clicks during the first network request', async () => {
  const r = setup()
  let finish!: (value: unknown) => void
  r.send.mockImplementation(() => new Promise((resolve) => { finish = resolve }))
  const first = r.session.submit(input())
  await expect(r.session.submit(input())).rejects.toThrow('已有')
  await expect(r.session.retry()).rejects.toThrow('正在提交')
  finish(receipt(r.send.mock.calls[0][0]))
  await first
  expect(r.send).toHaveBeenCalledTimes(1)
})

it('never lets a late acknowledgement clear another projects pending request', async () => {
  const r = setup()
  let finish!: (value: unknown) => void
  r.send.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
  const first = r.session.submit(input())
  const other = new GenerationSubmissionSession('other', { ...r.dependencies, id: () => nextId })
  r.send.mockRejectedValueOnce(new Error('other response lost'))
  await expect(other.submit({ ...input(), project_id: 'other' })).rejects.toThrow()
  finish(receipt(r.send.mock.calls[0][0]))
  await first
  expect(other.getSnapshot().pending?.id).toBe(nextId)
})

it('times out the local wait while preserving the server request identity', async () => {
  vi.useFakeTimers()
  const r = setup()
  r.send.mockImplementation((_body, signal) => new Promise((_resolve, reject) => {
    signal.addEventListener('abort', () => reject(new Error('local wait timed out')), { once: true })
  }))
  const result = expect(r.session.submit(input())).rejects.toThrow('timed out')
  await vi.advanceTimersByTimeAsync(45_000)
  await result
  expect(r.session.getSnapshot()).toMatchObject({ busy: false, pending: { id, sent: true, rejected: false } })
})

it('can persist a book or volume request without inventing a manuscript source', async () => {
  const r = setup()
  await r.session.submit({ project_id: 'book', task_type: 'outline_volume', volume_idx: 2, user_input: '第二卷' })
  const sent = JSON.parse(r.send.mock.calls[0][0])
  expect(sent).toMatchObject({ request_id: id, volume_idx: 2 })
  expect(sent.expected_content_text).toBeUndefined()
})
