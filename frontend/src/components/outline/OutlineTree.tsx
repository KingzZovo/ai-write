'use client'

import React, { useState } from 'react'
import { useProjectStore } from '@/stores/projectStore'
import { apiFetch } from '@/lib/api'
import { VolumeOutlineBlock } from '@/components/outline/VolumeOutlineBlock'
import { RowMenu } from './RowMenu'
import { DeleteVolumeModal } from './DeleteVolumeModal'
import { DeleteChapterModal } from './DeleteChapterModal'
import { RegenerateVolumeModal } from './RegenerateVolumeModal'

interface OutlineTreeProps {
  projectId: string
  onSelectChapter?: (chapterId: string) => void
  volumeOutlines?: Record<number, Record<string, unknown>>
  // PR-OL14: top-level book outline (content_json) for "全书大纲" view.
  bookOutline?: Record<string, unknown> | null
  onChanged?: () => void
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

export function OutlineTree({ projectId, onSelectChapter, volumeOutlines, bookOutline, onChanged }: OutlineTreeProps) {
  const { volumes, chapters, selectedChapterId, selectChapter } = useProjectStore()
  const [expandedNodes, setExpandedNodes] = useState<Set<string>>(new Set())
  const [outlineOpen, setOutlineOpen] = useState<Set<string>>(new Set())
  // PR-OL14: top-level (book) toggle + per-chapter outline toggle
  const [bookOpen, setBookOpen] = useState(false)
  const [chapterOutlineOpen, setChapterOutlineOpen] = useState<Set<string>>(new Set())
  const toggleChapterOutline = (id: string) => {
    setChapterOutlineOpen((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const [renamingVolumeId, setRenamingVolumeId] = useState<string | null>(null)
  const [renameVolumeValue, setRenameVolumeValue] = useState('')
  const [renamingChapterId, setRenamingChapterId] = useState<string | null>(null)
  const [renameChapterValue, setRenameChapterValue] = useState('')
  const [deleteVolume, setDeleteVolume] = useState<{ id: string; title: string; chapterCount: number } | null>(null)
  const [deleteChapter, setDeleteChapter] = useState<{ id: string; title: string } | null>(null)
  const [regenerateVolume, setRegenerateVolume] = useState<{ id: string; title: string; chapterCount: number } | null>(null)

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
      {/* PR-OL14: top-level book-outline view */}
      {bookOutline && (
        <div className="mb-2 px-3">
          <button
            onClick={() => setBookOpen((v) => !v)}
            className="flex items-center w-full py-1 text-xs text-emerald-700 hover:bg-emerald-50 rounded"
          >
            <span className="mr-1">{bookOpen ? '▼' : '▶'}</span>
            <span className="font-medium">全书大纲</span>
          </button>
          {bookOpen && (
            <div className="mt-1 px-3 py-2 text-xs whitespace-pre-wrap bg-emerald-50/40 border-l-2 border-emerald-200 rounded-r max-h-96 overflow-y-auto">
              {String(
                (() => {
                  const raw = (bookOutline as Record<string, unknown>)['raw_text']
                  const text = (typeof raw === 'string' && raw)
                    ? raw
                    : JSON.stringify(bookOutline, null, 2)
                  // PR-OL15-FE: strip <volume-plan>...</volume-plan> tag fallback.
                  return text.replace(/<volume-plan>[\s\S]+?<\/volume-plan>\s*/g, '')
                })()
              )}
            </div>
          )}
        </div>
      )}
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
            <div className="flex items-center w-full px-3 py-1.5 hover:bg-gray-100 rounded group">
              <button
                onClick={() => toggleNode(volume.id)}
                className="flex-1 flex items-center text-left min-w-0"
              >
                <span className="mr-1 text-gray-400 text-xs">
                  {expandedNodes.has(volume.id) ? '▼' : '▶'}
                </span>
                {renamingVolumeId === volume.id ? (
                  <input
                    autoFocus
                    value={renameVolumeValue}
                    onClick={(e) => e.stopPropagation()}
                    onChange={(e) => setRenameVolumeValue(e.target.value)}
                    onKeyDown={async (e) => {
                      if (e.key === 'Enter') {
                        const v = renameVolumeValue.trim() || volume.title
                        await apiFetch(`/api/projects/${projectId}/volumes/${volume.id}`, {
                          method: 'PUT',
                          body: JSON.stringify({ title: v }),
                        })
                        setRenamingVolumeId(null)
                        onChanged?.()
                      } else if (e.key === 'Escape') {
                        setRenamingVolumeId(null)
                      }
                    }}
                    onBlur={() => setRenamingVolumeId(null)}
                    className="text-sm flex-1 px-1 border border-blue-300 rounded"
                  />
                ) : (
                  <span className="font-medium text-gray-700 flex-1 truncate">
                    {volume.title}
                  </span>
                )}
                <span className="text-[10px] text-gray-400 ml-1 opacity-0 group-hover:opacity-100 transition-opacity">
                  {volChapters.length}章 {totalWords > 0 ? `${Math.round(totalWords / 1000)}k字` : ''}
                </span>
              </button>
              <div className="opacity-0 group-hover:opacity-100 transition-opacity ml-1">
                <RowMenu
                  items={[
                    {
                      label: '重命名',
                      onClick: () => {
                        setRenameVolumeValue(volume.title)
                        setRenamingVolumeId(volume.id)
                      },
                    },
                    {
                      label: '重新生成',
                      onClick: () =>
                        setRegenerateVolume({
                          id: volume.id,
                          title: volume.title,
                          chapterCount: volChapters.length,
                        }),
                    },
                    {
                      label: '删除',
                      danger: true,
                      onClick: () =>
                        setDeleteVolume({
                          id: volume.id,
                          title: volume.title,
                          chapterCount: volChapters.length,
                        }),
                    },
                  ]}
                />
              </div>
            </div>

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
                    <React.Fragment key={chapter.id}>
                    <div
                      className={`flex items-center w-full px-3 py-1 rounded group ${
                        selectedChapterId === chapter.id ? 'bg-blue-50' : 'hover:bg-gray-50'
                      }`}
                    >
                      <button
                        onClick={() => handleSelectChapter(chapter.id)}
                        className="flex-1 flex items-center text-left min-w-0"
                      >
                        <span className="mr-1.5 text-gray-300">-</span>
                        {renamingChapterId === chapter.id ? (
                          <input
                            autoFocus
                            value={renameChapterValue}
                            onClick={(e) => e.stopPropagation()}
                            onChange={(e) => setRenameChapterValue(e.target.value)}
                            onKeyDown={async (e) => {
                              if (e.key === 'Enter') {
                                const v = renameChapterValue.trim() || chapter.title
                                await apiFetch(`/api/projects/${projectId}/chapters/${chapter.id}`, {
                                  method: 'PUT',
                                  body: JSON.stringify({ title: v }),
                                })
                                setRenamingChapterId(null)
                                onChanged?.()
                              } else if (e.key === 'Escape') {
                                setRenamingChapterId(null)
                              }
                            }}
                            onBlur={() => setRenamingChapterId(null)}
                            className="text-sm flex-1 px-1 border border-blue-300 rounded"
                          />
                        ) : (
                          <span
                            className={`flex-1 truncate ${
                              selectedChapterId === chapter.id
                                ? 'text-blue-700'
                                : 'text-gray-600'
                            }`}
                          >
                            {chapter.title}
                          </span>
                        )}
                        <span className="flex items-center gap-1 ml-1">
                          {wc > 0 && (
                            <span className="text-[10px] text-gray-400">
                              {wc > 1000 ? `${(wc / 1000).toFixed(1)}k` : wc}
                            </span>
                          )}
                          <span
                            className={`text-[9px] px-1 py-0.5 rounded ${
                              statusColors[st] || statusColors.draft
                            }`}
                          >
                            {statusLabels[st] || st}
                          </span>
                        </span>
                      </button>
                      {/* PR-OL14: per-chapter outline toggle */}
                      {Boolean(chapter.outline_json || (chapter as unknown as { outlineJson?: unknown }).outlineJson) ? (
                        <button
                          title="查看本章大纲"
                          onClick={(e) => { e.stopPropagation(); toggleChapterOutline(chapter.id) }}
                          className="text-[10px] text-amber-600 hover:bg-amber-50 px-1 rounded ml-1"
                        >
                          {chapterOutlineOpen.has(chapter.id) ? '▼大纲' : '▶大纲'}
                        </button>
                      ) : null}
                      <div className="opacity-0 group-hover:opacity-100 transition-opacity ml-1">
                        <RowMenu
                          items={[
                            {
                              label: '重命名',
                              onClick: () => {
                                setRenameChapterValue(chapter.title)
                                setRenamingChapterId(chapter.id)
                              },
                            },
                            {
                              label: '删除',
                              danger: true,
                              onClick: () =>
                                setDeleteChapter({ id: chapter.id, title: chapter.title }),
                            },
                          ]}
                        />
                      </div>
                    </div>
                    {/* PR-OL14: chapter outline panel */}
                    {chapterOutlineOpen.has(chapter.id) && Boolean(chapter.outline_json || (chapter as unknown as { outlineJson?: unknown }).outlineJson) && (
                      <div className="ml-8 mb-1 px-3 py-2 text-[11px] whitespace-pre-wrap bg-amber-50/40 border-l-2 border-amber-200 rounded-r max-h-72 overflow-y-auto">
                        {((): string | null => {
                          const oj = (chapter.outline_json ?? (chapter as unknown as { outlineJson?: unknown }).outlineJson) as Record<string, unknown> | string | null
                          if (!oj) return null
                          if (typeof oj === "string") return oj
                          if (typeof oj === "object" && oj && "raw_text" in oj && typeof (oj as Record<string, unknown>).raw_text === "string") {
                            return String((oj as Record<string, unknown>).raw_text)
                          }
                          try { return JSON.stringify(oj, null, 2) } catch { return String(oj) }
                        })()}
                      </div>
                    )}
                    </React.Fragment>
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

      {deleteVolume && (
        <DeleteVolumeModal
          projectId={projectId}
          volumeId={deleteVolume.id}
          volumeTitle={deleteVolume.title}
          chapterCount={deleteVolume.chapterCount}
          onClose={() => setDeleteVolume(null)}
          onDone={() => {
            setDeleteVolume(null)
            onChanged?.()
          }}
        />
      )}
      {deleteChapter && (
        <DeleteChapterModal
          projectId={projectId}
          chapterId={deleteChapter.id}
          chapterTitle={deleteChapter.title}
          onClose={() => setDeleteChapter(null)}
          onDone={() => {
            setDeleteChapter(null)
            onChanged?.()
          }}
        />
      )}
      {regenerateVolume && (
        <RegenerateVolumeModal
          projectId={projectId}
          volumeId={regenerateVolume.id}
          volumeTitle={regenerateVolume.title}
          chapterCount={regenerateVolume.chapterCount}
          onClose={() => setRegenerateVolume(null)}
          onDone={() => {
            setRegenerateVolume(null)
            onChanged?.()
          }}
        />
      )}
    </div>
  )
}
