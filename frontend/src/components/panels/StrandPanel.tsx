'use client'

import React, { useState, useEffect, useCallback } from 'react'
import { apiFetch } from '@/lib/api'

// ----------------------------------------------------------------
// Types
// ----------------------------------------------------------------

interface StrandData {
  last_quest_chapter: number
  last_fire_chapter: number
  last_constellation_chapter: number
  current_dominant: string
  warnings: string[]
  history: Array<{ chapter: number; dominant: string }>
}

interface StrandPanelProps {
  projectId: string
}

// ----------------------------------------------------------------
// Constants
// ----------------------------------------------------------------

const STRAND_CONFIG = {
  quest: {
    label: '主线',
    sublabel: 'Quest',
    color: 'bg-amber-500',
    colorLight: 'bg-amber-100',
    colorText: 'text-amber-700',
    dotColor: 'bg-amber-400',
    warnThreshold: 5,
  },
  fire: {
    label: '感情线',
    sublabel: 'Fire',
    color: 'bg-rose-500',
    colorLight: 'bg-rose-100',
    colorText: 'text-rose-700',
    dotColor: 'bg-rose-400',
    warnThreshold: 10,
  },
  constellation: {
    label: '世界观',
    sublabel: 'Constellation',
    color: 'bg-indigo-500',
    colorLight: 'bg-indigo-100',
    colorText: 'text-indigo-700',
    dotColor: 'bg-indigo-400',
    warnThreshold: 15,
  },
} as const

type StrandKey = keyof typeof STRAND_CONFIG

// ----------------------------------------------------------------
// Strand bar component
// ----------------------------------------------------------------

function StrandBar({
  strand,
  chaptersSince,
  currentChapter,
}: {
  strand: StrandKey
  chaptersSince: number
  currentChapter: number
}) {
  const config = STRAND_CONFIG[strand]
  const maxDisplay = config.warnThreshold * 1.5
  const fillPercent = Math.min((chaptersSince / maxDisplay) * 100, 100)
  const thresholdPercent = (config.warnThreshold / maxDisplay) * 100
  const isOverThreshold = chaptersSince >= config.warnThreshold

  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-1.5">
          <span className={`inline-block w-2 h-2 rounded-full ${config.color}`} />
          <span className="text-xs font-medium text-stone-700">{config.label}</span>
          <span className="text-[10px] text-stone-400">{config.sublabel}</span>
        </div>
        <div className="flex items-center gap-1">
          <span
            className={`text-xs font-semibold ${
              isOverThreshold ? config.colorText : 'text-stone-600'
            }`}
          >
            {chaptersSince} 章
          </span>
          {isOverThreshold && (
            <svg
              className={`w-3.5 h-3.5 ${config.colorText} animate-pulse`}
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              strokeWidth={2}
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-2.5L13.732 4.5c-.77-.833-2.694-.833-3.464 0L3.34 16.5c-.77.833.192 2.5 1.732 2.5z"
              />
            </svg>
          )}
        </div>
      </div>

      {/* Progress bar with threshold marker */}
      <div className="relative h-2 rounded-full bg-stone-100 overflow-visible">
        {/* Fill bar */}
        <div
          className={`absolute inset-y-0 left-0 rounded-full transition-all duration-500 ${
            isOverThreshold ? config.color + ' animate-pulse' : config.color
          }`}
          style={{
            width: `${Math.min(fillPercent, 100)}%`,
            opacity: isOverThreshold ? 0.85 : 0.7,
          }}
        />

        {/* Threshold marker */}
        <div
          className="absolute top-0 bottom-0 w-0.5 bg-stone-400"
          style={{ left: `${thresholdPercent}%` }}
          title={`警戒线: ${config.warnThreshold} 章`}
        />

        {/* Threshold label */}
        <span
          className="absolute -top-3.5 text-[9px] text-stone-400 transform -translate-x-1/2"
          style={{ left: `${thresholdPercent}%` }}
        >
          {config.warnThreshold}
        </span>
      </div>

      {/* Last appearance */}
      <div className="text-[10px] text-stone-400">
        上次出现: 第 {currentChapter - chaptersSince} 章
      </div>
    </div>
  )
}

// ----------------------------------------------------------------
// Timeline strip (last 20 chapters)
// ----------------------------------------------------------------

