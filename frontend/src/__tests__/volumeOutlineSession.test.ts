import { afterEach, expect, it, vi } from 'vitest'
import { ApiError } from '@/lib/api'
import { sameJson, volumeOutlineEditText, type VolumeOutlineSource, type VolumeOutlineWrite } from '@/lib/volumeOutlines'
import { VolumeOutlineSession } from '@/components/outline/volumeOutlineSession'
import { volumeReceipt, volumeSource } from './fixtures/volumeOutline'

afterEach(() => vi.restoreAllMocks())

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (error: Error) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
function setup(project = 'p', volume = 'v') {
  let current = volumeSource(project, volume), id = 0
  const values = new Map<string, string>()
  const storage = { getItem: (key: string) => values.get(key) ?? null, setItem: vi.fn((key: string, value: string) => { values.set(key, value) }) }
  const read = vi.fn(async (_signal: AbortSignal): Promise<unknown> => structuredClone(current))
  const save = vi.fn(async (body: string, _signal: AbortSignal): Promise<unknown> => {
    const input = JSON.parse(body) as VolumeOutlineWrite
    if (!sameJson(input.expected_source, current)) throw new ApiError('卷纲已修改', 409, 'volume_outline_conflict')
    const receipt = volumeReceipt(input)
    current = receipt.source
    return receipt
  })
  const deps = { read, save, storage: () => storage, id: vi.fn(() => `00000000-0000-4000-8000-${String(++id).padStart(12, '0')}`) }
  const session = new VolumeOutlineSession(project, volume, deps)
  session.attach(current)
  return { session, deps, read, save, values, storage, current: () => current,
    setCurrent: (source: VolumeOutlineSource) => { current = source } }
}

it('persists the complete request before sending and keeps typing during save', async () => {
  const h = setup(), pending = deferred<unknown>()
  h.session.edit('第一份完整编辑稿。')
  h.save.mockImplementationOnce(async (body) => {
    expect([...h.values.values()][0]).toContain(JSON.stringify(body).slice(1, -1))
    return pending.promise
  })
  const saving = h.session.save()
  const input = JSON.parse(h.save.mock.calls[0][0])
  h.session.edit('保存期间继续修改的完整稿。')
  pending.resolve(volumeReceipt(input))
  await saving
  expect(h.session.getSnapshot().text).toBe('保存期间继续修改的完整稿。')
  expect(h.session.getSnapshot().source?.outline?.content_json.raw_text).toBe('第一份完整编辑稿。')
  expect(h.session.getSnapshot().pending).toBeNull()
})

it('serializes submissions and never changes the pending request after further typing', async () => {
  const h = setup(), pending = deferred<unknown>()
  h.save.mockReturnValueOnce(pending.promise)
  h.session.edit('待提交稿。')
  const saving = h.session.save()
  const body = h.session.getSnapshot().pending!.body
  await expect(h.session.save()).rejects.toThrow()
  h.session.edit('后续稿。')
  h.session.useCandidate({ task_id: 'other', text: '另一候选。' })
  expect(h.session.getSnapshot().pending!.body).toBe(body)
  expect(h.save).toHaveBeenCalledTimes(1)
  pending.resolve(volumeReceipt(JSON.parse(body)))
  await saving
})

it('restores an uncertain request after remount and retries its exact original body', async () => {
  const h = setup()
  h.save.mockRejectedValueOnce(new Error('response lost'))
  h.session.edit('已发送的稿。')
  await expect(h.session.save()).rejects.toThrow('response lost')
  const original = h.save.mock.calls[0][0]
  h.session.edit('断网后的后续输入。')
  const restored = new VolumeOutlineSession('p', 'v', h.deps)
  restored.attach()
  await restored.reload()
  h.save.mockResolvedValueOnce(volumeReceipt(JSON.parse(original)))
  await restored.retry()
  expect(h.save.mock.calls[1][0]).toBe(original)
  expect(restored.getSnapshot().text).toBe('断网后的后续输入。')
  expect(h.deps.id).toHaveBeenCalledTimes(1)
})

it('preserves a newer remote draft when recovering an older committed receipt', async () => {
  const h = setup()
  h.session.edit('第一次保存稿。')
  h.save.mockRejectedValueOnce(new Error('lost acknowledgement'))
  await expect(h.session.save()).rejects.toThrow()
  const input = JSON.parse(h.save.mock.calls[0][0]), oldReceipt = volumeReceipt(input)
  const later = volumeSource('p', 'v', '其他窗口后来保存的卷纲。')
  later.outline!.version = 8
  h.setCurrent(later)
  await h.session.reload()
  h.save.mockResolvedValueOnce(oldReceipt)
  await h.session.retry()
  expect(h.session.getSnapshot().remote).toEqual(later)
  expect(h.session.getSnapshot().text).toBe('第一次保存稿。')
})

it('requires explicit reconciliation after a conflict and preserves an authors return to the old text', async () => {
  const h = setup(), original = h.session.getSnapshot().text
  h.session.edit('新稿。')
  h.setCurrent(volumeSource('p', 'v', '另一处的新稿。'))
  await expect(h.session.save()).rejects.toThrow()
  h.session.edit(original)
  await h.session.reload()
  expect(h.session.getSnapshot().text).toBe(original)
  expect(h.session.getSnapshot().pending?.rejected).toBe(true)
  await expect(h.session.save()).rejects.toThrow()
  h.session.useRemoteSource()
  expect(h.session.getSnapshot().text).toBe(original)
  await h.session.save()
  expect(h.current().outline?.content_json.raw_text).toBe(original)
})

