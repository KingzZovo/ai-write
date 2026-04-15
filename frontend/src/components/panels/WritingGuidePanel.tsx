'use client'

import React, { useState } from 'react'

// ----------------------------------------------------------------
// Types & Constants
// ----------------------------------------------------------------

interface WritingModule {
  key: string
  label: string
  description: string
  tooltip: string
}

const WRITING_MODULES: WritingModule[] = [
  {
    key: 'show_not_tell',
    label: '展示而非讲述',
    description: '用场景和动作代替直白叙述',
    tooltip: '通过具体场景、对话和行为展现角色性格与情感，避免直接告知读者角色的想法和感受。',
  },
  {
    key: 'scene_immersion',
    label: '场景沉浸感',
    description: '五感描写与细节营造临场感',
    tooltip: '运用视觉、听觉、嗅觉、触觉和味觉描写，让读者身临其境地感受场景。',
  },
  {
    key: 'dialogue_craft',
    label: '对话技巧',
    description: '个性化对话与潜台词运用',
    tooltip: '每个角色说话方式应独特，对话包含潜台词和情感层次，避免说教式信息灌输。',
  },
  {
    key: 'tension_control',
    label: '张力控制',
    description: '情节节奏的松紧交替',
    tooltip: '控制章节内的紧张感起伏，在高潮和舒缓间合理切换，保持读者兴趣。',
  },
  {
    key: 'micro_tension',
    label: '微观张力',
    description: '每段落保持阅读牵引力',
    tooltip: '在段落和句子层面制造小悬念、暗示和未解答的问题，驱动读者继续阅读。',
  },
  {
    key: 'emotional_resonance',
    label: '情感共鸣',
    description: '引发读者情感投入与共情',
    tooltip: '通过角色困境、抉择和成长激发读者情感反应，建立读者与角色的情感连接。',
  },
  {
    key: 'info_weaving',
    label: '信息编织',
    description: '设定信息自然融入叙事',
    tooltip: '将世界观、背景设定等信息有机地编织进故事和对话中，避免信息堆砌。',
  },
]

const PROHIBITIONS = [
  '禁止使用"不禁"、"竟然"等AI高频词汇',
  '禁止"他的眼中闪过一丝..."句式滥用',
  '禁止连续使用三个以上四字成语',
  '禁止在对话中直接解释世界观设定',
  '禁止重复使用相同的情感描写句式',
  '禁止"仿佛...一般"比喻过度使用',
  '禁止角色突然获得超出设定的能力',
  '禁止无铺垫的情节转折',
  '禁止用旁白解释角色心理变化',
  '禁止忽略已建立的伏笔线索',
  '禁止"的"字使用密度超过8%',
  '禁止段落内句式结构完全相同',
]

const GENRES = [
  { value: '', label: '选择类型...' },
  { value: 'xuanhuan', label: '玄幻' },
  { value: 'xianxia', label: '仙侠' },
  { value: 'dushi', label: '都市' },
  { value: 'yanqing', label: '言情' },
  { value: 'xuanyi', label: '悬疑' },
  { value: 'kehuan', label: '科幻' },
  { value: 'lishi', label: '历史' },
]

// ----------------------------------------------------------------
// Tooltip component
// ----------------------------------------------------------------

function Tooltip({ text }: { text: string }) {
  const [show, setShow] = useState(false)

  return (
    <span className="relative inline-flex">
      <button
        onMouseEnter={() => setShow(true)}
        onMouseLeave={() => setShow(false)}
        onClick={(e) => {
          e.stopPropagation()
          setShow(!show)
        }}
        className="w-4 h-4 rounded-full bg-stone-200 text-stone-500 text-[9px] font-bold flex items-center justify-center hover:bg-stone-300 transition-colors"
        type="button"
      >
        ?
      </button>
      {show && (
        <div className="absolute bottom-full left-1/2 -translate-x-1/2 mb-1.5 w-48 bg-stone-800 text-white text-[10px] leading-relaxed p-2 rounded-lg shadow-lg z-20 pointer-events-none">
          {text}
          <div className="absolute top-full left-1/2 -translate-x-1/2 border-4 border-transparent border-t-stone-800" />
        </div>
      )}
    </span>
  )
}

