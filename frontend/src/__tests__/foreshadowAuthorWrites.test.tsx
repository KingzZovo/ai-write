import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ForeshadowPanel } from '@/components/panels/ForeshadowPanel'
import { ForeshadowCreateForm, ForeshadowEditForm } from '@/components/panels/ForeshadowAuthorForms'
import { apiFetch, ApiError } from '@/lib/api'
import { Foreshadow, draftKey, readForeshadowDraft } from '@/lib/foreshadowWrites'

vi.mock('@/lib/api', async importOriginal => ({ ...await importOriginal<typeof import('@/lib/api')>(), apiFetch: vi.fn() }))
vi.mock('@/components/panels/ForeshadowReviewList', () => ({ ForeshadowReviewList: () => null }))
vi.mock('@/lib/i18n/I18nProvider', () => ({ useT: () => (key: string) => ({
  'common.edit': '编辑', 'common.delete': '删除', 'foreshadow.action.resolve': '标记回收',
}[key] || key) }))

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.mocked(apiFetch).mockReset(); sessionStorage.clear() })
let counter = 0
function example() {
  const project = `author-test-${++counter}`
  const record: Foreshadow = { id: 'clue', project_id: project, description: '寄信人的旧印记', type: 'mystery',
    planted_chapter: 3, resolve_conditions_json: ['核对笔迹'], narrative_proximity: 0,
    status: 'planted', resolved_chapter: null, version: 'a'.repeat(64) }
  return { project, record }
}
function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (error: Error) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

describe('author edit preservation', () => {
  it('keeps a failed draft and retries with the same baseline and request ID', async () => {
    const { project, record } = example()
    const calls: RequestInit[] = []
    let current = record
    vi.mocked(apiFetch).mockImplementation(async (_path, options) => {
      if (options?.method === 'PUT') {
        calls.push(options)
        if (calls.length === 1) throw new ApiError('同步尚未完成', 503, 'foreshadow_sync_pending')
        current = { ...record, description: '补写寄信动机', version: 'b'.repeat(64) }
        return current
      }
      return { foreshadows: [current], total: 1 }
    })
    render(<ForeshadowPanel projectId={project} />)
    fireEvent.click(await screen.findByText('编辑'))
    fireEvent.change(screen.getByLabelText('伏笔描述'), { target: { value: '补写寄信动机' } })
    fireEvent.click(screen.getByText('保存'))
    expect(await screen.findByRole('alert')).toHaveProperty('textContent', '同步尚未完成')
    expect(screen.getByLabelText('伏笔描述')).toHaveProperty('value', '补写寄信动机')
    fireEvent.click(screen.getByText('保存'))
    await waitFor(() => expect(screen.queryByLabelText('伏笔描述')).toBeNull())
    expect(calls).toHaveLength(2)
    expect(calls[0].headers).toEqual(calls[1].headers)
    expect((calls[0].headers as Record<string, string>)['If-Match']).toBe(record.version)
    expect(await screen.findByText('补写寄信动机')).toBeTruthy()
  })

  it('keeps the editor through refresh failure and never claims there are no clues', async () => {
    const { project, record } = example()
    vi.mocked(apiFetch).mockResolvedValueOnce({ foreshadows: [record], total: 1 })
      .mockRejectedValueOnce(new Error('offline'))
    render(<ForeshadowPanel projectId={project} />)
    fireEvent.click(await screen.findByText('编辑'))
    fireEvent.change(screen.getByLabelText('伏笔描述'), { target: { value: '还没保存的草稿' } })
    fireEvent.click(screen.getByText('刷新伏笔列表'))
    expect(await screen.findByRole('alert')).toHaveProperty('textContent', expect.stringContaining('读取失败'))
    expect(screen.getByLabelText('伏笔描述')).toHaveProperty('value', '还没保存的草稿')
    expect(screen.getByText(record.description)).toBeTruthy()
    expect(screen.queryByText(/暂无伏笔/)).toBeNull()
  })

  it('requires an explicit check of a refreshed record before rebasing a retained draft', async () => {
    const { project, record } = example()
    const latest = { ...record, description: '另一位作者的新记录', version: 'b'.repeat(64) }
    const done = vi.fn()
    vi.mocked(apiFetch).mockResolvedValue({ ...latest, description: '我的未保存稿', version: 'c'.repeat(64) })
    const view = render(<ForeshadowEditForm foreshadow={record} current={record} projectId={project} onDone={done} onCancel={vi.fn()} onRefresh={vi.fn()} />)
    fireEvent.change(screen.getByLabelText('伏笔描述'), { target: { value: '我的未保存稿' } })
    view.rerender(<ForeshadowEditForm foreshadow={record} current={latest} projectId={project} onDone={done} onCancel={vi.fn()} onRefresh={vi.fn()} />)
    expect(screen.getByText('保存')).toHaveProperty('disabled', true)
    expect(screen.getByLabelText('伏笔描述')).toHaveProperty('value', '我的未保存稿')
    fireEvent.click(screen.getByText('已核对，保留我的草稿继续编辑'))
    fireEvent.click(screen.getByText('保存'))
    await waitFor(() => expect(done).toHaveBeenCalledOnce())
    const options = vi.mocked(apiFetch).mock.calls[0][1]!
    expect((options.headers as Record<string, string>)['If-Match']).toBe(latest.version)
    expect(JSON.parse(options.body as string).description).toBe('我的未保存稿')
  })

  it('does not clear a newer draft when an older unmounted save finishes', async () => {
    const { project, record } = example()
    const pending = deferred<Foreshadow>()
    const done = vi.fn()
    vi.mocked(apiFetch).mockReturnValue(pending.promise)
    const props = { foreshadow: record, current: record, projectId: project, onDone: done, onCancel: vi.fn(), onRefresh: vi.fn() }
    const first = render(<ForeshadowEditForm {...props} />)
    fireEvent.change(screen.getByLabelText('伏笔描述'), { target: { value: '先提交的稿' } })
    fireEvent.click(screen.getByText('保存'))
    first.unmount()
    render(<ForeshadowEditForm {...props} />)
    fireEvent.change(screen.getByLabelText('伏笔描述'), { target: { value: '重新打开后的新稿' } })
    await act(async () => pending.resolve({ ...record, description: '先提交的稿', version: 'b'.repeat(64) }))
    expect(done).not.toHaveBeenCalled()
    expect(screen.getByLabelText('伏笔描述')).toHaveProperty('value', '重新打开后的新稿')
    expect(readForeshadowDraft(draftKey(project, 'edit:clue'))).toMatchObject({ description: '重新打开后的新稿' })
  })

  it('does not flatten untouched structured conditions while editing the description', async () => {
    const { project, record } = example()
    record.resolve_conditions_json = { question: '寄信人是谁', evidence: { handwriting: '旧账簿' } }
    vi.mocked(apiFetch).mockResolvedValue({ ...record, description: '补写描述', version: 'b'.repeat(64) })
    render(<ForeshadowEditForm foreshadow={record} current={record} projectId={project} onDone={vi.fn()} onCancel={vi.fn()} onRefresh={vi.fn()} />)
    fireEvent.change(screen.getByLabelText('伏笔描述'), { target: { value: '补写描述' } })
    fireEvent.click(screen.getByText('保存'))
    await waitFor(() => expect(apiFetch).toHaveBeenCalledOnce())
    expect(JSON.parse(vi.mocked(apiFetch).mock.calls[0][1]!.body as string)).toEqual({ description: '补写描述', type: 'mystery' })
  })
})

