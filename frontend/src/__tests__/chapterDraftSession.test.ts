import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { ApiError } from '@/lib/api'
import { ChapterDraftSession, getChapterDraftSession } from '@/components/editor/chapterDraftSession'
import { ManuscriptReceipt, ManuscriptSource } from '@/lib/chapterManuscripts'

const source = (chapter = 'a', text = '原来的完整正文。', project = 'p'): ManuscriptSource => ({
  project_id: project, chapter_id: chapter, content_text: text, title: '交钱', status: 'completed', word_count: [...text].length,
})
function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (error: Error) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
const detached: (() => void)[] = []
beforeEach(() => vi.useFakeTimers())
afterEach(() => { for (const detach of detached.splice(0)) detach(); vi.clearAllTimers(); vi.useRealTimers() })

async function setup(chapter = 'a', project = 'p', initial?: string) {
  let saved = source(chapter, initial, project)
  const values = new Map<string, string>()
  const storage = { getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, text: string) => { values.set(key, text) }, removeItem: (key: string) => { values.delete(key) } }
  const read = vi.fn(async () => saved)
  const save = vi.fn(async (before: ManuscriptSource, text: string, force = false, status: 'draft' | 'needs_review' | null = null): Promise<ManuscriptReceipt> => {
    if (before.content_text !== saved.content_text) throw new ApiError('正文已变化', 409, 'chapter_edit_conflict')
    const changed = text !== saved.content_text
    saved = { ...saved, content_text: text, word_count: [...text].length, status: status ?? saved.status }
    return { status: 'saved', manuscript: saved, changed, backup_version_id: force && changed ? 'backup' : null }
  })
  const dependencies = { read, save, storage }
  const session = new ChapterDraftSession(project, chapter, dependencies)
  const detach = session.attach()
  detached.push(detach)
  await Promise.resolve()
  return { session, read, save, dependencies, values, storage, detach,
    setSaved: (text: string) => { saved = source(chapter, text, project) }, saved: () => saved }
}

it('serializes overlapping edits and does not replace new typing with an old acknowledgement', async () => {
  const h = await setup()
  const original = h.saved(), first = deferred<ManuscriptReceipt>()
  h.save.mockImplementationOnce(() => first.promise)
  h.session.edit('第一次修改。')
  const writing = h.session.save()
  h.session.edit('保存期间继续写。')
  await vi.advanceTimersByTimeAsync(4000)
  expect(h.save).toHaveBeenCalledTimes(1)
  h.setSaved('第一次修改。')
  first.resolve({ status: 'saved', manuscript: h.saved(), changed: true, backup_version_id: null })
  expect(await writing).toBe(true)
  expect(h.session.getSnapshot().text).toBe('保存期间继续写。')
  expect(h.save.mock.calls[0][0].content_text).toBe(original.content_text)
  await vi.advanceTimersByTimeAsync(3000)
  expect(h.save).toHaveBeenCalledTimes(2)
  expect(h.save.mock.calls[1][0].content_text).toBe('第一次修改。')
  expect(h.saved().content_text).toBe('保存期间继续写。')
})

it('keeps the original baseline after a conflict and never silently retries over the remote text', async () => {
  const h = await setup()
  h.session.edit('编辑区完整稿。')
  h.setSaved('另一处保存的新稿。')
  expect(await h.session.save()).toBe(false)
  h.session.edit('继续改过的编辑稿。')
  await vi.advanceTimersByTimeAsync(10000)
  expect(h.save).toHaveBeenCalledTimes(1)
  expect(h.session.getSnapshot().source?.content_text).toBe('原来的完整正文。')
  await h.session.reload()
  expect(h.session.getSnapshot().text).toBe('继续改过的编辑稿。')
  expect(h.session.getSnapshot().comparison?.content_text).toBe('另一处保存的新稿。')
  await h.session.saveReviewedDraft()
  expect(h.save.mock.calls[1][0].content_text).toBe('另一处保存的新稿。')
  expect(h.saved().content_text).toBe('继续改过的编辑稿。')
})

