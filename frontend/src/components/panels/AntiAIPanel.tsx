'use client'

import React, { useState, useCallback } from 'react'
import { apiFetch } from '@/lib/api'

// ----------------------------------------------------------------
// Types
// ----------------------------------------------------------------

interface AIWord {
  word: string
  count: number
}

interface AntiAIResult {
  score: number
  ai_words: AIWord[]
  de_density: number
  de_threshold: number
  idiom_density: number
  idiom_threshold: number
  sentence_variety: number
}

interface AntiAIPanelProps {
  chapterId: string
}

// ----------------------------------------------------------------
// Density meter component
// ----------------------------------------------------------------

function DensityMeter({
  label,
  value,
  threshold,
  unit,
  invert,
}: {
  label: string
  value: number
  threshold: number
  unit: string
  invert?: boolean
}) {
  // invert=true means "above threshold is bad" (like de density)
  const isOver = invert ? value > threshold : value < threshold
  const maxDisplay = threshold * 2
  const fillPercent = Math.min((value / maxDisplay) * 100, 100)
  const thresholdPercent = (threshold / maxDisplay) * 100

  const barColor = isOver ? 'bg-red-400' : 'bg-emerald-400'
  const textColor = isOver ? 'text-red-600' : 'text-emerald-600'

  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between">
        <span className="text-xs text-stone-600">{label}</span>
        <span className={`text-xs font-semibold ${textColor}`}>
          {value.toFixed(1)}{unit}
        </span>
      </div>
      <div className="relative h-2 rounded-full bg-stone-100">
        <div
          className={`absolute inset-y-0 left-0 rounded-full transition-all duration-500 ${barColor}`}
          style={{ width: `${fillPercent}%`, opacity: 0.75 }}
        />
        {/* Threshold marker */}
        <div
          className="absolute top-0 bottom-0 w-0.5 bg-stone-400"
          style={{ left: `${thresholdPercent}%` }}
        />
        <span
          className="absolute -top-3.5 text-[9px] text-stone-400 transform -translate-x-1/2"
          style={{ left: `${thresholdPercent}%` }}
        >
          {threshold}{unit}
        </span>
      </div>
    </div>
  )
}

// ----------------------------------------------------------------
// Sentence variety indicator
// ----------------------------------------------------------------

function VarietyIndicator({ value }: { value: number }) {
  // value 0-10, higher = more variety = better
  const segments = 10
  const filledSegments = Math.round(value)
  const color =
    value >= 7 ? 'bg-emerald-400' : value >= 4 ? 'bg-amber-400' : 'bg-red-400'
  const textColor =
    value >= 7 ? 'text-emerald-600' : value >= 4 ? 'text-amber-600' : 'text-red-600'

  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between">
        <span className="text-xs text-stone-600">句式多样性</span>
        <span className={`text-xs font-semibold ${textColor}`}>
          {value.toFixed(1)} / 10
        </span>
      </div>
      <div className="flex gap-0.5">
        {Array.from({ length: segments }).map((_, i) => (
          <div
            key={i}
            className={`h-2 flex-1 rounded-sm transition-colors ${
              i < filledSegments ? color : 'bg-stone-100'
            }`}
          />
        ))}
      </div>
    </div>
  )
}

// ----------------------------------------------------------------
// Component
// ----------------------------------------------------------------

