import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import UsagePage from '@/app/usage/page'
import { apiFetch, ApiError } from '@/lib/api'
import {
  barPercent,
  cacheRateInfo,
  formatCacheRate,
  formatExact,
  formatTokens,
  usageBudgetPath,
  usageSummaryPath,
} from '@/lib/usageTypes'

vi.mock('@/lib/api', async (original) => ({
  ...await original<typeof import('@/lib/api')>(),
  apiFetch: vi.fn(),
}))

const api = vi.mocked(apiFetch)

const PROJECT = { id: 'p1', title: '测试书' }
const CHAPTERS = [
  { id: 'c1', title: '第一章', chapter_idx: 1 },
  { id: 'c2', title: '第二章', chapter_idx: 2 },
]

type Overrides = { cached?: number; fail?: 404 | 500; empty?: boolean; budgetExceeded?: boolean; estimatedCalls?: number }

function setup(options: Overrides = {}) {
  const cached = options.cached ?? 45_000
  api.mockImplementation(async (path: string) => {
    if (path === '/api/projects') return { projects: [PROJECT] }
    if (path.includes('/chapters?lightweight=true')) return CHAPTERS
    if (path.startsWith('/api/usage/projects/')) {
      if (options.fail === 404) throw new ApiError('Not Found', 404)
      if (options.fail === 500) throw new ApiError('internal error', 500)
      const groupBy = new URL(path, 'http://test').searchParams.get('group_by')
      if (options.empty) {
        return {
          project_id: 'p1', group_by: groupBy,
          totals: { calls: 0, input_tokens: 0, cached_input_tokens: 0, output_tokens: 0, total_tokens: 0, cache_hit_rate: 0 },
          groups: [],
        }
      }
      return {
        project_id: 'p1',
        group_by: groupBy,
        totals: {
          calls: 12, input_tokens: 100_000, cached_input_tokens: cached,
          output_tokens: 50_000, total_tokens: 150_000, cache_hit_rate: cached / 100_000,
          ...(options.estimatedCalls
            ? { estimated_calls: options.estimatedCalls, estimated_tokens: 12_345 }
            : {}),
        },
        // 后端已按 total_tokens 降序;前端必须保持这个顺序。
        groups: [
          { key: 'c2', label: '第二章', calls: 7, input_tokens: 80_000, cached_input_tokens: cached, output_tokens: 40_000, total_tokens: 120_000 },
          { key: 'c1', label: '第一章', calls: 5, input_tokens: 20_000, cached_input_tokens: 0, output_tokens: 10_000, total_tokens: 30_000 },
        ],
      }
    }
    if (path.startsWith('/api/usage/chapters/')) {
      const limit = Number(new URL(path, 'http://test').searchParams.get('limit_tokens') ?? 100_000)
      return {
        chapter_id: 'c1', used_tokens: 120_000, input_tokens: 90_000, cached_input_tokens: 45_000,
        output_tokens: 30_000, calls: 4, limit_tokens: limit,
        exceeded: options.budgetExceeded ?? true,
        by_step: [
          { task_type: 'generation', calls: 3, total_tokens: 100_000 },
          { task_type: 'polishing', calls: 1, total_tokens: 20_000 },
        ],
      }
    }
    throw new Error(`unexpected path: ${path}`)
  })
}

function summaryPaths(): string[] {
  return api.mock.calls.map(([path]) => path).filter((p) => p.startsWith('/api/usage/projects/'))
}

beforeEach(() => {
  api.mockReset()
  setup()
})

afterEach(() => {
  cleanup()
})

describe('usage 格式化工具', () => {
  it('千分位与 k/M 缩写', () => {
    expect(formatExact(1234)).toBe('1,234')
    expect(formatExact(0)).toBe('0')
    expect(formatTokens(1234)).toBe('1,234')
    expect(formatTokens(9_999)).toBe('9,999')
    expect(formatTokens(123_400)).toBe('123.4k')
    expect(formatTokens(1_234_567)).toBe('1.2M')
    expect(formatTokens(undefined)).toBe('—')
  })

  it('缓存缺失一律"未记录",真实命中才给百分比', () => {
    expect(formatCacheRate(100_000, 0)).toBe('未记录')
    expect(formatCacheRate(0, 0)).toBe('未记录')
    expect(formatCacheRate(0, 500)).toBe('未记录')
    expect(formatCacheRate(100_000, 45_000)).toBe('45.0%')
    expect(cacheRateInfo(100_000, 0)).toEqual({ known: false, rate: null })
    expect(cacheRateInfo(100_000, 45_000).rate).toBeCloseTo(0.45)
  })

  it('占比条与契约路径', () => {
    expect(barPercent(120_000, 120_000)).toBe(100)
    expect(barPercent(30_000, 120_000)).toBe(25)
    expect(barPercent(5, 0)).toBe(0)
    expect(usageSummaryPath('p1', { groupBy: 'task_type', days: 30 }))
      .toBe('/api/usage/projects/p1/summary?group_by=task_type&days=30')
    expect(usageSummaryPath('p1', { groupBy: 'chapter', days: 7, chapterId: 'c1' }))
      .toBe('/api/usage/projects/p1/summary?group_by=chapter&days=7&chapter_id=c1')
    expect(usageBudgetPath('c1', 100_000)).toBe('/api/usage/chapters/c1/budget?limit_tokens=100000')
  })
})