it('invalidates a late read when a save receipt has already confirmed the new source', async () => {
  const h = setup(), stale = h.current(), pending = deferred<unknown>()
  h.read.mockReturnValueOnce(pending.promise)
  const reading = h.session.reload()
  h.session.edit('这份新卷纲已保存。')
  await h.session.save()
  pending.resolve(stale)
  await reading
  expect(h.session.getSnapshot().text).toBe('这份新卷纲已保存。')
  expect(h.session.getSnapshot().source).toEqual(h.current())
})

it('preserves typing that begins while a clean outline is being refreshed', async () => {
  const h = setup(), pending = deferred<unknown>()
  h.read.mockReturnValueOnce(pending.promise)
  const reading = h.session.reload()
  h.session.edit('读取期间的作者输入。')
  const remote = volumeSource('p', 'v', '服务端另一稿。')
  pending.resolve(remote)
  await reading
  expect(h.session.getSnapshot().remote).toEqual(remote)
  expect(h.session.getSnapshot().text).toBe('读取期间的作者输入。')
})

it('does not send until the full request can be stored', async () => {
  const h = setup()
  h.session.edit('需要保留的长卷纲。')
  h.storage.setItem.mockImplementation(() => { throw new Error('quota') })
  await expect(h.session.save()).rejects.toThrow('尚未发送')
  expect(h.save).not.toHaveBeenCalled()
  expect(h.session.getSnapshot().pending?.sent).toBe(false)
  expect(h.session.getSnapshot().text).toBe('需要保留的长卷纲。')
})

it.each(['source', 'uuid', 'draft', 'json'])('blocks damaged stored %s without overwriting the original record', async (damage) => {
  const h = setup()
  h.session.edit('完整作者稿。')
  h.save.mockRejectedValueOnce(new Error('offline'))
  await expect(h.session.save()).rejects.toThrow()
  const [key, stored] = [...h.values.entries()][0]
  const value = JSON.parse(stored)
  if (damage === 'source') value.source = null
  else if (damage === 'uuid') value.pending.body = JSON.stringify({ ...JSON.parse(value.pending.body), request_id: '-'.repeat(36) })
  else if (damage === 'draft') value.pending.draft = '不属于该请求的稿。'
  const raw = damage === 'json' ? '{broken' : JSON.stringify(value)
  h.values.set(key, raw)
  const restored = new VolumeOutlineSession('p', 'v', h.deps)
  restored.attach()
  await restored.reload()
  restored.edit('不应覆盖损坏记录。')
  expect(restored.getSnapshot().storageError).toBeTruthy()
  expect(h.values.get(key)).toBe(raw)
  expect(h.read).not.toHaveBeenCalled()
})

it('keeps a candidate separate until explicitly chosen, and restores the replaced structural draft', async () => {
  const h = setup()
  h.session.changeFormat('structure')
  const structural = h.session.getSnapshot().text
  h.session.useCandidate({ task_id: 'task-a', text: '候选安排全文。' })
  expect(h.save).not.toHaveBeenCalled()
  expect(h.session.getSnapshot().format).toBe('text')
  h.session.restorePreviousDraft()
  expect(h.session.getSnapshot().text).toBe(structural)
  expect(h.session.getSnapshot().format).toBe('structure')
  expect(h.session.getSnapshot().candidate).toBeNull()
})

it('keeps the original author draft when the same candidate is selected twice', () => {
  const h = setup()
  h.session.edit('替换前必须保留的作者安排。')
  const candidate = { task_id: 'same-task', text: '同一份候选。' }
  h.session.useCandidate(candidate)
  h.session.useCandidate(candidate)
  h.session.restorePreviousDraft()
  expect(h.session.getSnapshot().text).toBe('替换前必须保留的作者安排。')
})

it('stores structure edits using the same request and history contract', async () => {
  const h = setup()
  h.session.changeFormat('structure')
  const content = { ...h.current().outline!.content_json, chapter_summaries: [{ chapter_idx: 1, summary: '核对收据。' }] }
  h.session.edit(JSON.stringify(content))
  await h.session.save()
  const input = JSON.parse(h.save.mock.calls[0][0])
  expect(input.content_json).toEqual(content)
  expect(input.text).toBeUndefined()
  expect(h.session.getSnapshot().text).toBe(volumeOutlineEditText(h.current(), 'structure'))
})

it('keeps invalid structure text editable without sending a different request', async () => {
  const h = setup()
  h.session.changeFormat('structure')
  h.session.edit('{未写完')
  await expect(h.session.save()).rejects.toThrow('格式不完整')
  expect(h.session.getSnapshot().pending).toBeNull()
  expect(h.session.getSnapshot().text).toBe('{未写完')
  expect(h.save).not.toHaveBeenCalled()
  h.session.changeFormat('text')
  expect(h.session.getSnapshot().format).toBe('structure')
})

it('treats a mismatched receipt as uncertain and preserves the original request', async () => {
  const h = setup()
  h.save.mockImplementationOnce(async (body) => ({ ...volumeReceipt(JSON.parse(body)), request_id: 'wrong' }))
  h.session.edit('待确认卷纲。')
  await expect(h.session.save()).rejects.toThrow('回执')
  expect(h.session.getSnapshot().pending?.rejected).toBe(false)
  expect(h.session.getSnapshot().text).toBe('待确认卷纲。')
})

it('keeps different volumes and projects independent', () => {
  const a = setup('a', 'v'), b = setup('b', 'v')
  a.session.edit('甲书作者卷纲。')
  b.session.edit('乙书作者卷纲。')
  expect(a.session.getSnapshot().text).toBe('甲书作者卷纲。')
  expect(b.session.getSnapshot().text).toBe('乙书作者卷纲。')
  expect([...a.values.keys()][0]).not.toBe([...b.values.keys()][0])
})
