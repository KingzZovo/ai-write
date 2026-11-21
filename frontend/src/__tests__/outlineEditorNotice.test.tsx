import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { OutlineEditor } from '@/components/outline/OutlineEditor'
import { apiFetch } from '@/lib/api'
vi.mock('@/lib/api', () => ({ apiFetch: vi.fn() }))
afterEach(() => { cleanup(); vi.resetAllMocks() })
it('shows protected and unresolved chapters after saving a volume', async () => {
  vi.mocked(apiFetch).mockResolvedValue({ content_json: { raw_text: '作者新卷纲', chapter_summaries: [{ chapter_idx: 1, summary: '旧摘要' }] }, synced_chapters: 1, preserved_chapters: 2, unmaterialized_chapters: 1 })
  render(<OutlineEditor projectId="p" target={{ type: 'volume', volumeId: 'vol', outlineId: 'v', title: '第一卷',
    initialJson: { raw_text: '旧卷纲', chapter_summaries: [{ chapter_idx: 1, summary: '旧摘要' }] } }} onClose={() => {}} />)
  fireEvent.change(screen.getByRole('textbox'), { target: { value: '作者新卷纲' } })
  fireEvent.click(screen.getByText('立即保存'))
  const notice = await screen.findByRole('status')
  expect(notice.textContent).toContain('2 章已有内容')
  expect(notice.textContent).toContain('1 个章号尚无对应章节')
  expect(notice.textContent).toContain('已同步 1 章未编辑的摘要')
  const body = JSON.parse(vi.mocked(apiFetch).mock.calls[0][1]?.body as string)
  expect(body.content_json.chapter_summaries).toEqual([{ chapter_idx: 1, summary: '旧摘要' }])
})

it('keeps unresolved prose editable while retaining the complete save baseline', async () => {
  const original = { volume_id: 'v', volume_idx: 1, chapter_summaries: [
    { chapter_idx: 4, summary: '作者保留的还信安排', _chapter_id: null, _order_unavailable: true },
  ] }
  const edited = { volume_id: 'v', volume_idx: 1, chapter_summaries: [
    { chapter_idx: 9, summary: '作者明确安排在第九章还信' },
  ] }
  vi.mocked(apiFetch).mockResolvedValue({ content_json: { ...edited, chapter_summaries: [
    { ...edited.chapter_summaries[0], _chapter_id: 'chapter-nine' },
  ] } })
  render(<OutlineEditor projectId="order-notice" target={{ type: 'volume', volumeId: 'vol', outlineId: 'v', title: '第一卷',
    initialJson: original }} onClose={() => {}} />)
  const textbox = screen.getByRole('textbox') as HTMLTextAreaElement
  expect(textbox.value).toContain('作者保留的还信安排')
  expect(textbox.value).not.toContain('_chapter_id')
  expect(textbox.value).not.toContain('_order_unavailable')
  expect(screen.getByRole('note').textContent).toContain('暂不用于生成')
  fireEvent.change(textbox, { target: { value: JSON.stringify(edited) } })
  fireEvent.click(screen.getByText('立即保存'))
  await screen.findByText('已保存')
  const body = JSON.parse(vi.mocked(apiFetch).mock.calls[0][1]?.body as string)
  expect(body.expected_content_json).toEqual(original)
  expect(body.content_json).toEqual(edited)
  expect(screen.queryByRole('note')).toBeNull()
  expect(textbox.value).not.toContain('chapter-nine')
})

it('does not warn about old structured plans superseded by author text', () => {
  render(<OutlineEditor projectId="text-notice" target={{ type: 'volume', volumeId: 'vol', outlineId: 'v', title: '第一卷',
    initialJson: { raw_text: '作者当前安排', _author_text_revision: 'revision',
      chapter_summaries: [{ chapter_idx: 4, summary: '旧安排', _chapter_id: null, _order_unavailable: true }] },
  }} onClose={() => {}} />)
  expect(screen.queryByRole('note')).toBeNull()
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('作者当前安排')
})
