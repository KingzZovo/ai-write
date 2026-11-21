import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { TaskQueuePanel } from '@/components/workspace/TaskQueuePanel'
import { apiFetch } from '@/lib/api'
vi.mock('@/lib/api', () => ({ apiFetch: vi.fn() }))
afterEach(() => { cleanup(); vi.resetAllMocks() })
it('loads complete task result on expansion instead of the truncated queue preview', async () => {
  vi.mocked(apiFetch).mockResolvedValue({ result_text: '完整开头。\n完整结尾。', progress_text: '旧片段' })
  render(<TaskQueuePanel tasks={[{taskId:'t1',taskType:'chapter',status:'needs_review',charCount:1000,progressText:'仅末尾200字',errorMessage:'待复核',chapterId:'c',createdAt:null,updatedAt:null}]} cancelUnavailable={false} onCancel={vi.fn()} onRefreshChapter={vi.fn()} />)
  expect(apiFetch).not.toHaveBeenCalled()
  const details = screen.getByText('查看任务候选正文').closest('details')!
  details.open = true
  fireEvent(details, new Event('toggle'))
  await waitFor(() => expect(screen.getByText(/完整开头/).textContent).toBe('完整开头。\n完整结尾。'))
  expect(apiFetch).toHaveBeenCalledWith('/api/generate/async/t1')
  expect(screen.queryByText('仅末尾200字')).toBeNull()
})
