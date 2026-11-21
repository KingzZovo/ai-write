import { expect, it, vi } from 'vitest'
import { ApiError } from '@/lib/api'
import { ArcProgressSession } from '@/components/arc/arcProgressSession'
import type { ArcProgressView } from '@/lib/arcProgress'
import { arcFixture, arcManuscript } from './fixtures/arcProgress'

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(yes => { resolve = yes })
  return { promise, resolve }
}
async function setup() {
  const fixture = arcFixture(), manuscript = arcManuscript()
  const { project_id: pid, volume_id: vid } = fixture.before.source
  let current = fixture.before
  const values = new Map<string, string>()
  const storage = { getItem: (key: string) => values.get(key) ?? null,
    setItem: vi.fn((key: string, value: string) => { values.set(key, value) }) }
  const read = vi.fn(async (_signal: AbortSignal): Promise<unknown> => structuredClone(current))
  const save = vi.fn(async (_body: string, _signal: AbortSignal): Promise<unknown> => {
    current = fixture.after
    return structuredClone(fixture.receipt)
  })
  const receipt = vi.fn(async (_id: string, _signal: AbortSignal): Promise<unknown> => structuredClone(fixture.detail))
  const deps = { read, save, receipt, storage: () => storage, id: vi.fn(() => fixture.input.request_id) }
  const session = new ArcProgressSession(pid, vid, deps)
  session.attach(); await session.reload()
  session.editNote(manuscript.chapter_id, fixture.input.author_note)
  return { session, fixture, manuscript, pid, vid, deps, read, save, receipt, storage, values,
    setCurrent: (value: ArcProgressView) => { current = value } }
}

it('stores the complete immutable body before POST and retains notes typed during confirmation', async () => {
  const h = await setup(), pending = deferred<unknown>()
  h.save.mockImplementationOnce(async body => {
    const stored = JSON.parse([...h.values.values()][0])
    expect(stored.pending.body).toBe(body)
    expect(JSON.parse(body).expected_content_text).toBe(h.manuscript.content_text)
    return pending.promise
  })
  const operation = h.session.submit('acknowledge', h.manuscript)
  h.session.editNote(h.manuscript.chapter_id, '提交后继续写的说明。')
  h.setCurrent(h.fixture.after); pending.resolve(h.fixture.receipt); await operation
  expect(h.session.getSnapshot().notes[h.manuscript.chapter_id]).toBe('提交后继续写的说明。')
  expect(h.session.getSnapshot().pending).toBeNull()
  expect(h.session.getSnapshot().view?.confirmed_count).toBe(1)
})

it('serializes decisions and does not replace a pending request when the author changes direction', async () => {
  const h = await setup(), pending = deferred<unknown>()
  h.save.mockReturnValueOnce(pending.promise)
  const operation = h.session.submit('acknowledge', h.manuscript)
  const original = h.session.getSnapshot().pending!.body
  h.session.edit('direction', '另一项新输入')
  await expect(h.session.submit('direction')).rejects.toThrow()
  expect(h.session.getSnapshot().pending!.body).toBe(original)
  h.setCurrent(h.fixture.after); pending.resolve(h.fixture.receipt); await operation
  expect(h.session.getSnapshot().direction).toBe('另一项新输入')
  expect(h.save).toHaveBeenCalledTimes(1)
})

it('restores an uncertain request across remount and retries the original UUID, source and full prose', async () => {
  const h = await setup()
  h.save.mockRejectedValueOnce(new Error('response lost'))
  await expect(h.session.submit('acknowledge', h.manuscript)).rejects.toThrow()
  const original = h.save.mock.calls[0][0]
  h.session.editNote(h.manuscript.chapter_id, '重连前的新说明')
  const restored = new ArcProgressSession(h.pid, h.vid, h.deps)
  restored.attach(); await restored.reload(); await restored.retry()
  expect(h.save.mock.calls[1][0]).toBe(original)
  expect(h.deps.id).toHaveBeenCalledTimes(1)
  expect(restored.getSnapshot().notes[h.manuscript.chapter_id]).toBe('重连前的新说明')
})

it('queries the original durable receipt without performing another write', async () => {
  const h = await setup()
  h.save.mockRejectedValueOnce(new Error('ack lost'))
  await expect(h.session.submit('acknowledge', h.manuscript)).rejects.toThrow()
  h.setCurrent(h.fixture.after)
  await h.session.checkReceipt()
  expect(h.session.getSnapshot().pending).toBeNull()
  expect(h.save).toHaveBeenCalledTimes(1)
  expect(h.receipt).toHaveBeenCalledWith(h.fixture.input.request_id, expect.any(AbortSignal))
})

it('keeps the pending body when POST succeeds but receipt verification fails', async () => {
  const h = await setup()
  h.receipt.mockRejectedValueOnce(new ApiError('missing receipt', 404, 'arc_progress_unavailable'))
  await expect(h.session.submit('acknowledge', h.manuscript)).rejects.toThrow()
  expect(h.session.getSnapshot().pending?.sent).toBe(true)
  expect(h.session.getSnapshot().pending?.rejected).toBe(false)
  h.session.dismissRejected()
  expect(h.session.getSnapshot().pending).not.toBeNull()
  await h.session.checkReceipt()
  expect(h.session.getSnapshot().pending).toBeNull()
})