it('ignores a slow response from the previous project', async () => {
  const first = example(), second = example(), pending = deferred<unknown>()
  second.record.description = '另一部作品的线索'
  vi.mocked(apiFetch).mockImplementation(path => path.includes(first.project) ? pending.promise : Promise.resolve({ foreshadows: [second.record], total: 1 }))
  const view = render(<ForeshadowPanel projectId={first.project} />)
  view.rerender(<ForeshadowPanel projectId={second.project} />)
  expect(await screen.findByText(second.record.description)).toBeTruthy()
  await act(async () => pending.resolve({ foreshadows: [first.record], total: 1 }))
  expect(screen.queryByText(first.record.description)).toBeNull()
  expect(screen.getByText(second.record.description)).toBeTruthy()
})

it('preserves an uncertain create across remount and retries one identity without changing the body', async () => {
  const { project, record } = example()
  const posts: RequestInit[] = []
  vi.mocked(apiFetch).mockImplementation(async (_path, options) => {
    if (options?.method === 'POST') {
      posts.push(options)
      if (posts.length === 1) throw new ApiError('尚未获得创建确认', 503)
      return { ...record, id: (options.headers as Record<string, string>)['Idempotency-Key'] }
    }
    return { foreshadows: [], total: 0 }
  })
  const first = render(<ForeshadowPanel projectId={project} />)
  fireEvent.click(screen.getByText('+ 手动添加'))
  fireEvent.change(screen.getByLabelText('伏笔描述'), { target: { value: '需要保留的创建输入' } })
  fireEvent.click(screen.getByText('创建伏笔'))
  expect(await screen.findByRole('alert')).toHaveProperty('textContent', '尚未获得创建确认')
  expect(screen.getByLabelText('伏笔描述')).toHaveProperty('readOnly', true)
  first.unmount()
  render(<ForeshadowPanel projectId={project} />)
  fireEvent.click(screen.getByText('+ 手动添加'))
  expect(screen.getByLabelText('伏笔描述')).toHaveProperty('value', '需要保留的创建输入')
  fireEvent.click(screen.getByText('重试确认创建'))
  await waitFor(() => expect(screen.queryByLabelText('伏笔描述')).toBeNull())
  expect(posts).toHaveLength(2)
  expect(posts[0].headers).toEqual(posts[1].headers)
  expect(posts[0].body).toBe(posts[1].body)
})

