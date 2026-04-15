'use client'

import React, { useState, useCallback } from 'react'
import { apiFetch } from '@/lib/api'

// ----------------------------------------------------------------
// Types
// ----------------------------------------------------------------

interface CheckerIssue {
  type: string
  severity: string
  location: string
  description: string
  suggestion: string
}

interface CheckerResult {
  checker_name: string
  score: number
  passed: boolean
  issues: CheckerIssue[]
}

interface CheckerDashboardProps {
  chapterId: string
}

// ----------------------------------------------------------------
// Constants
// ----------------------------------------------------------------

const CHECKER_CHINESE: Record<string, string> = {
  consistency: '一致性',
  continuity: '连续性',
  ooc: '角色OOC',
  pacing: '节奏',
  reader_pull: '追读力',
  anti_ai: '去AI味',
}

const CHECKER_ORDER = ['consistency', 'continuity', 'ooc', 'pacing', 'reader_pull', 'anti_ai']

const SEVERITY_STYLES: Record<string, { bg: string; text: string; label: string }> = {
  high: { bg: 'bg-red-50', text: 'text-red-700', label: '严重' },
  medium: { bg: 'bg-amber-50', text: 'text-amber-700', label: '警告' },
  low: { bg: 'bg-stone-50', text: 'text-stone-600', label: '建议' },
}

// ----------------------------------------------------------------
// Score ring SVG (small arc indicator)
// ----------------------------------------------------------------

function ScoreRing({ score, size = 40 }: { score: number; size?: number }) {
  const radius = (size - 6) / 2
  const circumference = 2 * Math.PI * radius
  const progress = (score / 10) * circumference
  const color =
    score >= 8 ? 'stroke-emerald-500' : score >= 6 ? 'stroke-amber-500' : 'stroke-red-500'

  return (
    <svg width={size} height={size} className="transform -rotate-90">
      <circle
        cx={size / 2}
        cy={size / 2}
        r={radius}
        fill="none"
        stroke="currentColor"
        className="text-stone-100"
        strokeWidth={3}
      />
      <circle
        cx={size / 2}
        cy={size / 2}
        r={radius}
        fill="none"
        className={color}
        strokeWidth={3}
        strokeDasharray={circumference}
        strokeDashoffset={circumference - progress}
        strokeLinecap="round"
      />
    </svg>
  )
}

// ----------------------------------------------------------------
// Status dot
// ----------------------------------------------------------------

function StatusDot({ passed, score }: { passed: boolean; score: number }) {
  if (passed && score >= 8) {
    return <span className="inline-block w-2 h-2 rounded-full bg-emerald-500" />
  }
  if (passed || score >= 6) {
    return <span className="inline-block w-2 h-2 rounded-full bg-amber-400" />
  }
  return <span className="inline-block w-2 h-2 rounded-full bg-red-500" />
}

// ----------------------------------------------------------------
// Component
// ----------------------------------------------------------------

