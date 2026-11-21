import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import MobileWorkspace from '@/components/workspace/MobileWorkspace'
import { apiFetch, ApiError } from '@/lib/api'

const routing = vi.hoisted(() => ({ id: '', router: { push: vi.fn(), replace: vi.fn() } }))
vi.mock('@/lib/api', async (original) => ({ ...await original<typeof import('@/lib/api')>(), apiFetch: vi.fn() }))
vi.mock('next/navigation', () => ({ useRouter: () => routing.router, useSearchParams: () => new URLSearchParams(`id=${routing.id}`) }))
vi.mock('next/dynamic', () => ({ default: () => (props: { onGenerateOutline?: (level: string) => void; onViewOutline?: () => void; busy?: boolean; volumeDisabled?: boolean }) =>
  props.onGenerateOutline ? <div>
    <button disabled={props.busy || props.volumeDisabled} onClick={() => props.onGenerateOutline?.('volume')}>生成本卷候选</button>
    <button onClick={props.onViewOutline}>查看已有大纲</button>
  </div> : null }))
vi.mock('@/components/panels/GeneratePanel', () => ({ getSelectedStructureBookId: () => null }))
const api = vi.mocked(apiFetch)
let sequence = 0
const rawTask = (status = 'running', chapterId: string | null = 'a', taskId = 'task') => ({
  task_id: taskId, project_id: routing.id, chapter_id: chapterId, task_type: chapterId ? 'chapter' : 'outline_book',
  status, volume_idx: null as number | null, progress_text: '正在核对人物认知。', result_text: '', polished_text: '', error_message: null,
})
type Task = ReturnType<typeof rawTask>
type Interceptor = (path: string, options?: RequestInit) => Promise<unknown> | undefined

function setup(options: { failLoad?: boolean; empty?: boolean; tasks?: Task[]; volumes?: { id: string; title: string; volume_idx: number }[]; chapterVolume?: string } = {}) {
  const projectId = routing.id
  const state = { texts: { a: '原稿。', b: '乙章保存稿。' } as Record<string, string>, tasks: options.tasks ?? [],
    failLoad: options.failLoad ?? false, empty: options.empty ?? false, interceptor: null as Interceptor | null }
  const chapter = (id: string) => ({ id, title: id === 'a' ? '第一章' : '第二章', volume_id: options.chapterVolume ?? 'v',
    chapter_idx: id === 'a' ? 1 : 2, word_count: [...state.texts[id]].length, status: 'needs_review' })
  const source = (id: string, pid = projectId, text = state.texts[id]) => ({
    project_id: pid, chapter_id: id, title: id === 'a' ? '第一章' : '第二章',
    content_text: text, word_count: [...text].length, status: 'needs_review',
  })
  api.mockImplementation(async (path, init) => {
    const intercepted = state.interceptor?.(path, init)
    if (intercepted) return intercepted
    if (path === '/api/projects') return { projects: [{ id: projectId, title: '测试书', genre: '测试' }] }
    if (/^\/api\/projects\/[^/]+$/.test(path)) return { id: path.split('/').at(-1), title: '测试书', genre: '测试' }
    if (path.endsWith('/volumes')) return state.empty ? [] : options.volumes ?? [{ id: 'v', title: '第一卷', volume_idx: 1 }]
    if (path.includes('lightweight=true')) return state.empty ? [] : [chapter('a'), chapter('b')]
    if (path.endsWith('/outlines')) return []
    if (path.includes('outline-readiness')) {
      const chapterId = new URL(path, 'http://test').searchParams.get('chapter_id')
      return { project_id: path.split('/')[3], chapter_id: chapterId, ready: true, missing_layers: [],
        layers: { book: { ready: true }, volume: { ready: true }, chapter: { ready: true } } }
    }
    const manuscript = path.match(/^\/api\/projects\/([^/]+)\/chapters\/([^/]+)\/content$/)
    if (manuscript) {
      if (state.failLoad) throw new Error('保存稿暂不可读')
      const [, pid, cid] = manuscript
      if (init?.method === 'PUT') {
        const body = JSON.parse(init.body as string)
        const previous = state.texts[cid]
        if (previous !== body.expected_content_text) throw new ApiError('正文已更新', 409, 'chapter_edit_conflict')
        state.texts[cid] = body.content_text
        return { status: 'saved', manuscript: source(cid, pid), changed: previous !== body.content_text, backup_version_id: null }
      }
      return source(cid, pid)
    }
    if (path.startsWith('/api/generate/async/project/')) {
      return path.includes('active_only') ? state.tasks.filter((task) => !['completed', 'needs_review', 'needs_repair', 'failed', 'cancelled'].includes(task.status)) : state.tasks
    }
    if (path === '/api/generate/async' && init?.method === 'POST') {
      const body = JSON.parse(init.body as string)
      const task = { ...rawTask('pending', body.chapter_id ?? null, body.request_id), project_id: body.project_id, task_type: body.task_type, volume_idx: body.volume_idx ?? null }
      state.tasks = [task]
      return task
    }
    if (path.startsWith('/api/generate/async/')) {
      const task = state.tasks.find((item) => item.task_id === path.split('/').at(-1))
      if (!task) throw new Error('任务暂不可读')
      return task
    }
    throw new Error(`Unexpected path: ${path}`)
  })
  return { state, source, projectId, view: render(<MobileWorkspace />) }
}
async function openChapter(title = '第一章') {
  fireEvent.click(await screen.findByText(title))
  await waitFor(() => expect(screen.getByLabelText('章节编辑稿')).not.toHaveAttribute('readonly'))
}
async function switchChapter(title: string) {
  fireEvent.click(screen.getByTestId('mobile-outline-toggle'))
  await openChapter(title)
}
beforeEach(() => { routing.id = `mobile-save-${++sequence}`; sessionStorage.clear() })
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.resetAllMocks() })