it('reconciles a lost commit response by reading the real saved body without a duplicate PUT', async () => {
  const h = await setup()
  h.save.mockImplementationOnce(async (_before, text) => { h.setSaved(text); throw new Error('response lost') })
  h.session.edit('实际已经提交的稿件。')
  expect(await h.session.save()).toBe(false)
  expect(h.session.getSnapshot().blocked).toBe(true)
  await h.session.reload()
  expect(h.session.isDirty()).toBe(false)
  expect(h.session.getSnapshot().blocked).toBe(false)
  expect(h.save).toHaveBeenCalledTimes(1)
})

it.each(['conflict', 'lost_commit'])('preserves the authors return to the old baseline after %s', async (failure) => {
  const h = await setup(), original = h.saved().content_text
  h.session.edit('准备保存的第二稿。')
  if (failure === 'conflict') h.setSaved('另一处的修改稿。')
  else h.save.mockImplementationOnce(async (_source, text) => { h.setSaved(text); throw new Error('lost acknowledgement') })
  expect(await h.session.save()).toBe(false)
  h.session.edit(original)
  expect(h.session.hasPending()).toBe(true)
  await h.session.reload()
  expect(h.session.getSnapshot().text).toBe(original)
  expect(h.session.getSnapshot().comparison?.content_text).toBe(h.saved().content_text)
  expect(h.save).toHaveBeenCalledTimes(1)
})

it('does not overwrite typing that happens during a refresh which began on a clean chapter', async () => {
  const h = await setup(), pending = deferred<ManuscriptSource>()
  h.read.mockReturnValueOnce(pending.promise)
  const refresh = h.session.reload()
  h.session.edit('读取期间的新输入。')
  pending.resolve(source('a', '后台新保存稿。'))
  await refresh
  expect(h.session.getSnapshot().text).toBe('读取期间的新输入。')
  expect(h.session.getSnapshot().comparison?.content_text).toBe('后台新保存稿。')
  expect(h.save).not.toHaveBeenCalled()
})

it('ignores an old chapter read after detach and keeps another chapter independent', async () => {
  const a = await setup('a'), b = await setup('b'), pending = deferred<ManuscriptSource>()
  a.read.mockReturnValueOnce(pending.promise)
  const refresh = a.session.reload()
  a.detach()
  b.session.edit('乙章的修改。')
  pending.resolve(source('a', '甲章迟到正文。'))
  await refresh
  expect(a.session.getSnapshot().text).toBe('原来的完整正文。')
  expect(b.session.getSnapshot().text).toBe('乙章的修改。')
  expect(b.session.getSnapshot().source?.chapter_id).toBe('b')
})

it('retains the same pending writer when returning to a chapter before the response arrives', async () => {
  const h = await setup(), pending = deferred<ManuscriptReceipt>()
  h.save.mockReturnValueOnce(pending.promise)
  h.session.edit('正在保存的甲章。')
  const write = h.session.save()
  h.detach()
  const resumed = getChapterDraftSession('p', 'a')
  expect(resumed).toBe(h.session)
  detached.push(resumed.attach())
  resumed.edit('返回甲章后继续写。')
  h.setSaved('正在保存的甲章。')
  pending.resolve({ status: 'saved', manuscript: h.saved(), changed: true, backup_version_id: null })
  await write
  expect(resumed.getSnapshot().text).toBe('返回甲章后继续写。')
  expect(resumed.getSnapshot().source?.content_text).toBe('正在保存的甲章。')
})

it('re-registers the same session after an effect cleanup and setup', async () => {
  const h = await setup()
  h.detach()
  detached.push(h.session.attach())
  await Promise.resolve()
  h.session.edit('重新挂载后的稿件。')
  expect(getChapterDraftSession('p', 'a')).toBe(h.session)
})

it('keeps unsaved text and its whole baseline in the browser checkpoint when switching chapters', async () => {
  const h = await setup('a', 'p', '原稿内容。'.repeat(5000))
  const draft = '尚未提交。'.repeat(6000)
  h.session.edit(draft)
  h.detach()
  await vi.advanceTimersByTimeAsync(5000)
  expect(h.save).not.toHaveBeenCalled()
  const checkpoint = JSON.parse([...h.values.values()][0])
  expect(checkpoint.text).toBe(draft)
  expect(checkpoint.source.content_text).toBe(h.saved().content_text)
})

