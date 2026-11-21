'use client'

import React, { useState, useEffect } from 'react'
import { ClipboardList } from 'lucide-react'
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

interface TaskSnapshot {
  task_id: string
  chapter_id: string
  status: 'pending' | 'running' | 'completed' | 'failed'
  result?: Record<string, unknown> | null
  completed_at?: string | null
  source_status?: 'unchanged' | 'changed' | 'unknown'
}

const SCORE_FIELDS = ['plot_coherence', 'character_consistency', 'style_adherence', 'narrative_pacing', 'foreshadow_handling'] as const

function readResult(task: TaskSnapshot | null): EvaluationResult | null {
  const r = task?.result
  if (!r || r.parse_failed || r.failure_reason || !Array.isArray(r.issues)) return null
  const values = [r.overall, ...SCORE_FIELDS.map(k => r[k])]
  if (!values.every(v => typeof v === 'number' && Number.isFinite(v) && v >= 0 && v <= 10)) return null
  const issues = Array.isArray(r.issues) ? r.issues.filter((v): v is Record<string, unknown> => !!v && typeof v === 'object') : []
  return {
    overallScore: r.overall as number,
    scores: { plotCoherence: r.plot_coherence as number, characterConsistency: r.character_consistency as number,
      styleAdherence: r.style_adherence as number, narrativePacing: r.narrative_pacing as number, foreshadowHandling: r.foreshadow_handling as number },
    issues: issues.map(i => ({ location: typeof i.location === 'number' ? `第 ${i.location} 段` : typeof i.location === 'string' ? i.location : '',
      description: typeof i.description === 'string' ? i.description : '', suggestion: typeof i.suggestion === 'string' ? i.suggestion : '',
      severity: i.severity === 'high' || i.severity === 'critical' ? 'high' : i.severity === 'low' ? 'low' : 'medium' })),
    evaluatedAt: task?.completed_at || '',
  }
}

function failureLabel(task: TaskSnapshot | null): string | null {
  if (task?.status === 'failed') return '评审任务失败，正文未改动。可以重试。'
  if (task?.status !== 'completed' || readResult(task)) return null
  switch (task.result?.failure_reason) {
    case 'call_failed': return '评审模型调用失败，未获得有效评分。'
    case 'incomplete_response': return '评审响应未完整结束，未获得有效评分。请保留正文后重试评审。'
    case 'timeout': return '评审超时，未获得有效评分。'
    case 'empty_input': return '已保存正文为空，无法评审。'
    default: return '评审结果不可用，请重新评审。'
  }
}

export function EvaluationPanel({ chapterId }: EvaluationPanelProps) {
  // A chapter switch must also discard state and cancel in-flight polling.
  return <ChapterEvaluationPanel key={chapterId} chapterId={chapterId} />
}

function ChapterEvaluationPanel({ chapterId }: EvaluationPanelProps) {
  const [task, setTask] = useState<TaskSnapshot | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [command, setCommand] = useState({ mode: 'load', sequence: 0 })
  const evaluation = readResult(task)
  const failure = failureLabel(task)
  const evaluating = busy || task?.status === 'pending' || task?.status === 'running'

  useEffect(() => {
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout> | undefined
    const request = { signal: controller.signal }
    setError(null)
    setBusy(true)
    const receive = (snapshot: TaskSnapshot | null) => {
      if (controller.signal.aborted) return
      if (snapshot && snapshot.chapter_id !== chapterId) throw new Error('chapter mismatch')
      setTask(snapshot)
      setLoading(false)
      if (snapshot?.status === 'pending' || snapshot?.status === 'running') {
        timer = setTimeout(() => { void poll(snapshot.task_id) }, 2000)
      } else setBusy(false)
    }
    const failed = () => {
      if (controller.signal.aborted) return
      setLoading(false)
      setBusy(false)
      setError('无法获取评审结果，请刷新。运行评审前请先保存非空正文。')
    }
    const poll = async (id: string) => {
      try {
        const snapshot = await apiFetch<TaskSnapshot>(`/api/evaluate/tasks/${encodeURIComponent(id)}`, request)
        if (snapshot.task_id !== id) throw new Error('task mismatch')
        receive(snapshot)
      }
      catch { failed() }
    }
    const begin = async () => {
      try {
        if (command.mode === 'start') {
          const created = await apiFetch<TaskSnapshot>('/api/evaluate/start', { ...request, method: 'POST',
            body: JSON.stringify({ chapter_id: chapterId, caller: 'frontend_quality_panel' }) })
          receive(created)
        } else receive(await apiFetch<TaskSnapshot | null>(`/api/evaluate/chapters/${encodeURIComponent(chapterId)}/latest`, request))
      } catch { failed() }
    }
    void begin()
    return () => { controller.abort(); if (timer) clearTimeout(timer) }
  }, [chapterId, command])

  const handleRunEvaluation = () => {
    if (evaluating || loading || error) return
    setBusy(true)
    setCommand(c => ({ mode: 'start', sequence: c.sequence + 1 }))
  }

  return (
    <div className="space-y-3">
      <p className="text-xs text-gray-500">评审针对已保存正文；下方为历史反馈，改稿后请重新评审。模型评分仅供参考。</p>
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold text-gray-900">质量评估</h3>
      </div>

      {loading ? (
        <p className="text-xs text-gray-400">加载评估中...</p>
      ) : failure ? (
        <p role="alert" className="text-xs text-amber-700">{failure}</p>
      ) : evaluation ? (
        <div className="space-y-3">
          <p role="status" className="text-xs text-amber-700">
            {task?.source_status === 'changed'
              ? '正文或细纲已变化，以下是旧稿评审，请重新评估。'
              : task?.source_status === 'unchanged'
                ? '本次读取核对：正文与细纲和评审时一致。其他设定仍需核查。'
                : '此评审未记录稿件来源，以下仅作历史参考。'}
          </p>
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
                      className="bg-white border border-gray-200 rounded-lg p-2 text-xs transition-colors duration-150 hover:border-gray-300"
                    >
                      <div className="flex items-center gap-1.5 mb-1">
                        <span
                          className={`px-1.5 py-0.5 rounded-full text-[10px] font-medium ${severityCfg.color}`}
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
        <div className="flex flex-col items-center gap-1 py-4 text-center">
          <ClipboardList className="h-5 w-5 text-gray-300" aria-hidden />
          <p className="text-xs text-gray-400">暂无评估数据，运行评估后可查看结果。</p>
        </div>
      )}

      {error && <p className="text-xs text-red-500">{error}</p>}

      {/* Action buttons */}
      <div className="space-y-1.5">
        <button
          onClick={handleRunEvaluation}
          disabled={evaluating || loading || !!error}
          className="w-full px-3 py-1.5 text-xs font-medium bg-blue-600 text-white rounded-lg shadow-sm transition-all duration-150 hover:bg-blue-700 motion-safe:active:scale-[0.98] disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {evaluating ? '评估中...' : '运行评估'}
        </button>
        <button
          onClick={() => setCommand(c => ({ mode: 'load', sequence: c.sequence + 1 }))}
          className="w-full px-3 py-1.5 text-xs bg-gray-100 text-gray-700 rounded-lg"
        >刷新评审状态</button>
      </div>
    </div>
  )
}