it('saves the exact draft against the full source and requires its manuscript receipt', async () => {
  const { projectId } = setup()
  await openChapter()
  fireEvent.change(screen.getByLabelText('章节编辑稿'), { target: { value: '修改后的正文。' } })
  fireEvent.click(screen.getByText('立即保存'))
  await screen.findByText('正文已保存')
  const call = api.mock.calls.find(([, init]) => init?.method === 'PUT')!
  expect(call[0]).toBe(`/api/projects/${projectId}/chapters/a/content`)
  expect(JSON.parse(call[1]!.body as string)).toEqual({ content_text: '修改后的正文。', expected_content_text: '原稿。', force: false })
})

it('preserves a conflicted draft while switching away and returning without discarding it', async () => {
  const { state } = setup()
  await openChapter()
  fireEvent.change(screen.getByLabelText('章节编辑稿'), { target: { value: '保留我的改稿。' } })
  state.texts.a = '另一处后来保存的稿件。'
  fireEvent.click(screen.getByText('立即保存'))
  await screen.findByText(/保存稿已变化/)
  const confirm = vi.spyOn(window, 'confirm')
  await switchChapter('第二章')
  await switchChapter('第一章')
  await screen.findByLabelText('当前保存稿对照')
  expect(screen.getByLabelText('章节编辑稿')).toHaveValue('保留我的改稿。')
  expect(screen.getByLabelText('当前保存稿对照')).toHaveValue(state.texts.a)
  expect(confirm).not.toHaveBeenCalled()
})

it('does not treat a failed manuscript read as an editable empty chapter', async () => {
  setup({ failLoad: true })
  fireEvent.click(await screen.findByText('第一章'))
  await screen.findByText('保存稿暂不可读')
  expect(screen.getByText('立即保存')).toBeDisabled()
  expect(screen.getByLabelText('章节编辑稿')).toHaveAttribute('readonly')
  expect(screen.getByText('AI 生成')).toBeDisabled()
})

