import { afterEach, expect, it, vi } from 'vitest'
import { apiFetch } from '@/lib/api'
import { ManuscriptSource, parseManuscript, readManuscript, saveManuscript } from '@/lib/chapterManuscripts'

vi.mock('@/lib/api', () => ({ apiFetch: vi.fn() }))
const api = vi.mocked(apiFetch)
const original: ManuscriptSource = { project_id: 'p', chapter_id: 'a', title: '交钱',
  content_text: '完整原稿。'.repeat(3000), word_count: 15000, status: 'completed' }
afterEach(() => vi.resetAllMocks())

it('requires the correct project, chapter, full text and Unicode count', () => {
  expect(parseManuscript({ ...original, content_text: '𠮷说。', word_count: 3 }, 'p', 'a').word_count).toBe(3)
  expect(() => parseManuscript(original, 'other', 'a')).toThrow('不匹配')
  expect(() => parseManuscript(original, 'p', 'b')).toThrow('不匹配')
  expect(() => parseManuscript({ ...original, content_text: '𠮷说。', word_count: 4 }, 'p', 'a')).toThrow('不一致')
})

it('reads the canonical chapter source using the supplied abort signal', async () => {
  const abort = new AbortController()
  api.mockResolvedValue(original)
  expect(await readManuscript('p', 'a', abort.signal)).toEqual(original)
  expect(api).toHaveBeenCalledWith('/api/projects/p/chapters/a/content', { signal: abort.signal })
})

it('sends the entire original source and accepts only a matching saved receipt', async () => {
  const saved = { ...original, content_text: '他接过钱。', word_count: 5, status: 'needs_review' }
  api.mockResolvedValue({ status: 'saved', manuscript: saved, changed: true, backup_version_id: 'backup' })
  await saveManuscript(original, saved.content_text, true, 'needs_review')
  expect(JSON.parse(api.mock.calls[0][1]!.body as string)).toEqual({ content_text: saved.content_text,
    expected_content_text: original.content_text, force: true, status: 'needs_review' })
})

it.each(['id', 'content', 'status', 'backup', 'changed'])('rejects an incomplete or mismatched %s receipt', async (fault) => {
  const saved = { ...original, content_text: '新稿。', word_count: 3, status: 'draft' }
  const response = { status: 'saved', manuscript: saved, changed: true, backup_version_id: 'backup' as string | null }
  if (fault === 'id') saved.chapter_id = 'b'
  if (fault === 'content') saved.content_text = '旧稿。'
  if (fault === 'status') saved.status = 'completed'
  if (fault === 'backup') response.backup_version_id = null
  if (fault === 'changed') response.changed = false
  api.mockResolvedValue(response)
  await expect(saveManuscript(original, '新稿。', true, 'draft')).rejects.toThrow()
})