export function CheckerDashboard({ chapterId }: CheckerDashboardProps) {
  const [results, setResults] = useState<CheckerResult[] | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [expandedChecker, setExpandedChecker] = useState<string | null>(null)
  const [lastChecked, setLastChecked] = useState<string | null>(null)

  const handleRunCheck = useCallback(async () => {
    if (loading) return
    setLoading(true)
    setError(null)
    try {
      const data = await apiFetch<{ results: CheckerResult[] }>(
        `/api/chapters/${chapterId}/check-quality`,
        { method: 'POST' }
      )
      setResults(data.results)
      setLastChecked(new Date().toISOString())
    } catch (err) {
      setError(err instanceof Error ? err.message : '检查失败')
    } finally {
      setLoading(false)
    }
  }, [chapterId, loading])

  // Calculate overall score
  const overallScore =
    results && results.length > 0
      ? results.reduce((sum, r) => sum + r.score, 0) / results.length
      : null

  const overallColor =
    overallScore !== null
      ? overallScore >= 8
        ? 'text-emerald-700'
        : overallScore >= 6
          ? 'text-amber-700'
          : 'text-red-700'
      : 'text-stone-400'

  const overallBg =
    overallScore !== null
      ? overallScore >= 8
        ? 'bg-emerald-50/60 border-emerald-200/60'
        : overallScore >= 6
          ? 'bg-amber-50/60 border-amber-200/60'
          : 'bg-red-50/60 border-red-200/60'
      : 'bg-stone-50 border-stone-200'

  // Sort results by checker order
  const sortedResults = results
    ? CHECKER_ORDER.map((name) => results.find((r) => r.checker_name === name)).filter(
        Boolean
      ) as CheckerResult[]
    : []

  return (
    <div className="space-y-3">
      {/* Overall score display */}
      {overallScore !== null && (
        <div className={`rounded-xl border p-4 text-center ${overallBg}`}>
          <div
            className={`text-3xl font-serif font-bold tracking-tight ${overallColor}`}
            style={{ fontFamily: "'Noto Serif SC', 'Georgia', serif" }}
          >
            {overallScore.toFixed(1)}
          </div>
          <div className="text-[11px] text-stone-500 mt-0.5 tracking-wide">
            综合质量评分 / 10
          </div>
        </div>
      )}

      {/* 2x3 checker grid */}
      {sortedResults.length > 0 && (
        <div className="grid grid-cols-2 gap-2">
          {sortedResults.map((result) => {
            const isExpanded = expandedChecker === result.checker_name
            const issueCount = result.issues.length
            const chineseName = CHECKER_CHINESE[result.checker_name] || result.checker_name

            return (
              <div key={result.checker_name} className="col-span-1">
                <button
                  onClick={() =>
                    setExpandedChecker(isExpanded ? null : result.checker_name)
                  }
                  className={`w-full text-left rounded-lg border p-2.5 transition-all hover:shadow-sm ${
                    isExpanded
                      ? 'border-stone-300 bg-white shadow-sm'
                      : 'border-stone-150 bg-stone-50/50 hover:bg-white'
                  }`}
                >
                  <div className="flex items-start justify-between">
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-1.5 mb-1">
                        <StatusDot passed={result.passed} score={result.score} />
                        <span className="text-xs font-medium text-stone-800 truncate">
                          {chineseName}
                        </span>
                      </div>
                      {issueCount > 0 && (
                        <span className="text-[10px] text-stone-400">
                          {issueCount} 个问题
                        </span>
                      )}
                    </div>
                    <div className="relative flex-shrink-0">
                      <ScoreRing score={result.score} size={36} />
                      <span className="absolute inset-0 flex items-center justify-center text-[10px] font-semibold text-stone-700">
                        {result.score.toFixed(0)}
                      </span>
                    </div>
                  </div>
                </button>

                {/* Expanded issue details */}
                {isExpanded && result.issues.length > 0 && (
                  <div className="mt-1.5 space-y-1.5 col-span-2">
                    {result.issues.map((issue, idx) => {
                      const sevStyle =
                        SEVERITY_STYLES[issue.severity] || SEVERITY_STYLES.low
                      return (
                        <div
                          key={idx}
                          className="bg-white border border-stone-200 rounded-lg p-2 text-xs"
                        >
                          <div className="flex items-center gap-1.5 mb-0.5">
                            <span
                              className={`px-1.5 py-0.5 rounded text-[10px] font-medium ${sevStyle.bg} ${sevStyle.text}`}
                            >
                              {sevStyle.label}
                            </span>
                            <span className="text-stone-400 truncate text-[10px]">
                              {issue.location}
                            </span>
                          </div>
                          <p className="text-stone-700 leading-relaxed mt-1">
                            {issue.description}
                          </p>
                          {issue.suggestion && (
                            <p className="text-amber-700 mt-1 leading-relaxed text-[11px]">
                              {issue.suggestion}
                            </p>
                          )}
                        </div>
                      )
                    })}
                  </div>
                )}
              </div>
            )
          })}
        </div>
      )}

      {/* Last checked timestamp */}
      {lastChecked && (
        <div className="text-[10px] text-stone-400 text-right">
          上次检查: {new Date(lastChecked).toLocaleString('zh-CN')}
        </div>
      )}

      {error && <p className="text-xs text-red-500">{error}</p>}

      {/* Run check button */}
      <button
        onClick={handleRunCheck}
        disabled={loading}
        className="w-full px-3 py-2 text-xs font-medium rounded-lg transition-colors
          bg-stone-800 text-white hover:bg-stone-900
          disabled:opacity-50 disabled:cursor-not-allowed"
      >
        {loading ? '检查中...' : '运行质量检查'}
      </button>

      {!results && !loading && (
        <p className="text-xs text-stone-400 text-center">
          点击上方按钮运行六维质量检查
        </p>
      )}
    </div>
  )
}