describe('AI 用量页面', () => {
  it('渲染总览卡与分组表格,保持后端顺序', async () => {
    render(<UsagePage />)

    await waitFor(() => expect(screen.getByTestId('usage-stat-total').textContent).toContain('150.0k'))

    const total = screen.getByTestId('usage-stat-total')
    expect(total.textContent).toContain('150.0k')
    expect(total.textContent).toContain('精确 150,000')

    expect(screen.getByTestId('usage-stat-input').textContent).toContain('100.0k')
    expect(screen.getByTestId('usage-stat-output').textContent).toContain('50.0k')
    expect(screen.getByTestId('usage-stat-cache').textContent).toContain('45.0%')
    expect(screen.getByTestId('usage-stat-calls').textContent).toContain('12')

    const rows = screen.getAllByRole('row')
    expect(rows).toHaveLength(3)
    expect(rows[1].textContent).toContain('第二章')
    expect(rows[1].textContent).toContain('120.0k')
    expect(rows[2].textContent).toContain('第一章')
    // 该行 input>0 但 cached=0 → "未记录",不是 0%
    expect(rows[2].textContent).toContain('未记录')
    expect(rows[2].textContent).not.toContain('0.0%')
  })

  it('进入页面只请求一次,点击刷新才再次请求', async () => {
    render(<UsagePage />)
    await waitFor(() => expect(summaryPaths().length).toBe(1))
    await new Promise((resolve) => setTimeout(resolve, 80))
    expect(summaryPaths().length).toBe(1)

    fireEvent.click(screen.getByRole('button', { name: '刷新' }))
    await waitFor(() => expect(summaryPaths().length).toBe(2))
  })

  it('分组切换请求对应的 group_by', async () => {
    render(<UsagePage />)
    await waitFor(() => expect(summaryPaths().length).toBe(1))
    expect(screen.getByText('按章节分布')).toBeTruthy()

    fireEvent.click(screen.getByTestId('usage-group-tab-model'))
    await waitFor(() => expect(summaryPaths().some((p) => p.includes('group_by=model'))).toBe(true))
    expect(screen.getByText('按模型分布')).toBeTruthy()

    fireEvent.click(screen.getByTestId('usage-group-tab-task_type'))
    await waitFor(() => expect(summaryPaths().some((p) => p.includes('group_by=task_type'))).toBe(true))
    expect(screen.getByText('按步骤分布')).toBeTruthy()

    fireEvent.click(screen.getByTestId('usage-group-tab-day'))
    await waitFor(() => expect(summaryPaths().some((p) => p.includes('group_by=day'))).toBe(true))
    expect(screen.getByText('按天分布')).toBeTruthy()
  })

  it('缓存数据缺失时总览显示"未记录"并注明原因', async () => {
    setup({ cached: 0 })
    render(<UsagePage />)

    await waitFor(() => expect(screen.getByTestId('usage-stat-cache')).toBeTruthy())
    const card = screen.getByTestId('usage-stat-cache')
    expect(card.textContent).toContain('未记录')
    expect(card.textContent).not.toContain('0.0%')
    expect(card.textContent).toContain('缓存数据缺失时显示未知,不能当 0')
  })

  it('接口 5xx 显示可读中文提示而不是白屏', async () => {
    setup({ fail: 500 })
    render(<UsagePage />)

    const error = await screen.findByTestId('usage-summary-error')
    expect(error.textContent).toContain('服务端错误')
    expect(error.textContent).toContain('HTTP 500')
    expect(screen.getByRole('heading', { name: 'AI 用量' })).toBeTruthy()
  })

  it('接口 404 提示用量服务可能未上线', async () => {
    setup({ fail: 404 })
    render(<UsagePage />)

    const error = await screen.findByTestId('usage-summary-error')
    expect(error.textContent).toContain('接口不存在')
    expect(error.textContent).toContain('HTTP 404')
  })

  it('空数据显示中文空态', async () => {
    setup({ empty: true })
    render(<UsagePage />)

    await waitFor(() => expect(screen.getByTestId('usage-stat-total').textContent).toContain('0'))
    expect(screen.getByTestId('usage-empty').textContent).toContain('最近 30 天没有用量记录')
  })

  it('选择单章后展示预算与按步骤明细', async () => {
    render(<UsagePage />)

    await waitFor(() => expect(screen.getByLabelText('章节')).not.toBeDisabled())
    fireEvent.change(screen.getByLabelText('章节'), { target: { value: 'c1' } })

    await waitFor(() => expect(screen.getByTestId('usage-budget')).toBeTruthy())
    const urls = api.mock.calls.map(([path]) => path)
    expect(urls).toContain('/api/usage/chapters/c1/budget?limit_tokens=100000')
    expect(summaryPaths().some((p) => p.includes('chapter_id=c1'))).toBe(true)

    const budget = screen.getByTestId('usage-budget')
    expect(budget.textContent).toContain('单章预算 · 第一章')
    expect(budget.textContent).toContain('120.0k')
    expect(budget.textContent).toContain('generation')
    expect(screen.getByTestId('usage-budget-exceeded').textContent).toContain('已超限')
  })
})

describe('供应商未回传 usage 时的估算标注', () => {
  it('有估算调用时显示估算说明,并说明费用以厂商账单为准', async () => {
    setup({ estimatedCalls: 34 })
    render(<UsagePage />)
    const note = await screen.findByTestId('usage-estimated-note')
    expect(note.textContent).toContain('34')
    expect(note.textContent).toContain('估算')
    expect(note.textContent).toContain('厂商账单')
  })

  it('没有估算调用时不出现该说明', async () => {
    setup()
    render(<UsagePage />)
    await screen.findByTestId('usage-stat-total')
    expect(screen.queryByTestId('usage-estimated-note')).toBeNull()
  })
})
