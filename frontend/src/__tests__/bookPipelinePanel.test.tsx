import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { BookPipelinePanel } from '@/components/workspace/BookPipelinePanel'
import type { BookPipeline } from '@/components/workspace/useBookPipeline'
import { apiFetch } from '@/lib/api'

vi.mock('@/lib/api', () => ({ apiFetch: vi.fn() }))
const fetchMock = vi.mocked(apiFetch)
const pipeline = (state = 'planning'): BookPipeline => ({
  id: 'run-1', project_id: 'book-1', volume_id: null, state,
  total_chapters: 2, completed_chapters: 0, needs_review_chapters: state === 'needs_review' ? 1 : 0,
  current_chapter_id: null, error_message: null, created_at: null, heartbeat_at: null, can_rollback: false,
  chapters: [
    { chapter_id: 'chapter-1', volume_idx: 1, chapter_idx: 1, state: state === 'needs_review' ? 'needs_review' : 'pending', word_count: state === 'needs_review' ? 1200 : 0, error_message: null },
    { chapter_id: 'chapter-2', volume_idx: 2, chapter_idx: 1, state: 'pending', word_count: 0, error_message: null },
  ],
})
beforeEach(() => { fetchMock.mockReset() })
afterEach(() => { cleanup(); vi.useRealTimers() })
function respond(run: BookPipeline | null) {
  fetchMock.mockImplementation(async path => path.includes('/volumes')
    ? [{ id: 'volume-1', title: '启程', volume_idx: 1 }]
    : path.includes('/project/') ? (run ? [run] : []) : run)
}

describe('整书生成界面', () => {
  it('creates a reviewable task before starting and prevents repeated submission', async () => {
    respond(null)
    render(<BookPipelinePanel projectId="book-1" />)
    await screen.findByText('创建任务并检查章节')
    fireEvent.change(screen.getByLabelText('本次写作要求'), { target: { value: '有限视角' } })
    let finish: (value: BookPipeline) => void = () => {}
    fetchMock.mockImplementationOnce(() => new Promise(resolve => { finish = resolve as typeof finish }))
    fireEvent.click(screen.getByText('创建任务并检查章节'))
    expect(screen.getByText('创建任务并检查章节')).toBeDisabled()
    await act(async () => finish(pipeline()))
    expect(fetchMock).toHaveBeenCalledWith('/api/pipeline', expect.objectContaining({
      method: 'POST', body: JSON.stringify({ project_id: 'book-1', volume_id: null, options: { user_input: '有限视角', max_attempts: 2 } }),
    }))
    expect(fetchMock.mock.calls.some(([path]) => path.endsWith('/start'))).toBe(false)
    fireEvent.click(screen.getByText('启动生成'))
    expect(screen.getByRole('group', { name: '确认操作' })).toHaveTextContent('2 个未完成章节')
    fetchMock.mockResolvedValueOnce(pipeline('queued'))
    fireEvent.click(screen.getByText('确认执行'))
    await screen.findByText(/排队中 · 整书任务/)
  })

  it('keeps reviewable drafts out of completion and previews by stable ID', async () => {
    respond(pipeline('needs_review'))
    render(<BookPipelinePanel projectId="book-1" />)
    await screen.findByText('已完成 0 / 2 章 · 待审核 1 章')
    expect(screen.getByText('第1卷 · 第1章')).toBeInTheDocument()
    expect(screen.getByText('第2卷 · 第1章')).toBeInTheDocument()
    fetchMock.mockResolvedValueOnce({ title: '第一章', content_text: '已保存的草稿。' })
    fireEvent.click(screen.getByText('查看正文 · 1200字'))
    expect(await screen.findByLabelText('已保存正文')).toHaveValue('已保存的草稿。')
    expect(fetchMock).toHaveBeenLastCalledWith('/api/projects/book-1/chapters/chapter-1', expect.objectContaining({ signal: expect.any(AbortSignal) }))
  })

  it('shows pause acknowledgement and does not offer rollback while running', async () => {
    respond(pipeline('generating'))
    render(<BookPipelinePanel projectId="book-1" />)
    await screen.findByText('暂停任务')
    expect(screen.queryByText('恢复生成前版本')).not.toBeInTheDocument()
    fetchMock.mockResolvedValueOnce(pipeline('pausing'))
    fireEvent.click(screen.getByText('暂停任务'))
    await screen.findByText('正在完成当前章节并保存，随后暂停。')
    expect(screen.getByText('暂停任务')).toBeDisabled()
  })

  it('surfaces rollback conflicts without removing the current task', async () => {
    respond({ ...pipeline('paused'), can_rollback: true })
    render(<BookPipelinePanel projectId="book-1" />)
    fireEvent.click(await screen.findByText('恢复生成前版本'))
    fetchMock.mockRejectedValueOnce(new Error('正文在任务之外发生了修改，已保留当前内容'))
    fireEvent.click(screen.getByText('确认执行'))
    expect(await screen.findByRole('alert')).toHaveTextContent('已保留当前内容')
    expect(screen.getByText(/已暂停 · 整书任务/)).toBeInTheDocument()
  })

  it('discards delayed responses from the previous project', async () => {
    let resolveOld: (value: BookPipeline[]) => void = () => {}
    fetchMock.mockImplementation(async path => {
      if (path.includes('/volumes')) return []
      if (path.includes('book-1')) return new Promise(resolve => { resolveOld = resolve as typeof resolveOld })
      return [{ ...pipeline('completed'), id: 'new-run', project_id: 'book-2' }]
    })
    const view = render(<BookPipelinePanel projectId="book-1" />)
    view.rerender(<BookPipelinePanel projectId="book-2" />)
    await screen.findByText(/已完成 · 整书任务/)
    await act(async () => resolveOld([pipeline('failed')]))
    expect(screen.queryByText(/失败 · 整书任务/)).not.toBeInTheDocument()
  })

  it('polls active tasks, stops on completion, and resumes after transient failure', async () => {
    vi.useFakeTimers()
    respond(pipeline('queued'))
    render(<BookPipelinePanel projectId="book-1" />)
    await act(async () => {})
    fetchMock.mockRejectedValueOnce(new Error('网络暂时不可用'))
    await act(async () => vi.advanceTimersByTimeAsync(3000))
    expect(screen.getByRole('alert')).toHaveTextContent('网络暂时不可用')
    fetchMock.mockResolvedValueOnce({ ...pipeline('completed'), completed_chapters: 2 })
    await act(async () => vi.advanceTimersByTimeAsync(3000))
    expect(screen.getByText(/已完成 · 整书任务/)).toBeInTheDocument()
    const count = fetchMock.mock.calls.length
    await act(async () => vi.advanceTimersByTimeAsync(9000))
    expect(fetchMock).toHaveBeenCalledTimes(count)
  })
})

it('does not let an older poll overwrite a pause command', async () => {
  vi.useFakeTimers()
  respond(pipeline('queued'))
  render(<BookPipelinePanel projectId="book-1" />)
  await act(async () => {})
  let finishPoll: (value: BookPipeline) => void = () => {}
  fetchMock.mockImplementationOnce(() => new Promise(resolve => { finishPoll = resolve as typeof finishPoll }))
  await act(async () => vi.advanceTimersByTimeAsync(3000))
  fetchMock.mockResolvedValueOnce(pipeline('paused'))
  fireEvent.click(screen.getByText('暂停任务'))
  await act(async () => {})
  await act(async () => finishPoll(pipeline('generating')))
  expect(screen.getByText(/已暂停 · 整书任务/)).toBeInTheDocument()
})
