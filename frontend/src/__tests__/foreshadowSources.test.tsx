import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { apiFetch, ApiError } from '@/lib/api'
import { ForeshadowSourceForm } from '@/components/panels/ForeshadowSourceForm'
import { ForeshadowPanel } from '@/components/panels/ForeshadowPanel'
import { Foreshadow } from '@/lib/foreshadowWrites'
import { SourceContext, SourceLink } from '@/lib/foreshadowSources'

vi.mock('@/lib/api', async importOriginal => ({ ...await importOriginal<typeof import('@/lib/api')>(), apiFetch: vi.fn() }))
vi.mock('@/components/panels/ForeshadowReviewList', () => ({ ForeshadowReviewList: () => null }))
vi.mock('@/components/panels/ForeshadowPlanList', () => ({ ForeshadowPlanList: () => null }))
vi.mock('@/lib/i18n/I18nProvider', () => ({ useT: () => (key: string) => key }))

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.mocked(apiFetch).mockReset(); sessionStorage.clear() })
let count = 0
function example() {
  const projectId = `source-project-${++count}`
  const foreshadow: Foreshadow = { id: 'clue', project_id: projectId, type: 'mystery', description: '存根的去向',
    planted_chapter: 0, resolve_conditions_json: [], narrative_proximity: 0, status: 'planted', resolved_chapter: null,
    version: 'a'.repeat(64), source_state: 'unknown' }
  return { projectId, foreshadow, defaultChapterId: 'first', onDone: vi.fn(), onClose: vi.fn() }
}
function context(chapterId = 'first', overrides: Partial<SourceContext> = {}): SourceContext {
  return { foreshadow_id: 'clue', description: '存根的去向', type: 'mystery', record_present: true, record_version: 'a'.repeat(64),
    latest_link: null, history: [], has_more: false,
    preview: { chapter_id: chapterId, title: chapterId === 'first' ? '河岸' : '渡口', content: '陈伯把寄信的存根放在桌上。',
      source_snapshot: { volume_idx: 1, chapter_idx: chapterId === 'first' ? 1 : 2, global_idx: chapterId === 'first' ? 11 : 12 }, source_version: 'b'.repeat(64) },
    ...overrides }
}
function link(overrides: Partial<SourceLink> = {}): SourceLink {
  return { id: 'previous-link', revision: 1, operation: 'link', chapter_id: 'first',
    source_snapshot: { volume_idx: 1, chapter_idx: 1, global_idx: 11 }, record_snapshot: { description: '当时的存根记录' },
    evidence: '寄信的存根', note: '作者核对说明', created_by: '作者甲', created_at: '2026-10-08T12:00:00Z', ...overrides }
}
const chapters = { chapters: [
  { id: 'first', title: '河岸', volume_idx: 1, chapter_idx: 1, volume_title: '返乡' },
  { id: 'second', title: '渡口', volume_idx: 1, chapter_idx: 2, volume_title: '返乡' },
], has_more: false }
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(yes => { resolve = yes })
  return { promise, resolve }
}
function success(options: RequestInit) {
  return { id: (options.headers as Record<string, string>)['Idempotency-Key'], operation: options.method === 'DELETE' ? 'unlink' : 'link', is_latest: true }
}

it('opens the source form from an existing clue card', async () => {
  const props = example()
  vi.mocked(apiFetch).mockImplementation(async path => path.includes('source-chapters') ? chapters
    : path.includes('/source?') ? context() : { foreshadows: [props.foreshadow], total: 1 })
  render(<ForeshadowPanel projectId={props.projectId} chapterId="first" />)
  fireEvent.click(await screen.findByText('核对正文来源'))
  expect(await screen.findByLabelText('关联章节的已保存正文')).toHaveProperty('value', context().preview!.content)
})