function TimelineStrip({ history }: { history: Array<{ chapter: number; dominant: string }> }) {
  const last20 = history.slice(-20)

  if (last20.length === 0) return null

  return (
    <div className="space-y-1.5">
      <div className="text-[10px] text-stone-500 font-medium tracking-wide">
        近期章节走势
      </div>
      <div className="flex items-center gap-0.5">
        {last20.map((entry) => {
          const strandKey = entry.dominant as StrandKey
          const config = STRAND_CONFIG[strandKey]
          const dotColor = config ? config.dotColor : 'bg-stone-300'

          return (
            <div
              key={entry.chapter}
              className="group relative flex-1 flex flex-col items-center"
            >
              <div className={`w-2.5 h-2.5 rounded-full ${dotColor} transition-transform group-hover:scale-150`} />
              {/* Tooltip on hover */}
              <div className="absolute -top-6 opacity-0 group-hover:opacity-100 transition-opacity bg-stone-800 text-white text-[9px] px-1.5 py-0.5 rounded whitespace-nowrap z-10 pointer-events-none">
                Ch.{entry.chapter}
              </div>
            </div>
          )
        })}
      </div>
      <div className="flex justify-between text-[9px] text-stone-400">
        <span>Ch.{last20[0].chapter}</span>
        <span>Ch.{last20[last20.length - 1].chapter}</span>
      </div>
    </div>
  )
}

// ----------------------------------------------------------------
// Component
// ----------------------------------------------------------------

export function StrandPanel({ projectId }: StrandPanelProps) {
  const [data, setData] = useState<StrandData | null>(null)
  const [loading, setLoading] = useState(false)

  const fetchStrandData = useCallback(async () => {
    if (!projectId) return
    setLoading(true)
    try {
      const result = await apiFetch<StrandData>(
        `/api/projects/${projectId}/strand-status`
      )
      setData(result)
    } catch {
      setData(null)
    } finally {
      setLoading(false)
    }
  }, [projectId])

  useEffect(() => {
    fetchStrandData()
  }, [fetchStrandData])

  if (loading) {
    return <p className="text-xs text-stone-400">加载三线数据...</p>
  }

  if (!data) {
    return (
      <div className="space-y-2">
        <p className="text-xs text-stone-400">暂无三线编织数据。开始写作后将自动分析。</p>
      </div>
    )
  }

  // Calculate current chapter from history
  const currentChapter =
    data.history.length > 0
      ? data.history[data.history.length - 1].chapter
      : Math.max(data.last_quest_chapter, data.last_fire_chapter, data.last_constellation_chapter)

  const questSince = currentChapter - data.last_quest_chapter
  const fireSince = currentChapter - data.last_fire_chapter
  const constellationSince = currentChapter - data.last_constellation_chapter

  return (
    <div className="space-y-4">
      {/* Current dominant strand */}
      <div className="flex items-center gap-2">
        <span className="text-[10px] text-stone-500 uppercase tracking-wider">当前主导</span>
        <span
          className={`text-xs font-medium px-2 py-0.5 rounded-full ${
            STRAND_CONFIG[data.current_dominant as StrandKey]?.colorLight || 'bg-stone-100'
          } ${STRAND_CONFIG[data.current_dominant as StrandKey]?.colorText || 'text-stone-600'}`}
        >
          {STRAND_CONFIG[data.current_dominant as StrandKey]?.label || data.current_dominant}
        </span>
      </div>

      {/* Three strand bars */}
      <div className="space-y-4 pt-1">
        <StrandBar strand="quest" chaptersSince={questSince} currentChapter={currentChapter} />
        <StrandBar strand="fire" chaptersSince={fireSince} currentChapter={currentChapter} />
        <StrandBar
          strand="constellation"
          chaptersSince={constellationSince}
          currentChapter={currentChapter}
        />
      </div>

      {/* Warnings */}
      {data.warnings.length > 0 && (
        <div className="space-y-1">
          {data.warnings.map((w, i) => (
            <div
              key={i}
              className="flex items-start gap-1.5 text-[11px] text-amber-700 bg-amber-50/60 rounded-lg px-2.5 py-1.5"
            >
              <svg
                className="w-3 h-3 mt-0.5 flex-shrink-0"
                fill="none"
                viewBox="0 0 24 24"
                stroke="currentColor"
                strokeWidth={2}
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  d="M12 9v2m0 4h.01"
                />
              </svg>
              <span>{w}</span>
            </div>
          ))}
        </div>
      )}

      {/* Timeline strip */}
      <TimelineStrip history={data.history} />
    </div>
  )
}
