/** AI 用量(AI usage)页面的共享类型与纯格式化工具。
 *
 * 后端契约(与页面一一对应,不要在此之外臆造字段):
 *   GET /api/usage/projects/{project_id}/summary?group_by=chapter|task_type|model|day&days=30&chapter_id=<uuid>
 *   GET /api/usage/chapters/{chapter_id}/budget?limit_tokens=100000
 *
 * 规则:
 *   total_tokens = input_tokens + output_tokens
 *   cache_hit_rate = cached_input_tokens / input_tokens
 */

export type UsageGroupBy = 'chapter' | 'task_type' | 'model' | 'day'

export interface UsageTotals {
  calls: number
  input_tokens: number
  cached_input_tokens: number
  output_tokens: number
  total_tokens: number
  cache_hit_rate: number
  // 2026-11-17: 供应商没有返回 usage 时后端用字符数估算,这两项标明"含估算"。
  // 省略/缺省视为 0(向后兼容旧响应)。
  estimated_calls?: number
  estimated_tokens?: number
}

export interface UsageGroup {
  key: string
  label: string
  calls: number
  input_tokens: number
  cached_input_tokens: number
  output_tokens: number
  total_tokens: number
  estimated_calls?: number
  estimated_tokens?: number
}

export interface UsageSummary {
  project_id: string
  group_by: UsageGroupBy
  totals: UsageTotals
  groups: UsageGroup[]
}

export interface UsageBudgetStep {
  task_type: string
  calls: number
  total_tokens: number
}

export interface ChapterUsageBudget {
  chapter_id: string
  used_tokens: number
  input_tokens: number
  cached_input_tokens: number
  output_tokens: number
  calls: number
  limit_tokens: number
  exceeded: boolean
  by_step: UsageBudgetStep[]
}

export const GROUP_BY_OPTIONS: Array<{ value: UsageGroupBy; label: string }> = [
  { value: 'chapter', label: '章节' },
  { value: 'task_type', label: '步骤' },
  { value: 'model', label: '模型' },
  { value: 'day', label: '按天' },
]

export const GROUP_BY_HEADING: Record<UsageGroupBy, string> = {
  chapter: '按章节分布',
  task_type: '按步骤分布',
  model: '按模型分布',
  day: '按天分布',
}

/** "缓存数据缺失时显示未知" —— 页面与测试共用的唯一判定。 */
export const CACHE_UNKNOWN_HINT = '缓存数据缺失时显示未知,不能当 0'

export const CACHE_UNKNOWN_TEXT = '未记录'

/** 千分位整数(不可用值显示破折号)。 */
export function formatExact(value: number | null | undefined): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '—'
  return Math.round(value).toLocaleString('en-US')
}

/** 千分位 + k/M 缩写:1234 → "1,234";123456 → "123.4k";1234567 → "1.2M"。 */
export function formatTokens(value: number | null | undefined): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '—'
  const rounded = Math.round(value)
  const abs = Math.abs(rounded)
  if (abs < 10_000) return formatExact(rounded)
  if (abs < 1_000_000) return `${(rounded / 1_000).toFixed(1)}k`
  return `${(rounded / 1_000_000).toFixed(1)}M`
}

/**
 * 缓存命中率。
 *
 * 关键区别:`cached_input_tokens === 0 && input_tokens > 0` 既可能是"真的 0 命中",
 * 也可能是"后端没有记录缓存字段",两者无法区分,因此统一显示"未记录"而不是 0%。
 * 只有确实观察到缓存计数(>0)时才给出百分比。
 */
export function cacheRateInfo(
  inputTokens: number | null | undefined,
  cachedInputTokens: number | null | undefined,
): { known: boolean; rate: number | null } {
  const input = typeof inputTokens === 'number' && Number.isFinite(inputTokens) ? inputTokens : 0
  const cached = typeof cachedInputTokens === 'number' && Number.isFinite(cachedInputTokens) ? cachedInputTokens : 0
  if (input <= 0 || cached <= 0) return { known: false, rate: null }
  // 契约规定 cache_hit_rate = cached_input_tokens / input_tokens,直接按此计算,
  // 避免后端缺失/异常字段把百分比放大成错误值。
  return { known: true, rate: cached / input }
}

/** 保留 1 位的百分比;缓存数据缺失时为"未记录"。 */
export function formatCacheRate(
  inputTokens: number | null | undefined,
  cachedInputTokens: number | null | undefined,
): string {
  const info = cacheRateInfo(inputTokens, cachedInputTokens)
  if (!info.known || info.rate === null) return CACHE_UNKNOWN_TEXT
  return `${(info.rate * 100).toFixed(1)}%`
}

/** 表格占比条宽度(0–100),max 为 0 时视为无数据。 */
export function barPercent(value: number | null | undefined, max: number | null | undefined): number {
  const v = typeof value === 'number' && Number.isFinite(value) ? value : 0
  const m = typeof max === 'number' && Number.isFinite(max) ? max : 0
  if (m <= 0 || v <= 0) return 0
  return Math.min(100, (v / m) * 100)
}

export interface UsageSummaryQuery {
  groupBy: UsageGroupBy
  days: number
  chapterId?: string
}

export function usageSummaryPath(projectId: string, query: UsageSummaryQuery): string {
  const params = new URLSearchParams()
  params.set('group_by', query.groupBy)
  params.set('days', String(query.days))
  if (query.chapterId) params.set('chapter_id', query.chapterId)
  return `/api/usage/projects/${encodeURIComponent(projectId)}/summary?${params.toString()}`
}

export function usageBudgetPath(chapterId: string, limitTokens: number): string {
  return `/api/usage/chapters/${encodeURIComponent(chapterId)}/budget?limit_tokens=${encodeURIComponent(String(limitTokens))}`
}