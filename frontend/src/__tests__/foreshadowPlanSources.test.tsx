import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { ForeshadowPlanList } from '@/components/panels/ForeshadowPlanList'
import { ForeshadowPanel } from '@/components/panels/ForeshadowPanel'
import { apiFetch } from '@/lib/api'

vi.mock('@/lib/api', () => ({ apiFetch: vi.fn() }))
vi.mock('@/lib/i18n/I18nProvider', () => ({ useT: () => (key: string) => key }))
vi.mock('@/components/panels/ForeshadowReviewList', () => ({ ForeshadowReviewList: () => null }))
afterEach(() => { cleanup(); vi.resetAllMocks() })

const plan = { id: 'plan1', description: '保留一张存根', hint: '交还时核对笔迹',
  source_kind: 'volume_outline', is_confirmed: false, source_label: '第2卷卷级计划（未指定章节）' }
const page = { plans: [plan], total: 1, has_more: false, unavailable_sources: 0 }

it('loads plans when opened and preserves complete descriptions and their actual source', async () => {
  const longHint = '应当核对的细节。'.repeat(100)
  vi.mocked(apiFetch).mockResolvedValue({ ...page, plans: [{ ...plan, hint: longHint }] })
  render(<ForeshadowPlanList projectId="p" refreshVersion={0} />)
  expect(apiFetch).not.toHaveBeenCalled()
  fireEvent.click(screen.getByText('大纲中的伏笔计划 展开'))
  expect(await screen.findByText('保留一张存根')).toBeTruthy()
  expect(screen.getByText(/未指定章节.*待确认卷纲/)).toBeTruthy()
  expect(screen.getByText(`回应计划：${longHint}`)).toBeTruthy()
  expect(screen.queryByText(/第 0 章/)).toBeNull()
})

it('keeps the last displayed plan on failed refresh and applies a later author revision', async () => {
  vi.mocked(apiFetch).mockResolvedValueOnce(page).mockRejectedValueOnce(new Error('offline'))
    .mockResolvedValueOnce({ ...page, plans: [{ ...plan, id: 'revised', description: '作者新安排' }] })
  render(<ForeshadowPlanList projectId="p" refreshVersion={0} />)
  fireEvent.click(screen.getByText('大纲中的伏笔计划 展开'))
  await screen.findByText('保留一张存根')
  fireEvent.click(screen.getByText('刷新大纲计划'))
  expect(await screen.findByRole('alert')).toHaveProperty('textContent', expect.stringContaining('读取失败'))
  expect(screen.getByText('保留一张存根')).toBeTruthy()
  expect(screen.queryByText('当前大纲没有可列出的伏笔计划。')).toBeNull()
  fireEvent.click(screen.getByText('刷新大纲计划'))
  expect(await screen.findByText('作者新安排')).toBeTruthy()
  expect(screen.queryByText('保留一张存根')).toBeNull()
})

it('pages plans and reports source problems without silently showing a complete empty result', async () => {
  vi.mocked(apiFetch).mockResolvedValueOnce({ ...page, total: 21, has_more: true, unavailable_sources: 1 })
    .mockResolvedValueOnce({ ...page, plans: [{ ...plan, id: 'last', description: '最后一条' }], total: 21 })
  render(<ForeshadowPlanList projectId="p" refreshVersion={0} />)
  fireEvent.click(screen.getByText('大纲中的伏笔计划 展开'))
  await screen.findByText('保留一张存根')
  expect(screen.getByText(/有 1 处大纲计划/)).toBeTruthy()
  fireEvent.click(screen.getByText('下一页计划'))
  expect(await screen.findByText('最后一条')).toBeTruthy()
  expect(vi.mocked(apiFetch).mock.calls[1][0]).toContain('offset=20')
  expect(screen.queryByText('保留一张存根')).toBeNull()
})

it('discards a late response after changing projects', async () => {
  let resolve!: (value: unknown) => void
  vi.mocked(apiFetch).mockImplementationOnce(() => new Promise(yes => { resolve = yes }))
    .mockResolvedValueOnce({ ...page, plans: [{ ...plan, description: '另一作品的计划' }] })
  const view = render(<ForeshadowPlanList projectId="a" refreshVersion={0} />)
  fireEvent.click(screen.getByText('大纲中的伏笔计划 展开'))
  view.rerender(<ForeshadowPlanList projectId="b" refreshVersion={0} />)
  fireEvent.click(screen.getByText('大纲中的伏笔计划 展开'))
  await screen.findByText('另一作品的计划')
  await act(async () => resolve(page))
  expect(screen.queryByText('保留一张存根')).toBeNull()
})

it('labels historical numbers as unverified and renders a verified source with its quote', async () => {
  const old = { id: 'old', type: 'mystery', description: '旧记号', planted_chapter: 0, status: 'planted', source_state: 'unknown' }
  const verified = { ...old, id: 'verified', description: '有来源的记号', planted_chapter: 51, source_state: 'current',
    source_label: '第2卷第1章（全书位置51）', source_evidence: '陈伯收起存根。' }
  vi.mocked(apiFetch).mockResolvedValue({ foreshadows: [old, verified], total: 2 })
  render(<ForeshadowPanel projectId="p" />)
  const card = (await screen.findByText('旧记号')).closest('.rounded-lg') as HTMLElement
  expect(within(card).getByText('原记录章号 0（未核实）')).toBeTruthy()
  expect(within(card).getByText(/暂不进入自动写作上下文/)).toBeTruthy()
  expect(screen.getByText('第2卷第1章（全书位置51）')).toBeTruthy()
  expect(screen.getByText('登记时原句：陈伯收起存根。')).toBeTruthy()
})
