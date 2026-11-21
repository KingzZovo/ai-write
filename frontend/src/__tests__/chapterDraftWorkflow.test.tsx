import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { apiFetch, ApiError } from '@/lib/api'
import { useChapterDraft } from '@/components/editor/useChapterDraft'
import { ChapterSaveStatus } from '@/components/editor/ChapterSaveStatus'

vi.mock('@/lib/api', async (original) => ({ ...await original<typeof import('@/lib/api')>(), apiFetch: vi.fn() }))
const api = vi.mocked(apiFetch)
let sequence = 0
let project = ''
const source = (chapter: string, text: string) => ({ project_id: project, chapter_id: chapter, title: '交钱',
  content_text: text, word_count: [...text].length, status: 'completed' })

function Editor({ chapter }: { chapter: string }) {
  const { session, state } = useChapterDraft(project, chapter)
  return <div>
    {session && state && <ChapterSaveStatus key={chapter} session={session} state={state} />}
    <textarea aria-label="编辑稿" value={state?.text ?? ''} readOnly={!session?.canEdit()}
      onChange={(event) => session?.edit(event.target.value)} />
  </div>
}
beforeEach(() => { project = `draft-ui-${++sequence}`; sessionStorage.clear() })
afterEach(() => { cleanup(); vi.resetAllMocks(); vi.restoreAllMocks() })

it('does not show an old chapters late response in the newly selected editor', async () => {
  let finish!: (value: unknown) => void
  api.mockImplementation((path) => path.includes('/a/')
    ? new Promise((resolve) => { finish = resolve }) : Promise.resolve(source('b', '乙章保存稿。')))
  const view = render(<Editor chapter="a" />)
  const signal = api.mock.calls[0][1]?.signal
  view.rerender(<Editor chapter="b" />)
  await waitFor(() => expect((screen.getByLabelText('编辑稿') as HTMLTextAreaElement).value).toBe('乙章保存稿。'))
  await act(async () => { finish(source('a', '甲章迟到稿。')) })
  expect(signal?.aborted).toBe(true)
  expect((screen.getByLabelText('编辑稿') as HTMLTextAreaElement).value).toBe('乙章保存稿。')
})

it('keeps an unsaved chapter draft when switching away before the debounce fires', async () => {
  api.mockImplementation(async (path) => path.includes('/a/') ? source('a', '甲章原稿。') : source('b', '乙章原稿。'))
  const view = render(<Editor chapter="a" />)
  await screen.findByText('正文已保存')
  fireEvent.change(screen.getByLabelText('编辑稿'), { target: { value: '甲章未保存稿。' } })
  view.rerender(<Editor chapter="b" />)
  await waitFor(() => expect((screen.getByLabelText('编辑稿') as HTMLTextAreaElement).value).toBe('乙章原稿。'))
  view.rerender(<Editor chapter="a" />)
  await screen.findByLabelText('当前保存稿对照')
  expect((screen.getByLabelText('编辑稿') as HTMLTextAreaElement).value).toBe('甲章未保存稿。')
  expect((screen.getByLabelText('当前保存稿对照') as HTMLTextAreaElement).value).toBe('甲章原稿。')
  expect(api.mock.calls.some(([, options]) => options?.method === 'PUT')).toBe(false)
})

it('shows the whole saved manuscript for an explicit conflict decision and sends that exact baseline', async () => {
  let canonical = '原来保存的完整正文。'
  let conflict = true
  api.mockImplementation(async (_path, options) => {
    if (options?.method !== 'PUT') return source('a', canonical)
    if (conflict) {
      canonical = '别人后来保存的全文。'.repeat(2000)
      conflict = false
      throw new ApiError('已变化', 409, 'chapter_edit_conflict')
    }
    const body = JSON.parse(options.body as string)
    expect(body.expected_content_text).toBe(canonical)
    canonical = body.content_text
    return { status: 'saved', manuscript: source('a', canonical), changed: true, backup_version_id: null }
  })
  vi.spyOn(window, 'confirm').mockReturnValue(true)
  render(<Editor chapter="a" />)
  await screen.findByText('正文已保存')
  fireEvent.change(screen.getByLabelText('编辑稿'), { target: { value: '我正在写的全文。' } })
  fireEvent.click(screen.getByText('立即保存'))
  await screen.findByRole('alert')
  expect((screen.getByLabelText('编辑稿') as HTMLTextAreaElement).value).toBe('我正在写的全文。')
  fireEvent.click(screen.getByText('读取保存稿并核对'))
  await screen.findByLabelText('当前保存稿对照')
  expect((screen.getByLabelText('当前保存稿对照') as HTMLTextAreaElement).value).toBe(canonical)
  fireEvent.click(screen.getByText('确认保存编辑区稿件'))
  await screen.findByText('正文已保存')
  expect(canonical).toBe('我正在写的全文。')
})

it('does not report saved from a malformed acknowledgement', async () => {
  api.mockImplementation(async (_path, options) => options?.method === 'PUT' ? { status: 'saved' } : source('a', '原稿。'))
  render(<Editor chapter="a" />)
  await screen.findByText('正文已保存')
  fireEvent.change(screen.getByLabelText('编辑稿'), { target: { value: '需要保留的新稿。' } })
  fireEvent.click(screen.getByText('立即保存'))
  await screen.findByRole('alert')
  expect(screen.queryByText('正文已保存')).toBeNull()
  expect((screen.getByLabelText('编辑稿') as HTMLTextAreaElement).value).toBe('需要保留的新稿。')
  expect((screen.getByText('立即保存') as HTMLButtonElement).disabled).toBe(true)
})

it('keeps later typing when an earlier save is acknowledged', async () => {
  let finish!: (value: unknown) => void
  api.mockImplementation((_path, options) => options?.method === 'PUT'
    ? new Promise((resolve) => { finish = resolve }) : Promise.resolve(source('a', '原稿。')))
  render(<Editor chapter="a" />)
  await screen.findByText('正文已保存')
  fireEvent.change(screen.getByLabelText('编辑稿'), { target: { value: '已经送出的修改。' } })
  fireEvent.click(screen.getByText('立即保存'))
  await screen.findByText('正在保存…')
  fireEvent.change(screen.getByLabelText('编辑稿'), { target: { value: '请求期间又写了一句。' } })
  await act(async () => { finish({ status: 'saved', manuscript: source('a', '已经送出的修改。'), changed: true, backup_version_id: null }) })
  expect((screen.getByLabelText('编辑稿') as HTMLTextAreaElement).value).toBe('请求期间又写了一句。')
  expect(screen.getByText('尚有未保存修改')).toBeTruthy()
})