it('ignores a late chapter read after the author selects another chapter', async () => {
  const { state, source } = setup()
  await screen.findByText('第一章')
  let finish!: (value: unknown) => void
  state.interceptor = (path) => path.endsWith('/chapters/a/content') ? new Promise((resolve) => { finish = resolve }) : undefined
  fireEvent.click(screen.getByText('第一章'))
  await switchChapter('第二章')
  await act(async () => { finish(source('a', routing.id, '迟到的甲章正文。')) })
  expect(screen.getByLabelText('章节编辑稿')).toHaveValue('乙章保存稿。')
  expect(screen.getByRole('heading', { name: '第二章' })).toBeTruthy()
})

it('keeps text entered while an earlier draft save is in flight', async () => {
  const { state, source } = setup()
  await openChapter()
  let finish!: (value: unknown) => void
  state.interceptor = (_path, options) => options?.method === 'PUT' ? new Promise((resolve) => { finish = resolve }) : undefined
  fireEvent.change(screen.getByLabelText('章节编辑稿'), { target: { value: '已经送出的稿件。' } })
  fireEvent.click(screen.getByText('立即保存'))
  await screen.findByText('正在保存…')
  fireEvent.change(screen.getByLabelText('章节编辑稿'), { target: { value: '请求期间又写了一句。' } })
  await act(async () => { finish({ status: 'saved', manuscript: source('a', routing.id, '已经送出的稿件。'), changed: true, backup_version_id: null }) })
  expect(screen.getByLabelText('章节编辑稿')).toHaveValue('请求期间又写了一句。')
  expect(screen.getByText('尚有未保存修改')).toBeTruthy()
})

it('keeps an operational task message out of prose and binds completion to its original chapter', async () => {
  const { state } = setup({ tasks: [rawTask()] })
  await openChapter()
  await screen.findByText('查看任务进度记录')
  expect(screen.getByLabelText('章节编辑稿')).toHaveValue('原稿。')
  await switchChapter('第二章')
  state.tasks[0] = { ...state.tasks[0], status: 'needs_review', result_text: '任务候选，尚未采用。' }
  fireEvent.click(screen.getByText('重新读取任务'))
  await screen.findByLabelText('任务候选全文')
  expect(screen.getByLabelText('任务候选全文')).toHaveValue('任务候选，尚未采用。')
  expect(screen.getByLabelText('章节编辑稿')).toHaveValue('乙章保存稿。')
  expect(api.mock.calls.some(([, init]) => init?.method === 'PUT')).toBe(false)
  await switchChapter('第一章')
  expect(screen.getByLabelText('章节编辑稿')).toHaveValue('原稿。')
})

it('compares a finished tasks actual saved manuscript without replacing the authors newer draft', async () => {
  const { state } = setup({ tasks: [rawTask()] })
  await openChapter()
  await screen.findByText('查看任务进度记录')
  fireEvent.change(screen.getByLabelText('章节编辑稿'), { target: { value: '作者正在改的动作。' } })
  state.texts.a = '后台实际保存的全文。'
  state.tasks[0] = { ...state.tasks[0], status: 'completed', result_text: '与保存稿不同的任务候选。' }
  fireEvent.click(screen.getByText('重新读取任务'))
  await screen.findByLabelText('当前保存稿对照')
  expect(screen.getByLabelText('章节编辑稿')).toHaveValue('作者正在改的动作。')
  expect(screen.getByLabelText('当前保存稿对照')).toHaveValue('后台实际保存的全文。')
  expect(api.mock.calls.some(([, init]) => init?.method === 'PUT')).toBe(false)
})

it('stops a task with a mismatched chapter without reading or applying its result', async () => {
  const { state } = setup({ tasks: [rawTask()] })
  await openChapter()
  await screen.findByText('查看任务进度记录')
  state.interceptor = (path) => path === '/api/generate/async/task'
    ? Promise.resolve({ ...rawTask('completed', 'b'), result_text: '另一章任务。' }) : undefined
  fireEvent.click(screen.getByText('重新读取任务'))
  await screen.findByText(/后台任务来源已变化/)
  expect(screen.getByLabelText('章节编辑稿')).toHaveValue('原稿。')
  expect(screen.queryByLabelText('任务候选全文')).toBeNull()
})

