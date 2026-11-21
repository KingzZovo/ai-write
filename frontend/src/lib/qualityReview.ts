export const CHECKER_ORDER = ['consistency', 'continuity', 'ooc', 'pacing', 'reader_pull', 'anti_ai']

export interface CheckerIssue {
  type: string
  severity: string
  location: string
  description: string
  suggestion: string
}

export interface CheckerResult {
  name: string
  score: number | null
  passed: boolean
  review_status: 'complete' | 'incomplete' | 'unknown'
  issues: CheckerIssue[]
  observations: CheckerIssue[]
}

export interface QualityReview {
  checkers: CheckerResult[]
  overall_score: number | null
  review_status: 'complete' | 'incomplete' | 'unknown'
  passed: boolean
}

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown> : null
}

function score(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 10 ? value : null
}

function issues(value: unknown): CheckerIssue[] {
  if (!Array.isArray(value)) throw new Error('检查明细不完整，请重新检查。')
  return value.map((item) => {
    const row = record(item)
    if (!row || typeof row.description !== 'string' || typeof row.severity !== 'string') {
      throw new Error('检查明细不完整，请重新检查。')
    }
    return {
      type: typeof row.type === 'string' ? row.type : '',
      severity: row.severity, description: row.description,
      location: typeof row.location === 'string' ? row.location : '',
      suggestion: typeof row.suggestion === 'string' ? row.suggestion : '',
    }
  })
}

export function parseQualityReview(value: unknown, chapterId: string): QualityReview {
  const data = record(value)
  if (!data || !Array.isArray(data.checkers) || (data.chapter_id !== undefined && data.chapter_id !== chapterId)) {
    throw new Error('检查结果与本章不匹配或不完整，请重新检查。')
  }
  const checkers: CheckerResult[] = data.checkers.map((item) => {
    const row = record(item)
    if (!row || typeof row.name !== 'string') throw new Error('检查结果缺少检查项名称，请重新检查。')
    const value = score(row.score)
    const status = row.review_status === 'complete' && value !== null && typeof row.passed === 'boolean'
      && Array.isArray(row.errors) && row.errors.length === 0
      ? 'complete' : row.review_status === undefined ? 'unknown' : 'incomplete'
    return {
      name: row.name, score: status === 'complete' ? value : null,
      passed: status === 'complete' && row.passed === true,
      review_status: status, issues: issues(row.issues), observations: issues(row.observations ?? []),
    }
  })
  const allPresent = CHECKER_ORDER.every((name) => checkers.filter((r) => r.name === name).length === 1)
  const complete = data.review_status === 'complete' && allPresent && checkers.length === CHECKER_ORDER.length
    && checkers.every((r) => r.review_status === 'complete') && score(data.overall_score) !== null
    && Array.isArray(data.errors) && data.errors.length === 0
  return {
    checkers,
    review_status: complete ? 'complete' : data.review_status === undefined ? 'unknown' : 'incomplete',
    overall_score: complete ? score(data.overall_score) : null,
    passed: complete && data.passed === true && checkers.every((r) => r.passed),
  }
}