export function AntiAIPanel({ chapterId }: AntiAIPanelProps) {
  const [result, setResult] = useState<AntiAIResult | null>(null)
  const [loading, setLoading] = useState(false)
  const [removing, setRemoving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [dismissedWords, setDismissedWords] = useState<Set<string>>(new Set())

  const handleCheck = useCallback(async () => {
    if (loading) return
    setLoading(true)
    setError(null)
    setDismissedWords(new Set())
    try {
      const data = await apiFetch<AntiAIResult>(
        `/api/chapters/${chapterId}/anti-ai-check`,
        { method: 'POST' }
      )
      setResult(data)
    } catch (err) {
      setError(err instanceof Error ? err.message : '检查失败')
    } finally {
      setLoading(false)
    }
  }, [chapterId, loading])

  const handleOneClickRemove = useCallback(async () => {
    if (removing) return
    setRemoving(true)
    try {
      await apiFetch(`/api/chapters/${chapterId}/remove-ai-traces`, {
        method: 'POST',
      })
    } catch {
      // placeholder — will be connected to StyleAgent later
    } finally {
      setRemoving(false)
    }
  }, [chapterId, removing])

  const dismissWord = (word: string) => {
    setDismissedWords((prev) => {
      const next = new Set(prev)
      next.add(word)
      return next
    })
  }

  const scoreColor =
    result && result.score >= 8
      ? 'text-emerald-700'
      : result && result.score >= 5
        ? 'text-amber-700'
        : 'text-red-700'

  const scoreBg =
    result && result.score >= 8
      ? 'bg-emerald-50/60 border-emerald-200/60'
      : result && result.score >= 5
        ? 'bg-amber-50/60 border-amber-200/60'
        : result
          ? 'bg-red-50/60 border-red-200/60'
          : 'bg-stone-50 border-stone-200'

  return (
    <div className="space-y-3">
      {/* Overall AI trace score */}
      {result && (
        <div className={`rounded-xl border p-4 text-center ${scoreBg}`}>
          <div
            className={`text-3xl font-serif font-bold tracking-tight ${scoreColor}`}
            style={{ fontFamily: "'Noto Serif SC', 'Georgia', serif" }}
          >
            {result.score.toFixed(1)}
          </div>
          <div className="text-[11px] text-stone-500 mt-0.5 tracking-wide">
            人味指数 (越高越自然)
          </div>
        </div>
      )}

      {/* AI words as removable chips */}
      {result && result.ai_words.length > 0 && (
        <div>
          <div className="text-xs font-medium text-stone-600 mb-1.5">
            检测到的AI高频词
          </div>
          <div className="flex flex-wrap gap-1.5">
            {result.ai_words
              .filter((w) => !dismissedWords.has(w.word))
              .map((w) => (
                <span
                  key={w.word}
                  className="inline-flex items-center gap-1 px-2 py-1 text-[11px] bg-red-50 text-red-700 border border-red-200 rounded-full group"
                >
                  <span className="font-medium">{w.word}</span>
                  <span className="text-red-400 text-[9px]">x{w.count}</span>
                  <button
                    onClick={() => dismissWord(w.word)}
                    className="ml-0.5 w-3.5 h-3.5 rounded-full bg-red-200/60 text-red-500 text-[9px] flex items-center justify-center hover:bg-red-300 transition-colors opacity-0 group-hover:opacity-100"
                    type="button"
                  >
                    x
                  </button>
                </span>
              ))}
          </div>
          {dismissedWords.size > 0 && (
            <button
              onClick={() => setDismissedWords(new Set())}
              className="text-[10px] text-stone-400 mt-1 hover:text-stone-600"
              type="button"
            >
              恢复已忽略 ({dismissedWords.size})
            </button>
          )}
        </div>
      )}

      {/* Density meters */}
      {result && (
        <div className="space-y-4 pt-1">
          <DensityMeter
            label={'"的"字密度'}
            value={result.de_density}
            threshold={result.de_threshold}
            unit="%"
            invert
          />
          <DensityMeter
            label="四字成语密度"
            value={result.idiom_density}
            threshold={result.idiom_threshold}
            unit="%"
            invert
          />
          <VarietyIndicator value={result.sentence_variety} />
        </div>
      )}

      {/* Before/after preview area */}
      {result && (
        <div className="border border-dashed border-stone-200 rounded-lg p-3 text-center">
          <span className="text-[10px] text-stone-400">
            修改前后对比区域 (去AI味后显示)
          </span>
        </div>
      )}

      {error && <p className="text-xs text-red-500">{error}</p>}

      {/* Action buttons */}
      <div className="space-y-1.5">
        <button
          onClick={handleCheck}
          disabled={loading}
          className="w-full px-3 py-2 text-xs font-medium rounded-lg transition-colors
            bg-stone-800 text-white hover:bg-stone-900
            disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {loading ? '检测中...' : '运行AI痕迹检测'}
        </button>

        {result && (
          <button
            onClick={handleOneClickRemove}
            disabled={removing}
            className="w-full px-3 py-2 text-xs font-medium rounded-lg transition-colors
              bg-amber-600 text-white hover:bg-amber-700
              disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {removing ? '处理中...' : '一键去AI味'}
          </button>
        )}
      </div>

      {!result && !loading && (
        <p className="text-xs text-stone-400 text-center">
          检测章节中的AI写作痕迹
        </p>
      )}
    </div>
  )
}
