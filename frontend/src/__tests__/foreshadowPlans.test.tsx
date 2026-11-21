import { cleanup, render, screen, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { ForeshadowPanel } from '@/components/panels/ForeshadowPanel'
import { apiFetch } from '@/lib/api'
vi.mock('@/lib/api', () => ({ apiFetch: vi.fn() }))
vi.mock('@/lib/i18n/I18nProvider', () => ({ useT: () => (key: string) => key }))
afterEach(() => { cleanup(); vi.resetAllMocks() })
it('renders an outline plan as pending prose rather than planted or mature', async () => {
  vi.mocked(apiFetch).mockResolvedValue({ total: 1, foreshadows: [{
    id: 'plan', type: '伏笔', description: '蓝色印记计划', planted_chapter: 3,
    status: 'planned', narrative_proximity: 1, resolved_chapter: null,
  }] })
  render(<ForeshadowPanel projectId="p" />)
  const description = await screen.findByText('蓝色印记计划')
  const card = description.closest('.rounded-lg') as HTMLElement
  expect(screen.getByText(/计划埋设（待正文落实）/)).toBeTruthy()
  expect(within(card).getByText('计划中')).toBeTruthy()
  expect(within(card).queryByText('接近度')).toBeNull()
  expect(within(card).queryByText('已埋')).toBeNull()
})

it.each([
  ['planted', 12, false],
  ['resolved', 0, true],
  ['resolved', null, false],
] as const)('shows payoff chapter only for resolved records: %s / %s', async (status, chapter, showPayoff) => {
  vi.mocked(apiFetch).mockResolvedValue({ total: 1, foreshadows: [{
    id: 'record', type: '伏笔', description: '门上的旧印记', planted_chapter: 0,
    status, narrative_proximity: 1, resolved_chapter: chapter,
  }] })
  const { fireEvent } = await import('@testing-library/react')
  render(<ForeshadowPanel projectId="p" />)
  fireEvent.click(screen.getByText('foreshadow.filter.all'))
  const description = await screen.findByText('门上的旧印记')
  const card = description.closest('.rounded-lg') as HTMLElement
  expect(Boolean(within(card).queryByText(/登记收线章号：/))).toBe(showPayoff)
})

it('shows a ready record as requiring review and does not turn its legacy score into a payoff meter', async () => {
  vi.mocked(apiFetch).mockResolvedValue({ total: 1, foreshadows: [{
    id: 'clue', type: '伏笔', description: '寄信人的存根', planted_chapter: 3,
    status: 'ready', narrative_proximity: 0.99, resolved_chapter: null,
  }] })
  render(<ForeshadowPanel projectId="p" />)
  const card = (await screen.findByText('寄信人的存根')).closest('.rounded-lg') as HTMLElement
  expect(within(card).getByText('待核对收线')).toBeTruthy()
  expect(within(card).queryByText('接近度')).toBeNull()
  expect(within(card).queryByText('99%')).toBeNull()
  expect(within(card).queryByText('该收了')).toBeNull()
})
