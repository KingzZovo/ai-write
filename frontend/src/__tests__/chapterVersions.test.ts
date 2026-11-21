import { afterEach, expect, it, vi } from 'vitest'
import { apiFetch } from '@/lib/api'
import { ChapterVersion, VersionSource, parseChapterVersion, restoreChapterVersion } from '@/lib/chapterVersions'

vi.mock('@/lib/api', () => ({ apiFetch: vi.fn() }))
const api = vi.mocked(apiFetch)
const version: ChapterVersion = { id: 'v1', chapter_id: 'a', parent_id: null, branch_name: '初稿',
  content_text: '候选正文。', content_diff: '', word_count: 5, created_at: '', is_active: false, source: 'user_edit', metadata: {} }
const source: VersionSource = { chapter_id: 'a', project_id: 'p', title: '交付', content_text: '完整保存稿。'.repeat(2000), word_count: 14000 }
afterEach(() => vi.resetAllMocks())

it('uses the actual snake_case version contract and rejects another chapter', () => {
  expect(parseChapterVersion(version, 'a').branch_name).toBe('初稿')
  expect(() => parseChapterVersion(version, 'b')).toThrow('不匹配')
  expect(() => parseChapterVersion({ id: 'v1', branchName: '错误合同' }, 'a')).toThrow('不完整')
})

it('sends the complete reviewed source and requires a confirmed backup', async () => {
  api.mockResolvedValue({ status: 'ok', chapter_id: 'a', active_version_id: 'v1',
    content_text: version.content_text, word_count: 5, backup_version_id: 'backup', changed: true })
  const result = await restoreChapterVersion('a', version, source, true)
  expect(JSON.parse(api.mock.calls[0][1]!.body as string)).toEqual({ expected_content_text: source.content_text, force: true })
  expect(result.backup_version_id).toBe('backup')
})

it('does not accept a response for another version or a missing backup receipt', async () => {
  const response = { status: 'ok', chapter_id: 'a', active_version_id: 'v1',
    content_text: version.content_text, word_count: 5, backup_version_id: null, changed: true }
  api.mockResolvedValue(response)
  await expect(restoreChapterVersion('a', version, source, false)).rejects.toThrow('完整恢复确认')
  api.mockResolvedValue({ ...response, active_version_id: 'v2', backup_version_id: 'backup' })
  await expect(restoreChapterVersion('a', version, source, false)).rejects.toThrow('完整恢复确认')
})

it('rejects a source from another chapter before issuing any write', async () => {
  await expect(restoreChapterVersion('a', version, { ...source, chapter_id: 'b' }, false)).rejects.toThrow('不匹配')
  expect(api).not.toHaveBeenCalled()
})
