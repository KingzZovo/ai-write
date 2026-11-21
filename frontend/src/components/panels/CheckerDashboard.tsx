'use client'

import React, { useState, useCallback, useEffect, useRef } from 'react'
import { apiFetch } from '@/lib/api'
import { CHECKER_ORDER, parseQualityReview, type QualityReview } from '@/lib/qualityReview'

// ----------------------------------------------------------------
// Types
// ----------------------------------------------------------------

interface ChineseProseMechanics {
  passed: boolean
  pseudo_literary_register_count: number
  plain_contemporary_violation_count: number
  duplicate_explanation_span_count: number
  semantic_collocation_count: number
  resource_continuity_count: number
  scene_plausibility_count: number
  dialogue_symmetry_risk_count: number
  story_bible_leakage_count: number
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

const SEVERITY_STYLES: Record<string, { bg: string; text: string; label: string }> = {
  critical: { bg: 'bg-red-50', text: 'text-red-700', label: '严重' },
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

function StatusDot({ passed, score }: { passed: boolean; score: number | null }) {
  if (score === null) return <span className="inline-block w-2 h-2 rounded-full bg-stone-400" />
  if (!passed) return <span className="inline-block w-2 h-2 rounded-full bg-red-500" />
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
  return <ChapterChecker key={chapterId} chapterId={chapterId} />
}

function ChapterChecker({ chapterId }: CheckerDashboardProps) {
  const [review, setReview] = useState<QualityReview | null>(null)
  const [proseMechanics, setProseMechanics] = useState<ChineseProseMechanics | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [expandedChecker, setExpandedChecker] = useState<string | null>(null)
  const [lastChecked, setLastChecked] = useState<string | null>(null)
  const request = useRef<AbortController | null>(null)

  useEffect(() => () => {
    request.current?.abort()
    request.current = null
  }, [])

  const handleRunCheck = useCallback(async () => {
    if (request.current) return
    const controller = new AbortController()
    request.current = controller
    setLoading(true)
    setError(null)
    setReview(null)
    setProseMechanics(null)
    setLastChecked(null)
    setExpandedChecker(null)
    try {
      const data = await apiFetch<{
        chinese_prose_mechanics?: ChineseProseMechanics
      }>(`/api/chapters/${chapterId}/check-quality`, { method: 'POST', signal: controller.signal })
      if (request.current !== controller) return
      setReview(parseQualityReview(data, chapterId))
      setProseMechanics(data.chinese_prose_mechanics ?? null)
      setLastChecked(new Date().toISOString())
    } catch (err) {
      if (request.current === controller) setError(err instanceof Error ? err.message : '检查失败')
    } finally {
      if (request.current === controller) {
        request.current = null
        setLoading(false)
      }
    }
  }, [chapterId])

  // Use the server's complete-review diagnostic; never average away a failure.
  const results = review?.checkers ?? null
  const overallScore = review?.overall_score ?? null

  const overallColor =
    overallScore !== null
      ? review?.passed === false ? 'text-amber-700' : overallScore >= 8
        ? 'text-emerald-700'
        : overallScore >= 6
          ? 'text-amber-700'
          : 'text-red-700'
      : 'text-stone-400'

  const overallBg =
    overallScore !== null
      ? review?.passed === false ? 'bg-amber-50/60 border-amber-200/60' : overallScore >= 8
        ? 'bg-emerald-50/60 border-emerald-200/60'
        : overallScore >= 6
          ? 'bg-amber-50/60 border-amber-200/60'
          : 'bg-red-50/60 border-red-200/60'
      : 'bg-stone-50 border-stone-200'

  // Sort results by checker order
  const sortedResults = [...(results ?? [])].sort((a, b) => CHECKER_ORDER.indexOf(a.name) - CHECKER_ORDER.indexOf(b.name))

  return (
    <div className="space-y-3">
      {review && (
        <div role="status" className="rounded-lg border border-stone-200 bg-stone-50 p-3 text-xs text-stone-700">
          <p className="font-medium">
            {review.review_status === 'complete'
              ? (review.passed ? '自动检查已完成，仍需作者审读。' : '自动检查已完成，有待核对的问题。')
              : review.review_status === 'unknown' ? '检查结果缺少完整性信息，请重新检查。' : '检查未完成，暂不提供综合参考值。'}
          </p>
          <p className="mt-1 text-stone-500">对应本次检查开始时的正文。请结合原句审读人物、场面、语言与衔接，再决定怎样修改。</p>
        </div>
      )}
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
            自动检查参考值 / 10
          </div>
        </div>
      )}

      {proseMechanics && (
        <div
          className="rounded-lg border border-stone-200 bg-stone-50 p-3"
        >
          <div className="text-xs font-semibold text-stone-800 mb-2">行文观察</div>
          <p className="text-[11px] text-stone-500 mb-2">以下计数仅供结合原文复核，不代表事实错误或文学质量结论。</p>
          <div className="grid grid-cols-2 gap-x-3 gap-y-1 text-[11px] text-stone-600">
            <span>压缩表达线索：{proseMechanics.pseudo_literary_register_count}</span>
            <span>语体线索：{proseMechanics.plain_contemporary_violation_count}</span>
            <span>解释重复线索：{proseMechanics.duplicate_explanation_span_count}</span>
            <span>词语搭配线索：{proseMechanics.semantic_collocation_count}</span>
            <span>资源描述线索：{proseMechanics.resource_continuity_count}</span>
            <span>场景描述线索：{proseMechanics.scene_plausibility_count}</span>
            <span>对话对称：{proseMechanics.dialogue_symmetry_risk_count}</span>
            <span>设定名词线索：{proseMechanics.story_bible_leakage_count}</span>
          </div>
        </div>
      )}

      {/* 2x3 checker grid */}
      {sortedResults.length > 0 && (
        <div className="grid grid-cols-2 gap-2">
          {sortedResults.map((result) => {
            const isExpanded = expandedChecker === result.name
            const issueCount = result.issues.length
            const chineseName = CHECKER_CHINESE[result.name] || result.name

            return (
              <div key={result.name} className="col-span-1">
                <button
                  type="button"
                  aria-expanded={isExpanded}
                  onClick={() =>
                    setExpandedChecker(isExpanded ? null : result.name)
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
                          {issueCount} 条待核对发现
                        </span>
                      )}
                    </div>
                    {result.score !== null ? <div className="relative flex-shrink-0">
                      <ScoreRing score={result.score} size={36} />
                      <span className="absolute inset-0 flex items-center justify-center text-[10px] font-semibold text-stone-700">
                        {result.score.toFixed(0)}
                      </span>
                    </div> : <span className="text-[10px] text-stone-500">{result.review_status === 'unknown' ? '状态未知' : '未完成'}</span>}
                  </div>
                </button>

                {/* Expanded issue details */}
                {isExpanded && (
                  <div className="mt-1.5 space-y-1.5 col-span-2">
                    {result.review_status !== 'complete' && <p className="text-xs text-amber-700">本项检查未完成；已有发现可先核对，不能据此认定本稿没有其他问题。</p>}
                    {result.review_status === 'complete' && result.issues.length === 0 && <p className="text-xs text-stone-500">本项未报告具体问题，仍需结合原文审读。</p>}
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
                          </div>
                          {issue.location && (
                            <blockquote className="mt-1 border-l-2 border-stone-200 pl-2 text-stone-500 whitespace-pre-wrap break-words leading-relaxed">
                              {issue.location}
                            </blockquote>
                          )}
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
                    {result.observations.length > 0 && <div className="text-xs text-stone-500">
                      <p>关键词观察，仅供定位原文：</p>
                      {result.observations.map((item, index) => <p key={index} className="mt-1">{item.description}</p>)}
                    </div>}
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
        type="button"
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
