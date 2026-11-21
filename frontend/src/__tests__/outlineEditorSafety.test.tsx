import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { OutlineEditor, type OutlineEditTarget } from '@/components/outline/OutlineEditor'
import { apiFetch } from '@/lib/api'
vi.mock('@/lib/api', () => ({ apiFetch: vi.fn() }))
const api = vi.mocked(apiFetch)
const book: OutlineEditTarget = { type: 'book', outlineId: 'b', title: '全书', initialJson: { raw_text: '原文', volume_plan: [{ idx: 1 }] } }
const body = (idx: number) => JSON.parse(api.mock.calls[idx][1]?.body as string)
const edit = (text: string) => fireEvent.change(screen.getByRole('textbox'), { target: { value: text } })
const save = () => fireEvent.click(screen.getByText('立即保存'))
beforeEach(() => { sessionStorage.clear(); vi.useFakeTimers() })
afterEach(() => { cleanup(); vi.resetAllMocks(); vi.useRealTimers(); sessionStorage.clear() })

it('serializes manual/autosave and uses the last saved snapshot for newer typing', async () => {
  let resolve!: (value: unknown) => void
  api.mockImplementationOnce(() => new Promise(r => { resolve = r }))
    .mockResolvedValueOnce({ content_json: { ...book.initialJson, raw_text: '第二稿' } })
  render(<OutlineEditor projectId="p" target={book} onClose={() => {}} />)
  edit('第一稿'); save()
  fireEvent.keyDown(window, { key: 's', ctrlKey: true })
  edit('第二稿')
  await act(async () => { await vi.advanceTimersByTimeAsync(3000) })
  expect(api).toHaveBeenCalledTimes(1)
  await act(async () => { resolve({ content_json: { ...book.initialJson, raw_text: '第一稿' } }) })
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('第二稿')
  expect(screen.getByText('未保存')).toBeTruthy()
  await act(async () => { await vi.advanceTimersByTimeAsync(3000) })
  expect(api).toHaveBeenCalledTimes(2)
  expect(body(0).expected_content_json).toEqual(book.initialJson)
  expect(body(1).expected_content_json.raw_text).toBe('第一稿')
  expect(body(1).content_json.volume_plan).toEqual([{ idx: 1 }])
})

it('ignores late save responses after target changes and restores the unsaved draft', async () => {
  let resolve!: (value: unknown) => void
  api.mockImplementationOnce(() => new Promise(r => { resolve = r }))
  const onSaved = vi.fn()
  const view = render(<OutlineEditor projectId="p" target={book} onClose={() => {}} onSaved={onSaved} />)
  edit('未完成编辑'); save()
  view.rerender(<OutlineEditor projectId="p" target={{ ...book, outlineId: 'other', initialJson: { raw_text: '另一稿' } }} onClose={() => {}} onSaved={onSaved} />)
  await act(async () => { resolve({ content_json: { raw_text: '未完成编辑' } }) })
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('另一稿')
  expect(onSaved).not.toHaveBeenCalled()
  view.rerender(<OutlineEditor projectId="p" target={book} onClose={() => {}} />)
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('未完成编辑')
  expect(screen.getByText('未保存')).toBeTruthy()
})

it('uses expanded structure as the next save baseline, not the original stub', async () => {
  const expanded = { summary: '完整场景', key_events: ['送信', '归来'] }
  api.mockResolvedValueOnce({ outline_json: expanded }).mockResolvedValueOnce({ outline_json: { ...expanded, summary: '作者修订' } })
  render(<OutlineEditor projectId="p" target={{ type: 'chapter', chapterId: 'c', title: '第一章', initialJson: { summary: '原摘要' } }} onClose={() => {}} />)
  fireEvent.click(screen.getByText('AI 扩写本章大纲'))
  await act(async () => {})
  edit(JSON.stringify({ ...expanded, summary: '作者修订' })); save()
  await act(async () => {})
  expect(body(1).expected_outline_json).toEqual(expanded)
  expect(body(1).outline_json).toEqual({ ...expanded, summary: '作者修订' })
})

it('blocks expansion while there is an unsaved draft', () => {
  render(<OutlineEditor projectId="p" target={{ type: 'chapter', chapterId: 'c', title: '第一章', initialJson: { raw_text: '原文' } }} onClose={() => {}} />)
  edit('作者新稿')
  expect((screen.getByText('AI 扩写本章大纲') as HTMLButtonElement).disabled).toBe(true)
  expect(api).not.toHaveBeenCalled()
})

it('retains draft on conflict without automatic retry or success callback', async () => {
  api.mockRejectedValue(new Error('409'))
  const onSaved = vi.fn()
  render(<OutlineEditor projectId="p" target={book} onClose={() => {}} onSaved={onSaved} />)
  edit('冲突草稿'); save()
  await act(async () => {})
  await act(async () => { await vi.advanceTimersByTimeAsync(9000) })
  expect(api).toHaveBeenCalledTimes(1)
  expect(onSaved).not.toHaveBeenCalled()
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('冲突草稿')
  expect(screen.getByText('保存失败（请核对后重试）')).toBeTruthy()
})

it('rejects malformed structured editing before a request', async () => {
  render(<OutlineEditor projectId="p" target={{ type: 'chapter', chapterId: 'c', title: '章', initialJson: { summary: '原摘要' } }} onClose={() => {}} />)
  edit('{invalid'); save()
  await act(async () => {})
  expect(api).not.toHaveBeenCalled()
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('{invalid')
})

it('keeps the editor open if saving on close fails', async () => {
  api.mockRejectedValue(new Error('offline'))
  const onClose = vi.fn()
  render(<OutlineEditor projectId="p" target={book} onClose={onClose} />)
  edit('不能丢的草稿')
  fireEvent.click(screen.getByText('关闭'))
  await act(async () => {})
  expect(onClose).not.toHaveBeenCalled()
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('不能丢的草稿')
})

it('blocks typing during reload and preserves the draft if reload fails', async () => {
  let reject!: (error: Error) => void
  vi.spyOn(window, 'confirm').mockReturnValue(true)
  api.mockImplementationOnce(() => new Promise((_resolve, r) => { reject = r }))
  render(<OutlineEditor projectId="p" target={book} onClose={() => {}} />)
  edit('本地新稿')
  fireEvent.click(screen.getByText('重新加载'))
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).disabled).toBe(true)
  await act(async () => { reject(new Error('offline')) })
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('本地新稿')
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).disabled).toBe(false)
  vi.restoreAllMocks()
})

it('does not treat a response without saved content as proof of a save', async () => {
  api.mockResolvedValue({})
  const onSaved = vi.fn()
  render(<OutlineEditor projectId="p" target={book} onClose={() => {}} onSaved={onSaved} />)
  edit('需要核对的稿件'); save()
  await act(async () => {})
  expect(onSaved).not.toHaveBeenCalled()
  expect(screen.getByText('保存失败（请核对后重试）')).toBeTruthy()
})
