import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { ArcManuscriptReview } from '@/components/arc/ArcManuscriptReview'
import type { ArcProgressSession, ArcProgressState } from '@/components/arc/arcProgressSession'
import { arcFixture, arcManuscript } from './fixtures/arcProgress'

const mocks = vi.hoisted(() => ({ read: vi.fn(), draft: vi.fn() }))
vi.mock('@/lib/chapterManuscripts', () => ({ readManuscript: mocks.read }))
vi.mock('@/components/editor/useChapterDraft', () => ({ useChapterDraft: mocks.draft }))
afterEach(cleanup)
beforeEach(() => vi.clearAllMocks())

function setup() {
  const fixture = arcFixture(), manuscript = arcManuscript()
  const draftState = { source: manuscript, text: manuscript.content_text }
  const draft = { canGenerate: vi.fn(() => true), getSnapshot: () => draftState, reload: vi.fn() }
  mocks.draft.mockReturnValue({ session: draft, state: draftState })
  mocks.read.mockResolvedValue(manuscript)
  const session = { submit: vi.fn(async () => fixture.receipt), editNote: vi.fn() } as unknown as ArcProgressSession
  const state: ArcProgressState = { view: fixture.before, notes: {}, direction: '', closingNote: '',
    pending: null, receipt: null, initialized: true, loading: false, busy: false, error: null, storageError: null, unreadableDraft: null }
  const props = { projectId: manuscript.project_id, chapterId: manuscript.chapter_id, session, state, onOpenChapter: vi.fn() }
  return { fixture, manuscript, props, session, state, draft, draftState }
}

it('requires an explicit author reading of the entire saved manuscript, then submits that exact source', async () => {
  const h = setup(); render(<ArcManuscriptReview {...h.props} />)
  const text = await screen.findByRole('textbox', { name: '本章完整保存稿' })
  expect((text as HTMLTextAreaElement).value).toBe(h.manuscript.content_text)
  fireEvent.click(screen.getByRole('button', { name: '确认这份保存稿' }))
  expect(h.session.submit).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('checkbox'))
  fireEvent.click(screen.getByRole('button', { name: '确认这份保存稿' }))
  await waitFor(() => expect(h.session.submit).toHaveBeenCalledWith('acknowledge', h.manuscript))
})

it.each(['dirty', 'saving', 'title_changed', 'closed', 'unknown_request', 'empty'])('cannot confirm a %s manuscript', async kind => {
  const h = setup()
  if (kind === 'dirty') h.draftState.text = '编辑区尚未保存的稿。'
  if (kind === 'saving') h.draft.canGenerate.mockReturnValue(false)
  if (kind === 'title_changed') h.draftState.source = { ...h.manuscript, title: '作者新标题' }
  if (kind === 'closed') h.state.view!.progress.phase = 'completed'
  if (kind === 'unknown_request') h.state.pending = { body: JSON.stringify(h.fixture.input), sent: true, rejected: false }
  if (kind === 'empty') { h.manuscript.content_text = ''; h.draftState.text = ''; mocks.read.mockResolvedValue(h.manuscript) }
  render(<ArcManuscriptReview {...h.props} />)
  await screen.findByRole('textbox', { name: '本章完整保存稿' })
  fireEvent.click(screen.getByRole('checkbox'))
  fireEvent.click(screen.getByRole('button', { name: '确认这份保存稿' }))
  expect(h.session.submit).not.toHaveBeenCalled()
})

it('does not let a late manuscript response from another chapter populate the new review', async () => {
  const h = setup()
  let deliver!: (value: unknown) => void
  mocks.read.mockImplementationOnce(() => new Promise(resolve => { deliver = resolve }))
  const first = render(<ArcManuscriptReview {...h.props} />)
  const later = { ...h.manuscript, chapter_id: h.fixture.before.chapters[1].id, content_text: '当前第二章的完整保存稿。' }
  mocks.read.mockResolvedValue(later)
  first.rerender(<ArcManuscriptReview {...h.props} chapterId={later.chapter_id} />)
  await waitFor(() => expect((screen.getByRole('textbox', { name: '本章完整保存稿' }) as HTMLTextAreaElement).value).toBe(later.content_text))
  deliver(h.manuscript)
  await waitFor(() => expect((screen.getByRole('textbox', { name: '本章完整保存稿' }) as HTMLTextAreaElement).value).toBe(later.content_text))
  expect(h.session.submit).not.toHaveBeenCalled()
})