it('retains prose when submission acknowledgement is incomplete and recovers without a second POST', async () => {
  const { state } = setup()
  await openChapter()
  await waitFor(() => expect(screen.getByText('AI 生成')).not.toBeDisabled())
  state.interceptor = (path, options) => path === '/api/generate/async' && options?.method === 'POST'
    ? Promise.resolve({ task_id: 'task', status: 'pending' }) : undefined
  fireEvent.click(screen.getByText('AI 生成'))
  await screen.findByText(/请在请求提示中核对原请求/)
  expect(screen.getByLabelText('章节编辑稿')).toHaveValue('原稿。')
  expect(screen.getByText('AI 生成')).toBeDisabled()
  const submitted = api.mock.calls.find(([path, init]) => path === '/api/generate/async' && init?.method === 'POST')!
  const payload = JSON.parse(submitted[1]!.body as string)
  state.tasks = [rawTask('pending', 'a', payload.request_id)]
  fireEvent.click(screen.getByText('刷新后台任务'))
  await screen.findByText('查看任务进度记录')
  expect(api.mock.calls.filter(([path, options]) => path === '/api/generate/async' && options?.method === 'POST')).toHaveLength(1)
})

it('can create a full book outline before any chapter exists', async () => {
  const { state } = setup({ empty: true })
  const input = await screen.findByPlaceholderText('例如：都市修仙，主角是外卖员意外获得修炼功法...')
  fireEvent.change(input, { target: { value: '一部关于姐弟和旧债的现实小说。' } })
  await waitFor(() => expect(screen.getByText('生成全书大纲')).not.toBeDisabled())
  fireEvent.click(screen.getByText('生成全书大纲'))
  await screen.findByText('查看任务进度记录')
  expect(state.tasks[0].task_type).toBe('outline_book')
  expect(state.tasks[0].chapter_id).toBeNull()
  expect(api.mock.calls.some(([path]) => path.endsWith('/content'))).toBe(false)
})

it('recovers an old evaluating task even when recent history omits it', async () => {
  const { state } = setup()
  await screen.findByText('第一章')
  state.interceptor = (path) => path.includes('/async/project/')
    ? Promise.resolve(path.includes('active_only') ? [rawTask('evaluating')] : []) : undefined
  state.tasks = [rawTask('evaluating')]
  fireEvent.click(screen.getByText('刷新后台任务'))
  await screen.findByText('章节正文 · 检查中')
  await openChapter()
  expect(screen.getByText('生成中...')).toBeDisabled()
})

it('ignores a late readiness answer for a previously selected chapter', async () => {
  const { state } = setup()
  await openChapter()
  await waitFor(() => expect(screen.getByText('AI 生成')).not.toBeDisabled())
  let finish!: (value: unknown) => void
  state.interceptor = (path) => path.includes('outline-readiness')
    ? new Promise((resolve) => { finish = resolve }) : undefined
  await switchChapter('第二章')
  await act(async () => { finish({ project_id: routing.id, chapter_id: 'a', ready: true, missing_layers: [], layers: {} }) })
  expect(screen.getByText('AI 生成')).toBeDisabled()
  expect(screen.getByLabelText('章节编辑稿')).toHaveValue('乙章保存稿。')
})

it('ignores a late submitted task after navigation to another project', async () => {
  const { state, view, projectId } = setup()
  await openChapter()
  await waitFor(() => expect(screen.getByText('AI 生成')).not.toBeDisabled())
  let finish!: (value: unknown) => void
  state.interceptor = (path, options) => path === '/api/generate/async' && options?.method === 'POST'
    ? new Promise((resolve) => { finish = resolve }) : undefined
  fireEvent.click(screen.getByText('AI 生成'))
  routing.id = `mobile-other-${++sequence}`
  view.rerender(<MobileWorkspace />)
  await openChapter('第二章')
  const submitted = api.mock.calls.find(([path, init]) => path === '/api/generate/async' && init?.method === 'POST')!
  const payload = JSON.parse(submitted[1]!.body as string)
  await act(async () => { finish({ ...rawTask('pending', 'a', payload.request_id), project_id: projectId }) })
  expect(screen.queryByLabelText('后台生成任务')).toBeNull()
  expect(screen.getByLabelText('章节编辑稿')).toHaveValue('乙章保存稿。')
})