// ----------------------------------------------------------------
// Component
// ----------------------------------------------------------------

export function WritingGuidePanel() {
  const [activeModules, setActiveModules] = useState<Set<string>>(
    new Set(['show_not_tell', 'micro_tension', 'info_weaving'])
  )
  const [showProhibitions, setShowProhibitions] = useState(false)
  const [selectedGenre, setSelectedGenre] = useState('')

  const toggleModule = (key: string) => {
    setActiveModules((prev) => {
      const next = new Set(prev)
      if (next.has(key)) {
        next.delete(key)
      } else {
        next.add(key)
      }
      return next
    })
  }

  return (
    <div className="space-y-3">
      {/* Active count & genre selector */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="text-xs text-stone-500">当前激活</span>
          <span className="text-xs font-semibold text-stone-800 bg-stone-100 px-2 py-0.5 rounded-full">
            {activeModules.size} / {WRITING_MODULES.length}
          </span>
        </div>
      </div>

      {/* Genre selector */}
      <select
        value={selectedGenre}
        onChange={(e) => setSelectedGenre(e.target.value)}
        className="w-full px-2.5 py-1.5 text-xs border border-stone-200 rounded-lg bg-white text-stone-700 focus:ring-1 focus:ring-stone-300 focus:border-stone-300"
      >
        {GENRES.map((g) => (
          <option key={g.value} value={g.value}>
            {g.label}
          </option>
        ))}
      </select>

      {/* Module toggle cards */}
      <div className="space-y-1.5">
        {WRITING_MODULES.map((mod) => {
          const isActive = activeModules.has(mod.key)
          return (
            <button
              key={mod.key}
              onClick={() => toggleModule(mod.key)}
              className={`w-full text-left rounded-lg border p-2.5 transition-all ${
                isActive
                  ? 'border-stone-400 bg-white shadow-[0_0_8px_rgba(120,113,108,0.08)]'
                  : 'border-stone-150 bg-stone-50/40 hover:bg-white hover:border-stone-200'
              }`}
            >
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2 flex-1 min-w-0">
                  {/* Toggle indicator */}
                  <div
                    className={`w-7 h-4 rounded-full transition-colors flex-shrink-0 relative ${
                      isActive ? 'bg-stone-700' : 'bg-stone-200'
                    }`}
                  >
                    <div
                      className={`absolute top-0.5 w-3 h-3 rounded-full bg-white shadow transition-transform ${
                        isActive ? 'translate-x-3.5' : 'translate-x-0.5'
                      }`}
                    />
                  </div>
                  <div className="min-w-0">
                    <div className="text-xs font-medium text-stone-800 truncate">
                      {mod.label}
                    </div>
                    <div className="text-[10px] text-stone-400 truncate">
                      {mod.description}
                    </div>
                  </div>
                </div>
                <Tooltip text={mod.tooltip} />
              </div>
            </button>
          )
        })}
      </div>

      {/* Prohibitions section */}
      <div className="border-t border-stone-200 pt-2">
        <button
          onClick={() => setShowProhibitions(!showProhibitions)}
          className="w-full flex items-center justify-between text-xs text-stone-600 hover:text-stone-800 transition-colors py-1"
        >
          <span className="font-medium">
            禁忌清单
            <span className="text-[10px] text-stone-400 ml-1.5">
              ({PROHIBITIONS.length} 条)
            </span>
          </span>
          <svg
            className={`w-3.5 h-3.5 text-stone-400 transition-transform ${
              showProhibitions ? 'rotate-180' : ''
            }`}
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
            strokeWidth={2}
          >
            <path strokeLinecap="round" strokeLinejoin="round" d="M19 9l-7 7-7-7" />
          </svg>
        </button>

        {showProhibitions && (
          <div className="mt-1.5 space-y-1">
            {PROHIBITIONS.map((rule, idx) => (
              <div
                key={idx}
                className="flex items-start gap-1.5 text-[11px] text-stone-600 bg-red-50/40 rounded px-2 py-1.5"
              >
                <span className="text-red-400 flex-shrink-0 mt-px text-[10px]">
                  {idx + 1}.
                </span>
                <span className="leading-relaxed">{rule}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
