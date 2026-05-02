'use client'

import React, { useState, useEffect, useCallback } from 'react'
import { apiFetch } from '@/lib/api'

interface Tracker {
  last_quest_chapter: number
  last_fire_chapter: number
  last_constellation_chapter: number
  current_dominant: string
  history: Array<{ chapter: number; dominant: string }>
}

interface StrandResponse {
  tracker: Tracker
  warnings?: string[]
  history?: Array<{ chapter: number; dominant: string }>
}

interface StrandPanelProps { projectId: string }

const STRAND_CONFIG = {
  quest:         { label: '主线',   sublabel: 'Quest',         color: 'bg-amber-500',  colorLight: 'bg-amber-100',  colorText: 'text-amber-700',  dotColor: 'bg-amber-400',  warnThreshold: 5  },
  fire:          { label: '感情线', sublabel: 'Fire',          color: 'bg-rose-500',   colorLight: 'bg-rose-100',   colorText: 'text-rose-700',   dotColor: 'bg-rose-400',   warnThreshold: 10 },
  constellation: { label: '世界观', sublabel: 'Constellation', color: 'bg-indigo-500', colorLight: 'bg-indigo-100', colorText: 'text-indigo-700', dotColor: 'bg-indigo-400', warnThreshold: 15 },
} as const
type StrandKey = keyof typeof STRAND_CONFIG

function StrandBar({ strand, chaptersSince, currentChapter }: { strand: StrandKey; chaptersSince: number; currentChapter: number }) {
  const config = STRAND_CONFIG[strand]
  const safeSince = Number.isFinite(chaptersSince) ? Math.max(0, chaptersSince) : 0
  const maxDisplay = config.warnThreshold * 1.5
  const fillPercent = Math.min((safeSince / maxDisplay) * 100, 100)
  const thresholdPercent = (config.warnThreshold / maxDisplay) * 100
  const isOverThreshold = safeSince >= config.warnThreshold
  const lastCh = Math.max(0, currentChapter - safeSince)
  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-1.5">
          <span className={`inline-block w-2 h-2 rounded-full ${config.color}`} />
          <span className="text-xs font-medium text-stone-700">{config.label}</span>
          <span className="text-[10px] text-stone-400">{config.sublabel}</span>
        </div>
        <div className="flex items-center gap-1">
          <span className={`text-xs font-semibold ${isOverThreshold ? config.colorText : 'text-stone-600'}`}>{safeSince} 章</span>
          {isOverThreshold && <span className={`text-[10px] ${config.colorText}`}>⚠</span>}
        </div>
      </div>
      <div className="relative h-2 rounded-full bg-stone-100 overflow-visible">
        <div className={`absolute inset-y-0 left-0 rounded-full transition-all duration-500 ${config.color}`} style={{ width: `${fillPercent}%`, opacity: isOverThreshold ? 0.85 : 0.7 }} />
        <div className="absolute top-0 bottom-0 w-0.5 bg-stone-400" style={{ left: `${thresholdPercent}%` }} title={`警戒线: ${config.warnThreshold} 章`} />
        <span className="absolute -top-3.5 text-[9px] text-stone-400 transform -translate-x-1/2" style={{ left: `${thresholdPercent}%` }}>{config.warnThreshold}</span>
      </div>
      <div className="text-[10px] text-stone-400">上次出现: 第 {lastCh} 章</div>
    </div>
  )
}

function TimelineStrip({ history }: { history: Array<{ chapter: number; dominant: string }> }) {
  const last20 = history.slice(-20)
  if (last20.length === 0) return null
  return (
    <div className="space-y-1.5">
      <div className="text-[10px] text-stone-500 font-medium tracking-wide">近期章节走势</div>
      <div className="flex items-center gap-0.5">
        {last20.map((entry) => {
          const cfg = STRAND_CONFIG[entry.dominant as StrandKey]
          const dotColor = cfg ? cfg.dotColor : 'bg-stone-300'
          return (
            <div key={entry.chapter} className="group relative flex-1 flex flex-col items-center">
              <div className={`w-2.5 h-2.5 rounded-full ${dotColor} transition-transform group-hover:scale-150`} />
              <div className="absolute -top-6 opacity-0 group-hover:opacity-100 transition-opacity bg-stone-800 text-white text-[9px] px-1.5 py-0.5 rounded whitespace-nowrap z-10 pointer-events-none">Ch.{entry.chapter}</div>
            </div>
          )
        })}
      </div>
      <div className="flex justify-between text-[9px] text-stone-400"><span>Ch.{last20[0].chapter}</span><span>Ch.{last20[last20.length - 1].chapter}</span></div>
    </div>
  )
}

export function StrandPanel({ projectId }: StrandPanelProps) {
  const [data, setData] = useState<StrandResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const fetchStrandData = useCallback(async () => {
    if (!projectId) return
    setLoading(true)
    try {
      const result = await apiFetch<StrandResponse>(`/api/projects/${projectId}/strand-status`)
      setData(result)
    } catch { setData(null) } finally { setLoading(false) }
  }, [projectId])
  useEffect(() => { fetchStrandData() }, [fetchStrandData])

  if (loading) return <p className="text-xs text-stone-400">加载三线数据...</p>
  if (!data || !data.tracker) return <p className="text-xs text-stone-400">暂无三线编织数据。开始写作后将自动分析。</p>
  const t = data.tracker
  const history = data.history && data.history.length ? data.history : (t.history || [])
  const currentChapter = history.length > 0
    ? history[history.length - 1].chapter
    : Math.max(t.last_quest_chapter || 0, t.last_fire_chapter || 0, t.last_constellation_chapter || 0)
  const questSince = currentChapter - (t.last_quest_chapter || 0)
  const fireSince = currentChapter - (t.last_fire_chapter || 0)
  const constellationSince = currentChapter - (t.last_constellation_chapter || 0)
  const dom = t.current_dominant as StrandKey
  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2">
        <span className="text-[10px] text-stone-500 uppercase tracking-wider">当前主导</span>
        <span className={`text-xs font-medium px-2 py-0.5 rounded-full ${STRAND_CONFIG[dom]?.colorLight || 'bg-stone-100'} ${STRAND_CONFIG[dom]?.colorText || 'text-stone-600'}`}>{STRAND_CONFIG[dom]?.label || t.current_dominant || '—'}</span>
        <span className="ml-auto text-[10px] text-stone-400">第 {currentChapter} 章</span>
      </div>
      <div className="space-y-4 pt-1">
        <StrandBar strand="quest" chaptersSince={questSince} currentChapter={currentChapter} />
        <StrandBar strand="fire" chaptersSince={fireSince} currentChapter={currentChapter} />
        <StrandBar strand="constellation" chaptersSince={constellationSince} currentChapter={currentChapter} />
      </div>
      {data.warnings && data.warnings.length > 0 && (
        <div className="space-y-1">
          {data.warnings.map((w, i) => (
            <div key={i} className="flex items-start gap-1.5 text-[11px] text-amber-700 bg-amber-50/60 rounded-lg px-2.5 py-1.5">
              <span className="text-amber-600 flex-shrink-0">⚠</span><span>{w}</span>
            </div>
          ))}
        </div>
      )}
      <TimelineStrip history={history} />
    </div>
  )
}
