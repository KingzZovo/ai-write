'use client'

import React, { useState, useEffect, useCallback } from 'react'
import { apiFetch } from '@/lib/api'

interface EvaluationIssue {
  location: string
  description: string
  suggestion: string
  severity: 'low' | 'medium' | 'high'
}

interface EvaluationResult {
  overallScore: number
  scores: {
    plotCoherence: number
    characterConsistency: number
    styleAdherence: number
    narrativePacing: number
    foreshadowHandling: number
  }
  issues: EvaluationIssue[]
  evaluatedAt: string
}

interface EvaluationPanelProps {
  chapterId: string
}

const SCORE_LABELS: { key: keyof EvaluationResult['scores']; label: string }[] = [
  { key: 'plotCoherence', label: '情节连贯性' },
  { key: 'characterConsistency', label: '角色一致性' },
  { key: 'styleAdherence', label: '风格契合度' },
  { key: 'narrativePacing', label: '叙事节奏' },
  { key: 'foreshadowHandling', label: '伏笔处理' },
]

function getScoreColor(score: number): string {
  if (score < 6) return 'bg-red-500'
  if (score <= 8) return 'bg-yellow-500'
  return 'bg-green-500'
}

function getScoreTextColor(score: number): string {
  if (score < 6) return 'text-red-600'
  if (score <= 8) return 'text-yellow-600'
  return 'text-green-600'
}

function getOverallBg(score: number): string {
  if (score < 6) return 'bg-red-50 border-red-200'
  if (score <= 8) return 'bg-yellow-50 border-yellow-200'
  return 'bg-green-50 border-green-200'
}

const SEVERITY_CONFIG = {
  high: { color: 'bg-red-100 text-red-700', label: '高' },
  medium: { color: 'bg-yellow-100 text-yellow-700', label: '中' },
  low: { color: 'bg-gray-100 text-gray-600', label: '低' },
}

export function EvaluationPanel({ chapterId }: EvaluationPanelProps) {
  const [evaluation, setEvaluation] = useState<EvaluationResult | null>(null)
  const [loading, setLoading] = useState(false)
  const [evaluating, setEvaluating] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const fetchEvaluation = useCallback(async () => {
    if (!chapterId) return
    setLoading(true)
    setError(null)
    try {
      const data = await apiFetch<EvaluationResult>(
        `/api/chapters/${chapterId}/evaluate`
      )
      setEvaluation(data)
    } catch {
      setEvaluation(null)
    } finally {
      setLoading(false)
    }
  }, [chapterId])

  useEffect(() => {
    fetchEvaluation()
  }, [fetchEvaluation])

  const handleRunEvaluation = async () => {
    if (evaluating) return
    setEvaluating(true)
    setError(null)
    try {
      const data = await apiFetch<EvaluationResult>(
        `/api/chapters/${chapterId}/evaluate`,
        { method: 'POST' }
      )
      setEvaluation(data)
    } catch (err) {
      setError(err instanceof Error ? err.message : '评估失败')
    } finally {
      setEvaluating(false)
    }
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold text-gray-900">质量评估</h3>
      </div>

      {loading ? (
        <p className="text-xs text-gray-400">加载评估中...</p>
      ) : evaluation ? (
        <div className="space-y-3">
          {/* Overall score */}
          <div
            className={`rounded-lg border p-3 text-center ${getOverallBg(
              evaluation.overallScore
            )}`}
          >
            <div className={`text-2xl font-bold ${getScoreTextColor(evaluation.overallScore)}`}>
              {evaluation.overallScore.toFixed(1)}
            </div>
            <div className="text-xs text-gray-500 mt-0.5">综合评分 / 10</div>
          </div>

          {/* Individual scores as progress bars */}
          <div className="space-y-2">
            {SCORE_LABELS.map(({ key, label }) => {
              const score = evaluation.scores[key]
              return (
                <div key={key}>
                  <div className="flex items-center justify-between mb-0.5">
                    <span className="text-xs text-gray-600">{label}</span>
                    <span className={`text-xs font-medium ${getScoreTextColor(score)}`}>
                      {score.toFixed(1)}
                    </span>
                  </div>
                  <div className="w-full h-1.5 bg-gray-100 rounded-full overflow-hidden">
                    <div
                      className={`h-full rounded-full transition-all ${getScoreColor(score)}`}
                      style={{ width: `${Math.min(score * 10, 100)}%` }}
                    />
                  </div>
                </div>
              )
            })}
          </div>

          {/* Issues list */}
          {evaluation.issues.length > 0 && (
            <div>
              <h4 className="text-xs font-medium text-gray-600 mb-1.5">
                问题 ({evaluation.issues.length})
              </h4>
              <div className="space-y-1.5">
                {evaluation.issues.map((issue, idx) => {
                  const severityCfg = SEVERITY_CONFIG[issue.severity]
                  return (
                    <div
                      key={idx}
                      className="bg-white border border-gray-200 rounded-lg p-2 text-xs"
                    >
                      <div className="flex items-center gap-1.5 mb-1">
                        <span
                          className={`px-1 py-0.5 rounded text-[10px] font-medium ${severityCfg.color}`}
                        >
                          {severityCfg.label}
                        </span>
                        <span className="text-gray-400 truncate">{issue.location}</span>
                      </div>
                      <p className="text-gray-700 leading-relaxed">{issue.description}</p>
                      {issue.suggestion && (
                        <p className="text-blue-600 mt-1 leading-relaxed">
                          建议: {issue.suggestion}
                        </p>
                      )}
                    </div>
                  )
                })}
              </div>
            </div>
          )}

          <div className="text-[10px] text-gray-400">
            评估时间: {evaluation.evaluatedAt ? new Date(evaluation.evaluatedAt).toLocaleString() : ""}
          </div>
        </div>
      ) : (
        <p className="text-xs text-gray-400">暂无评估数据，运行评估后可查看结果。</p>
      )}

      {error && <p className="text-xs text-red-500">{error}</p>}

      {/* Action buttons */}
      <div className="space-y-1.5">
        <button
          onClick={handleRunEvaluation}
          disabled={evaluating}
          className="w-full px-3 py-1.5 text-xs bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {evaluating ? '评估中...' : '运行评估'}
        </button>
        <button
          disabled
          className="w-full px-3 py-1.5 text-xs bg-gray-100 text-gray-400 rounded-lg cursor-not-allowed"
        >
          重新生成问题段落
        </button>
      </div>
    </div>
  )
}