it('requires a clear volume target and labels the completed volume outline as a candidate', async () => {
  const { state } = setup({ volumes: [{ id: 'v1', title: '第一卷', volume_idx: 1 }, { id: 'v2', title: '第二卷', volume_idx: 2 }], chapterVolume: 'v2' })
  await screen.findByText('第一章')
  fireEvent.click(screen.getByText('工具'))
  expect(screen.getByText('生成本卷候选')).toBeDisabled()
  fireEvent.click(screen.getByText('查看已有大纲'))
  await openChapter()
  fireEvent.click(screen.getByText('工具'))
  await waitFor(() => expect(screen.getByText('生成本卷候选')).not.toBeDisabled())
  fireEvent.click(screen.getByText('生成本卷候选'))
  await screen.findByText('任务指定：第 2 卷')
  const call = api.mock.calls.find(([path, init]) => path === '/api/generate/async' && init?.method === 'POST')!
  expect(JSON.parse(call[1]!.body as string)).toMatchObject({ task_type: 'outline_volume', volume_idx: 2 })
  state.tasks[0] = { ...state.tasks[0], status: 'completed', result_text: '第二卷的新候选。' }
  // “重新读取任务”在读取中会被禁用;不等它可用就点击,点击会落空,
  // 详情停在上一份快照 —— 这正是本用例此前间歇失败的原因。
  await waitFor(() => expect(screen.getByText('重新读取任务')).not.toBeDisabled())
  fireEvent.click(screen.getByText('重新读取任务'))
  // 读回详情是一次异步往返:同步取节点会在详情到达前断言。
  await screen.findByLabelText('任务候选全文')
  expect(screen.getByLabelText('任务候选全文')).toHaveValue('第二卷的新候选。')
  await screen.findByText(/任务交付的是卷纲候选/)
  expect(screen.getByRole('button', { name: '核对并采用卷纲候选' })).toBeInTheDocument()
  expect(api.mock.calls.some(([, init]) => init?.method === 'PUT' || init?.method === 'DELETE')).toBe(false)
})


it('submits the reviewed manuscript and retains the same request after a lost response', async () => {
  const { state } = setup()
  await openChapter()
  await waitFor(() => expect(screen.getByText('AI 生成')).not.toBeDisabled())
  let first = true
  state.interceptor = (path, init) => {
    if (path !== '/api/generate/async' || init?.method !== 'POST') return undefined
    if (first) { first = false; return Promise.reject(new Error('回执丢失')) }
    return undefined
  }
  fireEvent.click(screen.getByText('AI 生成'))
  await screen.findByText('核对并继续原请求')
  fireEvent.change(screen.getByLabelText('章节编辑稿'), { target: { value: '作者在等待期间继续改稿。' } })
  fireEvent.click(screen.getByText('核对并继续原请求'))
  await screen.findByText('查看任务进度记录')
  const calls = api.mock.calls.filter(([path, init]) => path === '/api/generate/async' && init?.method === 'POST')
  expect(calls).toHaveLength(2)
  expect(calls[1][1]!.body).toBe(calls[0][1]!.body)
  expect(JSON.parse(calls[0][1]!.body as string)).toMatchObject({ chapter_id: 'a', expected_content_text: '原稿。', request_id: expect.any(String) })
  expect(screen.getByLabelText('章节编辑稿')).toHaveValue('作者在等待期间继续改稿。')
})

it('reloads the complete task list when returning to a project with a retained submission receipt', async () => {
  const first = setup()
  await openChapter()
  await waitFor(() => expect(screen.getByText('AI 生成')).not.toBeDisabled())
  fireEvent.click(screen.getByText('AI 生成'))
  await screen.findByText('查看任务进度记录')
  const known = first.state.tasks[0]
  first.view.unmount()
  setup({ tasks: [known, rawTask('running', 'b', 'another-task')] })
  const picker = await screen.findByLabelText('查看后台任务')
  expect(picker.querySelectorAll('option')).toHaveLength(2)
})
