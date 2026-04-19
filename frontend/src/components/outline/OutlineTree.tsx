'use client'

import React, { useState } from 'react'
import { useProjectStore } from '@/stores/projectStore'
import { VolumeOutlineBlock } from '@/components/outline/VolumeOutlineBlock'

interface OutlineTreeProps {
  onSelectChapter?: (chapterId: string) => void
  volumeOutlines?: Record<number, Record<string, unknown>>
}

const statusColors: Record<string, string> = {
  draft: 'bg-gray-200 text-gray-600',
  generating: 'bg-yellow-100 text-yellow-700',
  completed: 'bg-green-100 text-green-700',
}

const statusLabels: Record<string, string> = {
  draft: '草稿',
  generating: '生成中',
  completed: '完成',
}

export function OutlineTree({ onSelectChapter, volumeOutlines }: OutlineTreeProps) {
  const { volumes, chapters, selectedChapterId, selectChapter } = useProjectStore()
  const [expandedNodes, setExpandedNodes] = useState<Set<string>>(new Set())
  const [outlineOpen, setOutlineOpen] = useState<Set<string>>(new Set())

  const toggleNode = (id: string) => {
    setExpandedNodes((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const toggleOutline = (id: string) => {
    setOutlineOpen((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const handleSelectChapter = (chapterId: string) => {
    selectChapter(chapterId)
    onSelectChapter?.(chapterId)
  }

  const sortedVolumes = [...volumes].sort(
    (a, b) => (a.volume_idx ?? a.volumeIdx) - (b.volume_idx ?? b.volumeIdx)
  )

  if (sortedVolumes.length === 0) {
    return (
      <div className="p-4 text-sm text-gray-500">
        暂无卷册。请先生成大纲。
      </div>
    )
  }

  return (
    <div className="text-sm">
      {sortedVolumes.map((volume) => {
        const volIdx = volume.volume_idx ?? volume.volumeIdx
        const volOutline = volumeOutlines?.[volIdx]
        const volChapters = chapters
          .filter((ch) => (ch.volume_id ?? ch.volumeId) === volume.id)
          .sort((a, b) => (a.chapter_idx ?? a.chapterIdx) - (b.chapter_idx ?? b.chapterIdx))

        const totalWords = volChapters.reduce(
          (sum, ch) => sum + (ch.word_count ?? ch.wordCount ?? 0),
          0
        )

        return (
          <div key={volume.id} className="mb-1">
            <button
              onClick={() => toggleNode(volume.id)}
              className="flex items-center w-full px-3 py-1.5 hover:bg-gray-100 rounded text-left group"
            >
              <span className="mr-1 text-gray-400 text-xs">
                {expandedNodes.has(volume.id) ? '▼' : '▶'}
              </span>
              <span className="font-medium text-gray-700 flex-1 truncate">
                {volume.title}
              </span>
              <span className="text-[10px] text-gray-400 ml-1 opacity-0 group-hover:opacity-100 transition-opacity">
                {volChapters.length}章 {totalWords > 0 ? `${Math.round(totalWords / 1000)}k字` : ''}
              </span>
            </button>

            {expandedNodes.has(volume.id) && (
              <div className="ml-4">
                {volOutline && (
                  <div className="mb-1">
                    <button
                      onClick={() => toggleOutline(volume.id)}
                      className="flex items-center w-full px-3 py-1 text-xs text-indigo-600 hover:bg-indigo-50 rounded"
                    >
                      <span className="mr-1">
                        {outlineOpen.has(volume.id) ? '▼' : '▶'}
                      </span>
                      <span>本卷大纲</span>
                    </button>
                    {outlineOpen.has(volume.id) && (
                      <div className="px-3 py-2 text-xs bg-indigo-50/40 border-l-2 border-indigo-200 ml-2 rounded-r">
                        <VolumeOutlineBlock data={volOutline} />
                      </div>
                    )}
                  </div>
                )}
                {volChapters.map((chapter) => {
                  const wc = chapter.word_count ?? chapter.wordCount ?? 0
                  const st = chapter.status || 'draft'
                  return (
                    <button
                      key={chapter.id}
                      onClick={() => handleSelectChapter(chapter.id)}
                      className={`flex items-center w-full px-3 py-1 rounded text-left group ${
                        selectedChapterId === chapter.id
                          ? 'bg-blue-50 text-blue-700'
                          : 'hover:bg-gray-50 text-gray-600'
                      }`}
                    >
                      <span className="mr-1.5 text-gray-300">-</span>
                      <span className="flex-1 truncate">{chapter.title}</span>
                      <span className="flex items-center gap-1 ml-1">
                        {wc > 0 && (
                          <span className="text-[10px] text-gray-400">
                            {wc > 1000 ? `${(wc / 1000).toFixed(1)}k` : wc}
                          </span>
                        )}
                        <span
                          className={`text-[9px] px-1 py-0.5 rounded ${statusColors[st] || statusColors.draft}`}
                        >
                          {statusLabels[st] || st}
                        </span>
                      </span>
                    </button>
                  )
                })}
                {volChapters.length === 0 && (
                  <div className="px-3 py-1 text-xs text-gray-400">暂无章节</div>
                )}
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}
