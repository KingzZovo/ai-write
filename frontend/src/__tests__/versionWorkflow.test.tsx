import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { apiFetch } from '@/lib/api'
import { VersionPanel } from '@/components/panels/VersionPanel'
import { VersionDiffView } from '@/components/versions/VersionDiffView'

vi.mock('@/lib/api', () => ({ apiFetch: vi.fn() }))
vi.mock('next/dynamic', () => ({ default: () => function Diff({ original, modified }: { original: string; modified: string }) {
  return <div><pre data-testid="original">{original}</pre><pre data-testid="modified">{modified}</pre></div>
} }))
const api = vi.mocked(apiFetch)
const node = (id = 'v1', chapter = 'a', body = '候选正文。') => ({ id, chapter_id: chapter, parent_id: null,
  branch_name: id, content_text: body, content_diff: '', word_count: 5, created_at: '2026-10-09T00:00:00Z',
  is_active: id === 'v2', source: 'user_edit', metadata: {} })
const source = (chapter = 'a', body = '实际保存稿。') => ({ chapter_id: chapter, project_id: 'p', title: '交付', content_text: body, word_count: 6 })
afterEach(() => { cleanup(); vi.resetAllMocks(); vi.restoreAllMocks() })

it('renders backend fields and actual diff text without assuming diff.lines', async () => {
  api.mockImplementation(async (path) => path.includes('/diff?')
    ? { version_a: 'v2', version_b: 'v1', diff: '--- parent\n+++ current\n-旧稿\n+新稿\n' }
    : [node(), node('v2')])
  render(<VersionPanel chapterId="a" />)
  await screen.findByText('v1')
  expect(screen.getAllByText(/5 字 · 作者快照/)).toHaveLength(2)
  fireEvent.click(screen.getByRole('button', { name: /v1/ }))
  await screen.findByText(/\+新稿/)
  expect(screen.getByText('选为对比基准')).toBeTruthy()
})

it('uses the branch endpoint with a source and preserves the form on failure', async () => {
  api.mockImplementation(async (_path, options) => {
    if (options?.method === 'POST') throw new Error('保存未确认')
    return [node(), node('v2')]
  })
  render(<VersionPanel chapterId="a" />)
  await screen.findByText('v2')
  fireEvent.click(screen.getByText('创建分支'))
  fireEvent.change(screen.getByLabelText('分支名称'), { target: { value: '另一种收束' } })
  fireEvent.click(screen.getByText('确认创建分支'))
  await screen.findByText('保存未确认')
  const call = api.mock.calls.find(([, options]) => options?.method === 'POST')!
  expect(call[0]).toBe('/api/chapters/a/versions/branch')
  expect(JSON.parse(call[1]!.body as string)).toEqual({ source_version_id: 'v2', branch_name: '另一种收束' })
  expect((screen.getByLabelText('分支名称') as HTMLInputElement).value).toBe('另一种收束')
})

it('shows a failed list load instead of silently treating it as no versions', async () => {
  api.mockRejectedValue(new Error('版本暂不可读'))
  render(<VersionPanel chapterId="a" />)
  await screen.findByRole('alert')
  expect(screen.getByText('版本暂不可读')).toBeTruthy()
  expect(screen.queryByText('暂无版本记录。')).toBeNull()
  expect((screen.getByText('保存正文快照') as HTMLButtonElement).disabled).toBe(true)
})

it('defaults to the canonical saved body instead of the selected historical version', async () => {
  api.mockImplementation(async (path) => path.endsWith('/source') ? source() : [node(), node('v2', 'a', '仅是已选版本。')])
  render(<VersionDiffView chapterId="a" />)
  await waitFor(() => expect(screen.getByTestId('modified').textContent).toBe('实际保存稿。'))
  expect((screen.getByLabelText('右侧对照') as HTMLSelectElement).value).toBe('saved')
})

it('restores with the reviewed body and only reports success after the complete receipt', async () => {
  let canonical = '实际保存稿。'
  api.mockImplementation(async (path, options) => {
    if (options?.method === 'POST') {
      expect(JSON.parse(options.body as string).expected_content_text).toBe(canonical)
      canonical = '候选正文。'
      return { status: 'ok', chapter_id: 'a', active_version_id: 'v1', content_text: canonical,
        word_count: 5, changed: true, backup_version_id: 'backup' }
    }
    return path.endsWith('/source') ? source('a', canonical) : [node(), node('v2')]
  })
  vi.spyOn(window, 'confirm').mockReturnValue(true)
  render(<VersionDiffView chapterId="a" />)
  await screen.findByTestId('modified')
  fireEvent.click(screen.getByText('用左侧版本恢复正文'))
  await screen.findByText('正文已恢复，原保存稿已完整备份。编辑区中的未保存修改请另外核对。')
  await waitFor(() => expect(screen.getByTestId('modified').textContent).toBe('候选正文。'))
})

it('shows current prose for review before restoring from a historical comparison', async () => {
  api.mockImplementation(async (path) => path.endsWith('/source') ? source() : [node(), node('v2')])
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true)
  render(<VersionDiffView chapterId="a" />)
  await screen.findByTestId('modified')
  fireEvent.change(screen.getByLabelText('右侧对照'), { target: { value: 'v2' } })
  fireEvent.click(screen.getByText('用左侧版本恢复正文'))
  expect(screen.getByText('已切换为当前保存稿对照，请核对右侧正文后再确认恢复。')).toBeTruthy()
  expect(confirm).not.toHaveBeenCalled()
  expect(api.mock.calls.some(([, options]) => options?.method === 'POST')).toBe(false)
})

it('keeps the selected manuscript and requires a refresh after an unknown restore outcome', async () => {
  api.mockImplementation(async (path, options) => {
    if (options?.method === 'POST') throw new Error('恢复结果未确认')
    return path.endsWith('/source') ? source() : [node(), node('v2')]
  })
  vi.spyOn(window, 'confirm').mockReturnValue(true)
  render(<VersionDiffView chapterId="a" />)
  await screen.findByTestId('modified')
  fireEvent.click(screen.getByText('用左侧版本恢复正文'))
  await screen.findByText('恢复结果未确认')
  expect(screen.getByTestId('original').textContent).toBe('候选正文。')
  expect((screen.getByText('用左侧版本恢复正文') as HTMLButtonElement).disabled).toBe(true)
  expect((screen.getByLabelText('左侧版本') as HTMLSelectElement).value).toBe('v1')
})

it('cannot populate a new chapter with an old chapters late responses', async () => {
  let resolveVersions!: (data: unknown) => void
  let resolveSource!: (data: unknown) => void
  api.mockImplementation((path) => {
    if (path.includes('/a/')) return new Promise((resolve) => { if (path.endsWith('/source')) resolveSource = resolve; else resolveVersions = resolve })
    return Promise.resolve(path.endsWith('/source') ? source('b', '乙章保存稿。') : [node('b1', 'b', '乙章候选。')])
  })
  const view = render(<VersionDiffView chapterId="a" />)
  const firstSignal = api.mock.calls[0][1]?.signal
  view.rerender(<VersionDiffView chapterId="b" />)
  await screen.findByText('乙章保存稿。')
  await act(async () => { resolveVersions([node()]); resolveSource(source()) })
  expect(firstSignal?.aborted).toBe(true)
  expect(screen.getByTestId('modified').textContent).toBe('乙章保存稿。')
})
