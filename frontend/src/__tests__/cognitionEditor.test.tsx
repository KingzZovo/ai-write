import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { CognitionEditor } from '@/components/panels/CognitionEditor'
import { CognitionPanel } from '@/components/panels/CognitionPanel'
import { CognitionHistory } from '@/components/panels/CognitionHistory'
import { apiFetch } from '@/lib/api'
import { readCognitionDraft, rowsFromChanges, writeCognitionDraft } from '@/lib/cognitionDrafts'
import type { CognitionDetail } from '@/lib/cognitionTypes'
import type { CognitionIntent } from '@/lib/cognitionRequests'

vi.mock('@/lib/api', async original => ({ ...await original<typeof import('@/lib/api')>(), apiFetch: vi.fn() }))
afterEach(() => { cleanup(); vi.resetAllMocks(); sessionStorage.clear() })
const cid = '00000000-0000-4000-8000-000000000001'
const rid = '00000000-0000-4000-8000-000000000002'
let number = 0
const project = () => `cognition-editor-${++number}`
const prose = '风声穿过院子。'.repeat(180) + '沈青以为信已寄出，却怀疑回信是陈川代写的。' + '灯灭了。'.repeat(150)
const detail = (): CognitionDetail => ({ chapter_id: cid, chapter_title: '回信', global_idx: 1,
  status: 'completed', version: 'b'.repeat(64), source_hash: 'a'.repeat(64), attempts: 1, changes_count: 1,
  can_retry: false, can_edit: true, source_mode: 'auto', revision_id: null, message: '请核对原句。',
  changes: [{ character: '沈青', learns: '信已寄出', evidence: '沈青以为信已寄出' }],
  model_changes: [{ character: '沈青', learns: '信已寄出', evidence: '沈青以为信已寄出' }],
  model_status: 'completed', source_content: prose,
})
const submit = () => vi.fn(async (_intent: CognitionIntent) => {})

it('selects a verbatim quote from the full saved text and submits a distinct suspicion', async () => {
  const data = detail(), onSubmit = submit()
  vi.mocked(apiFetch).mockResolvedValue(data)
  render(<CognitionEditor projectId={project()} chapterId={cid} currentVersion={data.version} disabled={false} pending={false} onSubmit={onSubmit} />)
  const source = await screen.findByLabelText('已保存的完整正文') as HTMLTextAreaElement
  expect(source.value).toBe(prose)
  fireEvent.change(screen.getByLabelText('认知类型'), { target: { value: 'suspects' } })
  fireEvent.change(screen.getByLabelText('认知内容'), { target: { value: '怀疑回信由陈川代写' } })
  const quote = '却怀疑回信是陈川代写的。', start = prose.indexOf(quote)
  source.setSelectionRange(start, start + quote.length)
  fireEvent.select(source)
  fireEvent.click(screen.getByText('使用选中的原句'))
  expect((screen.getByLabelText('依据原句') as HTMLTextAreaElement).value).toBe(quote)
  fireEvent.click(screen.getByText('保存作者校订'))
  expect(onSubmit).toHaveBeenCalledWith({ operation: 'edit', chapter_id: cid, source_hash: data.source_hash,
    version: data.version, changes: [{ character: '沈青', suspects: '怀疑回信由陈川代写', evidence: quote }] })
})

it('requires explicit review of a recovered draft after its source version changed', async () => {
  const pid = project(), previous = detail(), newer = { ...detail(), version: 'c'.repeat(64) }
  writeCognitionDraft(pid, cid, { source_hash: previous.source_hash!, version: previous.version!,
    rows: [{ id: 0, character: '沈青', kind: 'believes', fact: '作者未保存的误信', evidence: '沈青以为信已寄出' }] })
  vi.mocked(apiFetch).mockResolvedValue(newer)
  const onSubmit = submit()
  render(<CognitionEditor projectId={pid} chapterId={cid} currentVersion={newer.version} disabled={false} pending={false} onSubmit={onSubmit} />)
  await screen.findByText(/这份草稿仍保留原版本/)
  expect((screen.getByLabelText('认知内容') as HTMLTextAreaElement).value).toBe('作者未保存的误信')
  expect((screen.getByText('保存作者校订') as HTMLButtonElement).disabled).toBe(true)
  fireEvent.click(screen.getByText('已核对当前记录，沿用这份草稿'))
  fireEvent.click(screen.getByText('保存作者校订'))
  expect(onSubmit.mock.calls[0][0]).toMatchObject({ version: newer.version, changes: [{ believes: '作者未保存的误信' }] })
})

it('keeps input typed while a newer detail is still loading', async () => {
  const pid = project(), initial = detail()
  let finish!: (value: unknown) => void
  vi.mocked(apiFetch).mockResolvedValueOnce(initial).mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
  const props = { projectId: pid, chapterId: cid, disabled: false, pending: false, onSubmit: submit() }
  const { rerender } = render(<CognitionEditor {...props} currentVersion={initial.version} />)
  await screen.findByLabelText('认知内容')
  rerender(<CognitionEditor {...props} currentVersion={'c'.repeat(64)} />)
  fireEvent.change(screen.getByLabelText('认知内容'), { target: { value: '刷新期间新输入' } })
  await act(async () => { finish({ ...initial, version: 'c'.repeat(64) }) })
  expect((screen.getByLabelText('认知内容') as HTMLTextAreaElement).value).toBe('刷新期间新输入')
  expect(readCognitionDraft(pid, cid)?.version).toBe(initial.version)
})

