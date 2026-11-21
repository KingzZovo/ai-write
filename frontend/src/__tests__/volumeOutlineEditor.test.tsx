import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { VolumeOutlineEditor } from '@/components/outline/VolumeOutlineEditor'
import { VolumeOutlineHistory } from '@/components/outline/VolumeOutlineHistory'
import { apiFetch } from '@/lib/api'
import { volumeReceipt, volumeSource } from './fixtures/volumeOutline'

vi.mock('@/lib/api', async (original) => ({ ...await original<typeof import('@/lib/api')>(), apiFetch: vi.fn() }))
let sequence = 0
beforeEach(() => { vi.mocked(apiFetch).mockReset(); sessionStorage.clear() })
afterEach(() => { cleanup(); vi.restoreAllMocks() })

it('previews a candidate in its recorded volume without replacing an existing author draft', async () => {
  const projectId = `preview-${++sequence}`
  const source = volumeSource(projectId, 'recorded-volume')
  const candidate = { task_id: 'candidate-task', text: '候选全文：收据要当面核对。' }
  vi.mocked(apiFetch).mockImplementation(async (path, options) => {
    if (String(path).endsWith('/candidates/candidate-task')) return { source, candidate } as never
    if (options?.method === 'PUT') return volumeReceipt(JSON.parse(options.body as string)) as never
    return source as never
  })
  const view = render(<VolumeOutlineEditor projectId={projectId} volumeId="recorded-volume" onClose={() => {}} />)
  const editor = await screen.findByRole('textbox', { name: '卷纲编辑稿' })
  await waitFor(() => expect(editor).not.toBeDisabled())
  fireEvent.change(editor, { target: { value: '作者自己写的安排。' } })
  view.rerender(<VolumeOutlineEditor projectId={projectId} candidateTaskId="candidate-task" onClose={() => {}} />)
  await screen.findByRole('textbox', { name: '待采用卷纲候选' })
  expect(screen.getByRole('textbox', { name: '卷纲编辑稿' })).toHaveValue('作者自己写的安排。')
  expect(vi.mocked(apiFetch).mock.calls.filter(([, options]) => options?.method === 'PUT')).toHaveLength(0)
  fireEvent.click(screen.getByRole('button', { name: '将这份候选放入编辑稿' }))
  expect(screen.getByRole('textbox', { name: '卷纲编辑稿' })).toHaveValue(candidate.text)
  expect(screen.getByRole('textbox', { name: '替换前卷纲编辑稿', hidden: true })).toHaveValue('作者自己写的安排。')
  fireEvent.click(screen.getByRole('button', { name: '保存为本卷大纲' }))
  await screen.findByText(/本次卷纲保存已确认/)
  const saved = vi.mocked(apiFetch).mock.calls.find(([, options]) => options?.method === 'PUT')!
  expect(saved[0]).toContain('/recorded-volume')
  expect(JSON.parse(saved[1]!.body as string).candidate).toEqual(candidate)
})

it('does not fall back to the current selected volume when candidate provenance fails', async () => {
  vi.mocked(apiFetch).mockRejectedValue(new Error('候选的分卷来源已变化'))
  render(<VolumeOutlineEditor projectId={`invalid-${++sequence}`} volumeId="current-volume" candidateTaskId="old-task" onClose={() => {}} />)
  await screen.findByRole('alert')
  expect(screen.queryByRole('textbox', { name: '卷纲编辑稿' })).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: '保存为本卷大纲' })).not.toBeInTheDocument()
})

it('loads older history pages without fetching all full outlines', async () => {
  const projectId = `history-${++sequence}`, before = volumeSource(projectId), input = {
    request_id: 'r1', expected_source: before, text: '修改后的完整稿。', candidate: null,
  }
  vi.mocked(apiFetch).mockImplementation(async (path) => {
    if (String(path).endsWith('/r1')) return { request_id: 'r1', before, receipt: volumeReceipt(input) } as never
    if (String(path).includes('cursor=r1')) return { items: [{ request_id: 'r0', created_at: '2026-10-08T10:00:00Z' }], next_cursor: null } as never
    return { items: [{ request_id: 'r1', created_at: '2026-10-09T10:00:00Z' }], next_cursor: 'r1' } as never
  })
  render(<VolumeOutlineHistory projectId={projectId} volumeId="v" />)
  fireEvent.click(await screen.findByRole('button', { name: '查看更早记录' }))
  await waitFor(() => expect(screen.queryByRole('button', { name: '查看更早记录' })).not.toBeInTheDocument())
  expect(vi.mocked(apiFetch).mock.calls).toHaveLength(2)
  const firstDate = new Date('2026-10-09T10:00:00Z').toLocaleString()
  fireEvent.click(screen.getByRole('button', { name: firstDate }))
  expect(await screen.findByRole('textbox', { name: '历史修改后卷纲' })).toHaveValue(input.text)
  expect(screen.getByRole('textbox', { name: '历史修改前卷纲' })).toHaveValue(String(before.outline!.content_json.raw_text))
})

it('clears an aborted history detail when a new save refreshes the history', async () => {
  const projectId = `history-refresh-${++sequence}`
  vi.mocked(apiFetch).mockImplementation(async (path) => {
    if (String(path).endsWith('/r1')) return new Promise(() => {})
    return { items: [{ request_id: 'r1', created_at: '2026-10-09T10:00:00Z' }], next_cursor: null } as never
  })
  const view = render(<VolumeOutlineHistory projectId={projectId} volumeId="v" revision="one" />)
  fireEvent.click(await screen.findByRole('button', { name: new Date('2026-10-09T10:00:00Z').toLocaleString() }))
  await screen.findByText('正在读取本次修改前后全文…')
  view.rerender(<VolumeOutlineHistory projectId={projectId} volumeId="v" revision="two" />)
  await waitFor(() => expect(screen.queryByText('正在读取本次修改前后全文…')).not.toBeInTheDocument())
})
