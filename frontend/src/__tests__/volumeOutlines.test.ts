import { expect, it } from 'vitest'
import { parseVolumeOutlineReceipt, parseVolumeOutlineSource, volumeOutlineContentText } from '@/lib/volumeOutlines'
import { selectVolumeOutlines } from '@/lib/volumeOutlineSelection'
import { volumeReceipt, volumeSource } from './fixtures/volumeOutline'

it('renders complete author text ahead of older extracted plot fields', () => {
  const text = '作者完整改稿：两千元已经交清。'.repeat(5000)
  expect(volumeOutlineContentText({ volume_id: 'v', raw_text: text, _author_text_revision: 'author', summary: '旧摘要' })).toBe(text)
})

it('renders all structured narrative fields and marks unverified chapter associations', () => {
  const rendered = volumeOutlineContentText({ volume_id: 'v', raw_text: '旧原文', summary: '当前摘要',
    chapter_summaries: [{ chapter_idx: 2, _chapter_id: null, _order_unavailable: true, raw_text: '保留的章节详解',
      unknown_story_field: '不应丢弃的未知剧情字段' }], cross_volume_arrangement: { result: '为下一卷留下的具体结果。' } })
  expect(rendered).not.toContain('旧原文')
  expect(rendered).toContain('保留的章节详解')
  expect(rendered).toContain('不应丢弃的未知剧情字段')
  expect(rendered).toContain('为下一卷留下的具体结果。')
  expect(rendered).toContain('原章节关联待核对')
})

it('resolves stable volume IDs before numeric labels and skips explicit unknown owners', () => {
  const outlines = [
    { id: 'wrong', level: 'volume', version: 10, is_confirmed: 1, content_json: { volume_id: '', volume_idx: 1 } },
    { id: 'old-label', level: 'volume', version: 3, is_confirmed: 1, content_json: { volume_id: 'v', volume_idx: 9 } },
    { id: 'foreign', level: 'volume', version: 20, is_confirmed: 1, content_json: { volume_id: 'other', volume_idx: 1 } },
  ]
  expect(selectVolumeOutlines(outlines, [{ id: 'v', volume_idx: 1 }]).v.id).toBe('old-label')
  expect(selectVolumeOutlines(outlines, [{ id: 'v', volume_idx: 1 }, { id: 'duplicate', volume_idx: 1 }])).toEqual({})
})

it('uses confirmation, version, microsecond timestamp and ID to choose current outlines', () => {
  const base = { level: 'volume', is_confirmed: 1, version: 2, content_json: { volume_idx: 1 }, created_at: '2026-10-09T12:00:00.000001Z' }
  const outlines = [
    { ...base, id: 'higher-version-unconfirmed', is_confirmed: 0, version: 100 },
    { ...base, id: 'earlier-version', version: 1 },
    { ...base, id: 'z-old-time' },
    { ...base, id: 'a-new-time', created_at: '2026-10-09T12:00:00.000002Z' },
    { ...base, id: 'b-new-time', created_at: '2026-10-09T12:00:00.000002Z' },
  ]
  expect(selectVolumeOutlines(outlines, [{ id: 'v', volume_idx: 1 }]).v.id).toBe('b-new-time')
})

it.each(['project', 'volume', 'selection', 'confirmation'])('rejects a source with inconsistent %s', (field) => {
  const source = volumeSource()
  if (field === 'project') source.project_id = 'other'
  if (field === 'volume') source.volume_id = 'other'
  if (field === 'selection') source.selection[0].id = 'other'
  if (field === 'confirmation') source.outline!.is_confirmed = 7
  expect(() => parseVolumeOutlineSource(source, 'p', 'v')).toThrow()
})

it.each(['request', 'text', 'candidate', 'title'])('rejects a receipt with changed %s', (field) => {
  const input = { request_id: 'r', expected_source: volumeSource(), text: '准备保存的完整卷纲。', candidate: null }
  const receipt = volumeReceipt(input)
  if (field === 'request') receipt.request_id = 'other'
  if (field === 'text') receipt.source.outline!.content_json.raw_text += '不同的末尾'
  if (field === 'candidate') receipt.candidate_task_id = 'other'
  if (field === 'title') receipt.source.title = '另一个卷名'
  expect(() => parseVolumeOutlineReceipt(receipt, input)).toThrow()
})