it('shows retained draft text even when the saved source cannot be loaded', async () => {
  const pid = project(), initial = detail()
  writeCognitionDraft(pid, cid, { source_hash: initial.source_hash!, version: initial.version!, rows: rowsFromChanges(initial.changes!) })
  vi.mocked(apiFetch).mockRejectedValue(new Error('unavailable'))
  render(<CognitionEditor projectId={pid} chapterId={cid} currentVersion={initial.version} disabled={false} pending={false} onSubmit={submit()} />)
  await screen.findByText(/无法读取完整保存稿/)
  expect(screen.getByText(/本地校订草稿仍已保留/)).toBeTruthy()
  expect(screen.queryByText('保存作者校订')).toBeNull()
})

it('preserves pending edits and makes an empty decision explicit', async () => {
  const data = detail(), pid = project(), onSubmit = submit()
  vi.mocked(apiFetch).mockResolvedValue(data)
  const props = { projectId: pid, chapterId: cid, currentVersion: data.version, disabled: false, onSubmit }
  const { rerender } = render(<CognitionEditor {...props} pending />)
  await screen.findByLabelText('认知内容')
  fireEvent.change(screen.getByLabelText('认知内容'), { target: { value: '待确认期间的新稿' } })
  expect(readCognitionDraft(pid, cid)?.rows[0].fact).toBe('待确认期间的新稿')
  expect((screen.getByText('保存作者校订') as HTMLButtonElement).disabled).toBe(true)
  rerender(<CognitionEditor {...props} pending={false} />)
  fireEvent.click(screen.getByText('删除这条校订'))
  fireEvent.click(screen.getByText('确认本章无认知变化'))
  expect(onSubmit.mock.calls[0][0]).toMatchObject({ operation: 'edit', changes: [] })
})

it('does not submit evidence absent from the current full source', async () => {
  const data = detail(), onSubmit = submit()
  vi.mocked(apiFetch).mockResolvedValue(data)
  render(<CognitionEditor projectId={project()} chapterId={cid} currentVersion={data.version} disabled={false} pending={false} onSubmit={onSubmit} />)
  await screen.findByLabelText('依据原句')
  fireEvent.change(screen.getByLabelText('依据原句'), { target: { value: '凭空编出的依据' } })
  fireEvent.click(screen.getByText('保存作者校订'))
  await screen.findByText(/无法在当前保存稿逐字定位/)
  expect(onSubmit).not.toHaveBeenCalled()
})

it('restores automatic work explicitly and retains unsaved text', async () => {
  const data = { ...detail(), source_mode: 'author', revision_id: rid }, pid = project(), onSubmit = submit()
  vi.mocked(apiFetch).mockResolvedValue(data)
  render(<CognitionEditor projectId={pid} chapterId={cid} currentVersion={data.version} disabled={false} pending={false} onSubmit={onSubmit} />)
  await screen.findByLabelText('认知内容')
  fireEvent.change(screen.getByLabelText('认知内容'), { target: { value: '仍保留的草稿' } })
  fireEvent.click(screen.getByText('恢复自动提取'))
  expect(onSubmit).toHaveBeenCalledWith({ operation: 'restore_auto', chapter_id: cid, source_hash: data.source_hash, version: data.version })
  expect(readCognitionDraft(pid, cid)?.rows[0].fact).toBe('仍保留的草稿')
})

it('does not let a late acknowledged edit clear text entered after submission', async () => {
  const pid = project()
  let current = detail(), finish!: (value: unknown) => void
  let body!: { request_id: string; changes: CognitionDetail['changes'] }
  vi.mocked(apiFetch).mockImplementation(async (path, options) => {
    if (options?.method === 'POST') {
      body = JSON.parse(String(options.body))
      return new Promise(resolve => { finish = resolve })
    }
    if (path.endsWith('/backfills')) return { backfills: [] }
    if (path.includes('include_source=true')) return current
    return { chapters: [current], total: 1, has_more: false }
  })
  render(<CognitionPanel projectId={pid} />)
  fireEvent.click(await screen.findByText('校订认知条目'))
  await screen.findByLabelText('认知内容')
  fireEvent.change(screen.getByLabelText('认知类型'), { target: { value: 'believes' } })
  fireEvent.click(screen.getByText('保存作者校订'))
  fireEvent.change(screen.getByLabelText('认知内容'), { target: { value: '等待响应时又改了一句' } })
  current = { ...current, source_mode: 'author', version: 'c'.repeat(64), revision_id: rid, changes: body.changes }
  await act(async () => { finish({ request_id: body.request_id, operation: 'edit', outcome: 'saved',
    chapter_id: cid, record_id: cid, cycle_id: rid, source_hash: current.source_hash, revision_id: rid, source_mode: 'author' }) })
  await screen.findByText('作者校订已生效')
  expect((screen.getByLabelText('认知内容') as HTMLTextAreaElement).value).toBe('等待响应时又改了一句')
  expect(readCognitionDraft(pid, cid)?.rows[0].fact).toBe('等待响应时又改了一句')
})

it('shows historical author decisions with full quotes and their source status', async () => {
  const evidence = '沈青以为信已寄出。'.repeat(80)
  vi.mocked(apiFetch).mockResolvedValue({ has_more: false, revisions: [{ revision_id: rid, mode: 'author',
    changes: [{ character: '沈青', believes: '信已寄出', evidence }], actor: '作者甲',
    created_at: '2026-10-08T12:00:00Z', valid: true, matches_current_source: false, is_current: false, model_snapshot: null }] })
  render(<CognitionHistory projectId={project()} chapterId={cid} currentVersion={'a'.repeat(64)} />)
  const history = screen.getByRole('region', { name: '认知校订历史' })
  await within(history).findByText(/当时确信/)
  expect(history.querySelector('blockquote')?.textContent).toBe(evidence)
  expect(within(history).getByText(/当前原文已变化或不可用/)).toBeTruthy()
})