it('keeps the full saved prose and quotes the actual selected Unicode text', async () => {
  const props = example(), sent: RequestInit[] = []
  const body = '他们沿河回村。'.repeat(800) + '\r\n😀陈伯把寄信的存根放在桌上。\r\n他没有解释。'
  const saved = context(); saved.preview!.content = body
  vi.mocked(apiFetch).mockImplementation(async (path, options) => {
    if (options?.method) { sent.push(options); return success(options) }
    return path.includes('source-chapters') ? chapters : saved
  })
  render(<ForeshadowSourceForm {...props} />)
  const prose = await screen.findByLabelText('关联章节的已保存正文') as HTMLTextAreaElement
  expect(prose.value).toBe(body.replace(/\r\n/g, '\n'))
  const quote = '😀陈伯把寄信的存根放在桌上。\r\n他没有解释。'
  prose.setSelectionRange(prose.value.indexOf('😀'), prose.value.length)
  fireEvent.click(screen.getByText('引用所选文字'))
  expect(screen.getByLabelText('引用原句')).toHaveProperty('value', quote.replace(/\r\n/g, '\n'))
  fireEvent.change(screen.getByLabelText('关联说明'), { target: { value: '人物反复收起这张存根，后文需回看。' } })
  fireEvent.click(screen.getByText('确认原句并关联'))
  await waitFor(() => expect(props.onDone).toHaveBeenCalledOnce())
  expect(JSON.parse(sent[0].body as string)).toEqual({ expected_link_id: null, chapter_id: 'first',
    source_version: 'b'.repeat(64), evidence: quote, note: '人物反复收起这张存根，后文需回看。' })
  expect((sent[0].headers as Record<string, string>)['If-Match']).toBe('a'.repeat(64))
})

it('retries an uncertain request after remount with its original body and identity', async () => {
  const props = example(), sent: RequestInit[] = []
  vi.mocked(apiFetch).mockImplementation(async (path, options) => {
    if (options?.method) {
      sent.push(options)
      if (sent.length === 1) throw new ApiError('保存确认尚未返回', 503)
      return success(options)
    }
    return path.includes('source-chapters') ? chapters : context('first', {
      // A response lost after commit may already be visible on the next GET.
      latest_link: sent.length ? link({ id: (sent[0].headers as Record<string, string>)['Idempotency-Key'] }) : null,
    })
  })
  const first = render(<ForeshadowSourceForm {...props} />)
  await screen.findByLabelText('关联章节的已保存正文')
  fireEvent.change(screen.getByLabelText('引用原句'), { target: { value: '寄信的存根' } })
  fireEvent.click(screen.getByText('确认原句并关联'))
  expect(await screen.findByRole('alert')).toHaveProperty('textContent', '保存确认尚未返回')
  expect(screen.getByLabelText('引用原句')).toHaveProperty('readOnly', true)
  first.unmount()
  render(<ForeshadowSourceForm {...props} />)
  expect(screen.getByLabelText('引用原句')).toHaveProperty('value', '寄信的存根')
  fireEvent.click(screen.getByText('重试确认关联操作'))
  await waitFor(() => expect(props.onDone).toHaveBeenCalledOnce())
  expect(sent).toHaveLength(2)
  expect(sent[1].body).toBe(sent[0].body)
  expect(sent[1].headers).toEqual(sent[0].headers)
})

it('ignores a late chapter preview after a different source is selected', async () => {
  const props = example(), old = deferred<SourceContext>()
  const second = context('second'); second.preview!.content = '渡口这一章的完整正文。'
  vi.mocked(apiFetch).mockImplementation(path => Promise.resolve(path.includes('source-chapters') ? chapters
    : path.includes('chapter_id=second') ? second : old.promise))
  render(<ForeshadowSourceForm {...props} />)
  await waitFor(() => expect(screen.getByLabelText('来源章节')).toHaveProperty('disabled', false))
  fireEvent.change(screen.getByLabelText('来源章节'), { target: { value: 'second' } })
  expect(await screen.findByLabelText('关联章节的已保存正文')).toHaveProperty('value', second.preview!.content)
  await act(async () => old.resolve(context()))
  expect(screen.getByLabelText('关联章节的已保存正文')).toHaveProperty('value', second.preview!.content)
})

