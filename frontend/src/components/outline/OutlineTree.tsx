'use client'

import React, { useState } from 'react'
import { useProjectStore } from '@/stores/projectStore'

interface TreeNode {
  id: string
  label: string
  children?: TreeNode[]
  type: 'volume' | 'chapter'
  data?: Record<string, unknown>
}

interface OutlineTreeProps {
  onSelectChapter?: (chapterId: string) => void
}

export function OutlineTree({ onSelectChapter }: OutlineTreeProps) {
  const { volumes, chapters, selectedChapterId, selectChapter } = useProjectStore()
  const [expandedNodes, setExpandedNodes] = useState<Set<string>>(new Set())

  const toggleNode = (id: string) => {
    setExpandedNodes((prev) => {
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

  const tree: TreeNode[] = volumes.map((vol) => ({
    id: vol.id,
    label: vol.title,
    type: 'volume' as const,
    children: chapters
      .filter((ch) => ch.volumeId === vol.id)
      .sort((a, b) => a.chapterIdx - b.chapterIdx)
      .map((ch) => ({
        id: ch.id,
        label: ch.title,
        type: 'chapter' as const,
      })),
  }))

  if (tree.length === 0) {
    return (
      <div className="p-4 text-sm text-gray-500">
        No volumes yet. Generate an outline to get started.
      </div>
    )
  }

  return (
    <div className="text-sm">
      {tree.map((volume) => (
        <div key={volume.id} className="mb-1">
          <button
            onClick={() => toggleNode(volume.id)}
            className="flex items-center w-full px-3 py-1.5 hover:bg-gray-100 rounded text-left"
          >
            <span className="mr-1 text-gray-400">
              {expandedNodes.has(volume.id) ? '▼' : '▶'}
            </span>
            <span className="font-medium text-gray-700">{volume.label}</span>
          </button>

          {expandedNodes.has(volume.id) && volume.children && (
            <div className="ml-4">
              {volume.children.map((chapter) => (
                <button
                  key={chapter.id}
                  onClick={() => handleSelectChapter(chapter.id)}
                  className={`flex items-center w-full px-3 py-1 rounded text-left ${
                    selectedChapterId === chapter.id
                      ? 'bg-blue-50 text-blue-700'
                      : 'hover:bg-gray-50 text-gray-600'
                  }`}
                >
                  <span className="mr-1.5 text-gray-300">-</span>
                  {chapter.label}
                </button>
              ))}
            </div>
          )}
        </div>
      ))}
    </div>
  )
}
