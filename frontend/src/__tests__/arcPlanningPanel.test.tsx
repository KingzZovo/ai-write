import { render, screen } from '@testing-library/react'
import { expect, it, vi } from 'vitest'
import fixture from './fixtures/arcPlanning.contract.json'
import type { ArcPlanningState } from '@/components/arc/arcPlanningSession'
import { ArcPlanningPanel } from '@/components/arc/ArcPlanningPanel'

const { holder } = vi.hoisted(() => ({ holder: { state: null as ArcPlanningState | null } }))
vi.mock('@/components/arc/arcPlanningSession', () => ({ getArcPlanningSession: () => ({
  subscribe: () => () => {}, getSnapshot: () => holder.state, attach: vi.fn(),
  reload: vi.fn(), command: vi.fn(), edit: vi.fn(), select: vi.fn(), recoverStorage: vi.fn(),
}) }))

function state(): ArcPlanningState {
  return { draft: { ...fixture.input, target_chapters: '4' }, pending: null, selected: fixture.input.request_id,
    source: null, status: fixture.after as ArcPlanningState['status'], detail: fixture.after as ArcPlanningState['detail'],
    initialized: true, busy: false, loading: false, sourceLoading: false, error: null, storageError: null, unreadable: null }
}

it('renders all accepted chapter plans and original author material without clipping', () => {
  holder.state = state()
  render(<ArcPlanningPanel projectId={fixture.after.project_id} />)
  for (const chapter of fixture.after.accepted_outline.chapter_summaries) {
    expect(screen.getByText(`第 ${chapter.chapter_idx} 章 · ${chapter.title}`)).toBeTruthy()
    expect(screen.getAllByText(chapter.summary).length).toBeGreaterThan(0)
  }
  expect(screen.getByText(fixture.input.background)).toBeTruthy()
  expect(screen.getByText('全部章草稿已保存。请先审读剧情安排，再开始正文创作。')).toBeTruthy()
})

it('keeps a damaged original visible for download and blocks resume', () => {
  holder.state = state()
  holder.state.status = { ...holder.state.status!, status: 'needs_review', result: null }
  holder.state.detail = { ...holder.state.detail!, status: 'needs_review', result: null, integrity_valid: false, can_resume: false }
  render(<ArcPlanningPanel projectId={fixture.after.project_id} />)
  expect((screen.getByRole('button', { name: '恢复原任务' }) as HTMLButtonElement).disabled).toBe(true)
  expect(screen.getByRole('button', { name: '下载完整记录' })).toBeTruthy()
  expect(screen.getByText('原记录无法完整核对。请下载保留材料，当前记录不会用于继续生成。')).toBeTruthy()
})

it('shows the complete book material actually supplied to this request', () => {
  holder.state = state()
  const original = '书纲中段约定：借据留在姐姐的柜底，本弧不提前兑现远期结局。'.repeat(300)
  holder.state.detail = { ...holder.state.detail!, book_material_text: original }
  render(<ArcPlanningPanel projectId={fixture.after.project_id} />)
  expect(screen.getByText('本次传入的全书创作材料')).toBeTruthy()
  expect(screen.getByText(original)).toBeTruthy()
})