it('preserves a conflict draft and requires a fresh review before using a new baseline', async () => {
  const props = example(), sent: RequestInit[] = []
  const next = context('first', { record_version: 'c'.repeat(64), description: '另一位作者改过的登记', latest_link: link() })
  next.preview!.source_version = 'd'.repeat(64)
  vi.mocked(apiFetch).mockImplementation(async (path, options) => {
    if (options?.method) {
      sent.push(options)
      if (sent.length === 1) throw new ApiError('关联已有新版本，请核对', 409, 'link_changed')
      return success(options)
    }
    return path.includes('source-chapters') ? chapters : sent.length ? next : context()
  })
  render(<ForeshadowSourceForm {...props} />)
  await screen.findByLabelText('关联章节的已保存正文')
  fireEvent.change(screen.getByLabelText('引用原句'), { target: { value: '寄信的存根' } })
  fireEvent.click(screen.getByText('确认原句并关联'))
  await screen.findByText('关联已有新版本，请核对')
  expect(screen.getByLabelText('引用原句')).toHaveProperty('value', '寄信的存根')
  expect(screen.getByText('确认原句并关联')).toHaveProperty('disabled', true)
  fireEvent.click(screen.getByText('重新读取并核对保存稿'))
  await screen.findByText('伏笔：另一位作者改过的登记')
  fireEvent.click(screen.getByText('确认原句并关联'))
  await waitFor(() => expect(props.onDone).toHaveBeenCalledOnce())
  expect((sent[1].headers as Record<string, string>)['If-Match']).toBe('c'.repeat(64))
  expect(JSON.parse(sent[1].body as string).expected_link_id).toBe('previous-link')
  expect(JSON.parse(sent[1].body as string).source_version).toBe('d'.repeat(64))
})

it('does not report completion or clear a new project when an old save returns late', async () => {
  const first = example(), second = example(), pending = deferred<unknown>()
  let oldRequest: RequestInit | undefined
  vi.mocked(apiFetch).mockImplementation(async (path, options) => {
    if (options?.method) { oldRequest = options; return pending.promise }
    return path.includes('source-chapters') ? chapters : context()
  })
  const view = render(<ForeshadowSourceForm {...first} />)
  await screen.findByLabelText('关联章节的已保存正文')
  fireEvent.change(screen.getByLabelText('引用原句'), { target: { value: '寄信的存根' } })
  fireEvent.click(screen.getByText('确认原句并关联'))
  view.rerender(<ForeshadowSourceForm {...second} />)
  await screen.findByLabelText('关联章节的已保存正文')
  fireEvent.change(screen.getByLabelText('引用原句'), { target: { value: '另一本书的新输入' } })
  await act(async () => pending.resolve(success(oldRequest!)))
  expect(first.onDone).not.toHaveBeenCalled()
  expect(second.onDone).not.toHaveBeenCalled()
  expect(screen.getByLabelText('引用原句')).toHaveProperty('value', '另一本书的新输入')
})

it('keeps input when an acknowledged unlink has already been superseded', async () => {
  const props = example(), sent: RequestInit[] = []
  const oldLink = link()
  vi.mocked(apiFetch).mockImplementation(async (path, options) => {
    if (options?.method) { sent.push(options); return { ...success(options), is_latest: false } }
    return path.includes('source-chapters') ? chapters : context('first', { latest_link: oldLink, history: [oldLink] })
  })
  render(<ForeshadowSourceForm {...props} />)
  await screen.findByLabelText('关联章节的已保存正文')
  fireEvent.change(screen.getByLabelText('引用原句'), { target: { value: '仍要保留的原句' } })
  fireEvent.click(screen.getByText('解除正文关联'))
  expect(await screen.findByRole('alert')).toHaveProperty('textContent', expect.stringContaining('随后关联又有变化'))
  expect(props.onDone).not.toHaveBeenCalled()
  expect(screen.getByLabelText('引用原句')).toHaveProperty('value', '仍要保留的原句')
  expect(sent[0].method).toBe('DELETE')
  expect(JSON.parse(sent[0].body as string)).toEqual({ expected_link_id: 'previous-link', note: '' })
})

