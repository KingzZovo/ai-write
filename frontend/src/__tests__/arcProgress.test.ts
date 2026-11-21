import { expect, it } from 'vitest'
import { confirmArcReceipt, parseArcHistoryDetail, parseArcProgress, parseArcWrite } from '@/lib/arcProgress'
import { arcFixture } from './fixtures/arcProgress'

it('reads the exact SQLite/ASGI progress, request and durable manuscript receipt', () => {
  const { before, after, input, detail, receipt } = arcFixture()
  const { project_id: pid, volume_id: vid } = before.source
  expect(parseArcProgress(before, pid, vid)).toEqual(before)
  expect(parseArcProgress(after, pid, vid)).toEqual(after)
  expect(parseArcWrite(input, pid, vid)).toEqual(input)
  expect(confirmArcReceipt(detail, input)).toEqual(receipt)
})

it.each(['project', 'volume', 'prose', 'title', 'note', 'source', 'request', 'integrity'])('cannot confirm a mismatched %s receipt', field => {
  const { detail, input } = arcFixture()
  if (field === 'project') detail.receipt.project_id = '00000000-0000-4000-8000-000000000001'
  if (field === 'volume') detail.receipt.volume_id = '00000000-0000-4000-8000-000000000002'
  if (field === 'prose') detail.manuscript.content_text = input.expected_content_text + '不同结尾。'
  if (field === 'title') detail.manuscript.title = '改过的章名'
  if (field === 'note') detail.before.author_note = '另一份说明'
  if (field === 'source') detail.before.source = { schema: 1 }
  if (field === 'request') detail.request_id = '00000000-0000-4000-8000-000000000003'
  if (field === 'integrity') detail.integrity_valid = false
  expect(() => confirmArcReceipt(detail, input)).toThrow()
})

it.each(['count', 'next', 'confirmation', 'duplicate', 'empty_source'])('rejects inconsistent current %s instead of guessing progress', field => {
  const { after, input } = arcFixture()
  if (field === 'count') after.confirmed_count = 4
  if (field === 'next') after.next_chapter_id = input.chapter_id
  if (field === 'confirmation') after.chapters[0].confirmation = null
  if (field === 'duplicate') after.chapters.push(after.chapters[0])
  if (field === 'empty_source') after.source.chapters = []
  expect(() => parseArcProgress(after, after.source.project_id, after.source.volume_id)).toThrow()
})

it('allows damaged raw history to be retained but never used as confirmation', () => {
  const { detail, input } = arcFixture()
  detail.integrity_valid = false; detail.receipt = { damaged: true }
  expect(parseArcHistoryDetail(detail, input.request_id)).toEqual(detail)
  expect(() => confirmArcReceipt(detail, input)).toThrow()
})

it.each(['blank', 'missing_content', 'mixed_action'])('rejects %s saved manuscript requests', kind => {
  const { input } = arcFixture()
  if (kind === 'blank') input.expected_content_text = '\n \u3000'
  if (kind === 'missing_content') input.expected_content_text = null
  if (kind === 'mixed_action') input.direction = '同时修改方向'
  expect(() => parseArcWrite(input, input.expected_source.project_id, input.expected_source.volume_id)).toThrow()
})
