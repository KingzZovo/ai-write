// Exact first receipt captured by the offline SQLite/ASGI backend test.
// All identities and manuscripts are synthetic; no live service was called.
import raw from './arcProgress.json'
import type { ArcProgressReceipt, ArcProgressView, ArcProgressWrite, ArcHistoryDetail } from '@/lib/arcProgress'
import type { ManuscriptSource } from '@/lib/chapterManuscripts'

export function arcFixture() {
  const value = structuredClone(raw)
  return { before: value.before as ArcProgressView, after: value.after as ArcProgressView,
    input: value.input as ArcProgressWrite, receipt: value.receipt as ArcProgressReceipt, detail: value.detail as ArcHistoryDetail }
}
export function arcManuscript(): ManuscriptSource {
  const { input } = arcFixture()
  return { project_id: input.expected_source.project_id, chapter_id: input.chapter_id!, title: input.expected_title!,
    content_text: input.expected_content_text!, word_count: [...input.expected_content_text!].length, status: 'draft' }
}