it('recovers a browser draft for comparison without automatically saving it', async () => {
  const h = await setup()
  h.session.edit('浏览器暂存全文。')
  h.detach()
  h.setSaved('另一页修改过的稿件。')
  const resumed = new ChapterDraftSession('p', 'a', h.dependencies)
  detached.push(resumed.attach())
  await Promise.resolve()
  expect(resumed.getSnapshot().text).toBe('浏览器暂存全文。')
  expect(resumed.getSnapshot().comparison?.content_text).toBe('另一页修改过的稿件。')
  await vi.advanceTimersByTimeAsync(5000)
  expect(h.save).not.toHaveBeenCalled()
})

it('keeps a recovered draft even if its stored source metadata is damaged', async () => {
  const h = await setup()
  h.detach()
  h.values.set('aiwrite.chapter-draft.v1.p.a', JSON.stringify({ schema: 1, projectId: 'p', chapterId: 'a',
    text: '需要救回的完整稿件。', source: { invalid: true } }))
  const resumed = new ChapterDraftSession('p', 'a', h.dependencies)
  detached.push(resumed.attach())
  await Promise.resolve()
  expect(resumed.getSnapshot().text).toBe('需要救回的完整稿件。')
  expect(resumed.getSnapshot().comparison?.content_text).toBe('原来的完整正文。')
})

it('keeps the in-memory draft and exposes browser quota failure', async () => {
  const h = await setup()
  vi.spyOn(h.storage, 'setItem').mockImplementation(() => { throw new Error('quota') })
  h.session.edit('不能丢的稿件。')
  await vi.advanceTimersByTimeAsync(300)
  expect(h.session.getSnapshot().text).toBe('不能丢的稿件。')
  expect(h.session.getSnapshot().backupError).toContain('下载备份')
})

it('checkpoints the latest keystrokes before a pending browser navigation', async () => {
  const h = await setup()
  h.session.edit('离开前最后输入的一句。')
  h.session.preserve()
  const checkpoint = JSON.parse([...h.values.values()][0])
  expect(checkpoint.text).toBe('离开前最后输入的一句。')
  expect(h.save).not.toHaveBeenCalled()
})

it('makes load failure visible and permits a successful read to recover', async () => {
  const read = vi.fn().mockRejectedValueOnce(new Error('章节暂不可读')).mockResolvedValue(source('load-failure'))
  const h = new ChapterDraftSession('p', 'load-failure', { read, save: vi.fn(), storage: null })
  detached.push(h.attach())
  await Promise.resolve()
  expect(h.getSnapshot().error).toBe('章节暂不可读')
  expect(h.canEdit()).toBe(false)
  await h.reload()
  expect(h.getSnapshot().text).toBe('原来的完整正文。')
})

it('requires explicit review before allowing a protected reduction with a full new baseline', async () => {
  const h = await setup()
  h.session.edit('')
  await h.session.reload()
  expect(h.session.getSnapshot().blocked).toBe(true)
  expect(h.save).not.toHaveBeenCalled()
  await h.session.saveReviewedDraft(true)
  expect(h.save.mock.calls[0].slice(1, 3)).toEqual(['', true])
  expect(h.session.getSnapshot().notice).toContain('版本历史')
})

it('does not begin generation over a dirty, pending or conflicted author draft', async () => {
  const h = await setup(), pending = deferred<ManuscriptReceipt>()
  h.session.edit('还在编辑。')
  expect(h.session.beginGeneration()).toBeNull()
  h.save.mockReturnValueOnce(pending.promise)
  const writing = h.session.save()
  expect(h.session.beginGeneration()).toBeNull()
  pending.reject(new Error('unknown'))
  await writing
  expect(h.session.beginGeneration()).toBeNull()
})

it('uses the saved draft only after the author explicitly chooses it', async () => {
  const h = await setup()
  h.session.edit('未保存的编辑稿。')
  h.setSaved('外部的保存稿。')
  await h.session.reload()
  expect(h.session.getSnapshot().text).toBe('未保存的编辑稿。')
  h.session.useSavedDraft()
  expect(h.session.getSnapshot().text).toBe('外部的保存稿。')
  expect(h.session.isDirty()).toBe(false)
  expect(h.save).not.toHaveBeenCalled()
})