it('keeps the reopened creation identity when the old component receives a late success', async () => {
  const { project, record } = example()
  const pending = deferred<Foreshadow>()
  const done = vi.fn()
  const posts: RequestInit[] = []
  vi.mocked(apiFetch).mockImplementation(async (_path, options) => {
    posts.push(options!)
    if (posts.length === 1) return pending.promise
    return { ...record, id: (options!.headers as Record<string, string>)['Idempotency-Key'] }
  })
  const first = render(<ForeshadowCreateForm projectId={project} onCreated={done} />)
  fireEvent.change(screen.getByLabelText('伏笔描述'), { target: { value: '迟到确认的创建稿' } })
  fireEvent.click(screen.getByText('创建伏笔'))
  first.unmount()
  render(<ForeshadowCreateForm projectId={project} onCreated={done} />)
  await act(async () => pending.resolve({ ...record, id: (posts[0].headers as Record<string, string>)['Idempotency-Key'] }))
  expect(done).not.toHaveBeenCalled()
  fireEvent.click(screen.getByText('重试确认创建'))
  await waitFor(() => expect(done).toHaveBeenCalledOnce())
  expect(posts).toHaveLength(2)
  expect(posts[0].headers).toEqual(posts[1].headers)
})

it.each(['resolve', 'delete'] as const)('keeps a failed %s action until a committed reply and retries the original request', async operation => {
  const { project, record } = example()
  vi.spyOn(window, 'prompt').mockReturnValue('12')
  vi.spyOn(window, 'confirm').mockReturnValue(true)
  const mutations: RequestInit[] = []
  let changed = false
  vi.mocked(apiFetch).mockImplementation(async (_path, options) => {
    if (options?.method) {
      mutations.push(options)
      if (mutations.length === 1) throw new ApiError('操作还未确认', 503)
      changed = true
      return operation === 'delete' ? undefined : { ...record, status: 'resolved', resolved_chapter: 12, version: 'b'.repeat(64) }
    }
    return { foreshadows: changed ? [] : [record], total: changed ? 0 : 1 }
  })
  render(<ForeshadowPanel projectId={project} />)
  const button = await screen.findByText(operation === 'delete' ? '删除' : '标记回收')
  fireEvent.click(button)
  expect(await screen.findByRole('alert')).toHaveProperty('textContent', '操作还未确认')
  expect(screen.getByText(record.description)).toBeTruthy()
  fireEvent.click(button)
  await waitFor(() => expect(screen.queryByText(record.description)).toBeNull())
  expect(mutations).toHaveLength(2)
  expect(mutations[0].headers).toEqual(mutations[1].headers)
  expect((mutations[0].headers as Record<string, string>)['If-Match']).toBe(record.version)
})

it('allows a fresh author action after a definitive rejection and refreshed record', async () => {
  const { project, record } = example()
  vi.spyOn(window, 'confirm').mockReturnValue(true)
  const mutations: RequestInit[] = []
  let current = record
  vi.mocked(apiFetch).mockImplementation(async (_path, options) => {
    if (options?.method === 'DELETE') {
      mutations.push(options)
      if (mutations.length === 1) {
        current = { ...record, version: 'b'.repeat(64), description: '已经改过的记录' }
        throw new ApiError('伏笔已被修改，请刷新核对', 409, 'source_changed')
      }
      return undefined
    }
    return { foreshadows: [current], total: 1 }
  })
  render(<ForeshadowPanel projectId={project} />)
  fireEvent.click(await screen.findByText('删除'))
  await screen.findByRole('alert')
  fireEvent.click(screen.getByText('刷新伏笔列表'))
  await screen.findByText('已经改过的记录')
  fireEvent.click(screen.getByText('删除'))
  await waitFor(() => expect(mutations).toHaveLength(2))
  expect(mutations[0].headers).not.toEqual(mutations[1].headers)
  expect((mutations[1].headers as Record<string, string>)['If-Match']).toBe('b'.repeat(64))
})