it('paginates association history while preserving the latest decision as the write baseline', async () => {
  const props = example(), newest = link({ id: 'newest', revision: 22 }), oldest = link({ id: 'old', revision: 1, note: '更早的核对说明' })
  vi.mocked(apiFetch).mockImplementation(async path => path.includes('source-chapters') ? chapters : context('first', {
    latest_link: newest, history: path.includes('offset=20') ? [oldest] : [newest], has_more: !path.includes('offset=20'),
  }))
  render(<ForeshadowSourceForm {...props} />)
  fireEvent.click(await screen.findByText('下一页关联记录'))
  expect(await screen.findByText('更早的核对说明')).toBeTruthy()
  expect(screen.getByText('下一页关联记录')).toHaveProperty('disabled', true)
})

it('lets the author expand an ambiguous quote without discarding the current preview', async () => {
  const props = example(), sent: RequestInit[] = []
  vi.mocked(apiFetch).mockImplementation(async (path, options) => {
    if (options?.method) {
      sent.push(options)
      if (sent.length === 1) throw new ApiError('请扩大引用范围', 409, 'quote_ambiguous')
      return success(options)
    }
    return path.includes('source-chapters') ? chapters : context()
  })
  render(<ForeshadowSourceForm {...props} />)
  await screen.findByLabelText('关联章节的已保存正文')
  fireEvent.change(screen.getByLabelText('引用原句'), { target: { value: '陈伯' } })
  fireEvent.click(screen.getByText('确认原句并关联'))
  await screen.findByText('请扩大引用范围')
  fireEvent.change(screen.getByLabelText('引用原句'), { target: { value: '陈伯把寄信的存根放在桌上。' } })
  expect(screen.getByText('确认原句并关联')).toHaveProperty('disabled', false)
  fireEvent.click(screen.getByText('确认原句并关联'))
  await waitFor(() => expect(props.onDone).toHaveBeenCalledOnce())
  expect(sent).toHaveLength(2)
})

it('shows retained history after clue deletion and disables fresh mutations', async () => {
  const props = example(), retained = link()
  vi.mocked(apiFetch).mockImplementation(async path => path.includes('source-chapters') ? chapters : context('', {
    record_present: false, record_version: null, preview: null, latest_link: retained, history: [retained],
  }))
  render(<ForeshadowSourceForm {...props} defaultChapterId="" />)
  await screen.findByText('伏笔记录已删除，保留的关联历史可继续查看。')
  expect(screen.getByText('当时的登记：当时的存根记录')).toBeTruthy()
  expect(screen.getByText('确认原句并关联')).toHaveProperty('disabled', true)
  expect(screen.getByText('解除正文关联')).toHaveProperty('disabled', true)
})

it('can open history and unlink when the previously selected chapter is gone', async () => {
  const props = example(), retained = link()
  vi.mocked(apiFetch).mockImplementation(async (path, options) => {
    if (options?.method) return success(options)
    if (path.includes('source-chapters')) return chapters
    if (path.includes('chapter_id=')) throw new ApiError('来源章节不可用', 409, 'chapter_unavailable')
    return context('', { preview: null, latest_link: retained, history: [retained] })
  })
  render(<ForeshadowSourceForm {...props} />)
  expect(await screen.findByRole('alert')).toHaveProperty('textContent', expect.stringContaining('来源章节不可用'))
  fireEvent.change(screen.getByLabelText('来源章节'), { target: { value: '' } })
  fireEvent.click(await screen.findByText('解除正文关联'))
  await waitFor(() => expect(props.onDone).toHaveBeenCalledOnce())
})