it('does not replace a newer current view with the result of an older request', async () => {
  const h = await setup()
  h.save.mockRejectedValueOnce(new Error('ack lost'))
  await expect(h.session.submit('acknowledge', h.manuscript)).rejects.toThrow()
  const later = structuredClone(h.fixture.after)
  later.source.latest = { request_id: '00000000-0000-4000-8000-000000000002', sequence: 2, result_hash: '2'.repeat(64) }
  later.progress.direction = '后来的方向'; later.progress.phase = 'active'
  later.progress.direction_chapter_id = later.next_chapter_id
  h.setCurrent(later); await h.session.reload(); await h.session.checkReceipt()
  expect(h.session.getSnapshot().receipt?.sequence).toBe(1)
  expect(h.session.getSnapshot().view?.source.latest?.sequence).toBe(2)
  expect(h.session.getSnapshot().view?.progress.direction).toBe('后来的方向')
})

it('ignores a stale read that completes after confirmation', async () => {
  const h = await setup(), posting = deferred<unknown>(), reading = deferred<unknown>()
  h.save.mockReturnValueOnce(posting.promise)
  const submitting = h.session.submit('acknowledge', h.manuscript)
  h.read.mockReturnValueOnce(reading.promise)
  const oldRead = h.session.reload()
  h.setCurrent(h.fixture.after); posting.resolve(h.fixture.receipt); await submitting
  reading.resolve(h.fixture.before); await oldRead
  expect(h.session.getSnapshot().view?.confirmed_count).toBe(1)
})

it('refuses a read older than the known durable sequence', async () => {
  const h = await setup()
  await h.session.submit('acknowledge', h.manuscript)
  h.setCurrent(h.fixture.before); await h.session.reload()
  expect(h.session.getSnapshot().view).toBeNull()
  expect(h.session.getSnapshot().error).toContain('早于')
  expect(h.session.getSnapshot().receipt?.sequence).toBe(1)
})

it.each(['first', 'retry'])('does not send a %s request until its complete body is stored', async stage => {
  const h = await setup()
  if (stage === 'retry') {
    h.save.mockRejectedValueOnce(new Error('lost'))
    await expect(h.session.submit('acknowledge', h.manuscript)).rejects.toThrow()
  }
  h.storage.setItem.mockImplementation(() => { throw new Error('quota') })
  await expect(stage === 'retry' ? h.session.retry() : h.session.submit('acknowledge', h.manuscript)).rejects.toThrow()
  expect(h.save).toHaveBeenCalledTimes(stage === 'retry' ? 1 : 0)
  expect(h.session.getSnapshot().pending?.sent).toBe(stage === 'retry')
  expect(h.session.getSnapshot().pending?.rejected).toBe(stage === 'first')
})

it('retains source conflicts until the author explicitly rechecks, while keeping typed notes', async () => {
  const h = await setup()
  h.save.mockRejectedValueOnce(new ApiError('正文已改变', 409, 'arc_manuscript_changed'))
  await expect(h.session.submit('acknowledge', h.manuscript)).rejects.toThrow()
  h.session.editNote(h.manuscript.chapter_id, '待保留的说明')
  expect(h.session.getSnapshot().pending?.rejected).toBe(true)
  h.session.dismissRejected(); await h.session.reload()
  expect(h.session.getSnapshot().pending).toBeNull()
  expect(h.session.getSnapshot().notes[h.manuscript.chapter_id]).toBe('待保留的说明')
})

it.each(['uuid_conflict', 'bad_receipt', 'network'])('does not discard an uncertain %s request', async kind => {
  const h = await setup()
  const error = kind === 'network' ? new Error('connection') : new ApiError('unverified', 409,
    kind === 'uuid_conflict' ? 'arc_progress_request_conflict' : 'arc_progress_receipt_invalid')
  h.save.mockRejectedValueOnce(error)
  await expect(h.session.submit('acknowledge', h.manuscript)).rejects.toThrow()
  h.session.dismissRejected()
  expect(h.session.getSnapshot().pending).not.toBeNull()
  expect(h.session.getSnapshot().pending?.rejected).toBe(false)
})

it('preserves a damaged stored request without overwriting it or allowing a new write', async () => {
  const h = await setup()
  const key = [...h.values.keys()][0]
  h.values.set(key, '{ incomplete record')
  const restored = new ArcProgressSession(h.pid, h.vid, h.deps)
  restored.attach(); await restored.reload()
  restored.edit('direction', '仍在本页的新输入')
  await expect(restored.submit('acknowledge', h.manuscript)).rejects.toThrow()
  expect(restored.getSnapshot().unreadableDraft).toBe('{ incomplete record')
  expect(h.values.get(key)).toBe('{ incomplete record')
  expect(h.save).not.toHaveBeenCalled()
})
