'use client'

import React, { useEffect, useRef, useState } from 'react'
import { useProjectStore } from '@/stores/projectStore'
import type { Chapter } from '@/stores/projectStore'
import { apiFetch } from '@/lib/api'
import { chapterPositionLabel, moveAdjacentChapter } from '@/lib/chapterOrder'
import { RowMenu } from './RowMenu'
import { DeleteVolumeModal } from './DeleteVolumeModal'
import { DeleteChapterModal } from './DeleteChapterModal'
import { RegenerateVolumeModal } from './RegenerateVolumeModal'
import type { OutlineEditTarget } from './OutlineEditor'

interface OutlineTreeProps {
  projectId: string
  onSelectChapter?: (chapterId: string) => void
  volumeOutlines?: Record<number, Record<string, unknown>>
  // PR-OL14: top-level book outline (content_json) for "全书大纲" view.
  bookOutline?: Record<string, unknown> | null
  // PR-OUTLINE-CENTER-EDIT (2026-05-04): outline IDs so click handlers can
  // route to the centre editor instead of expanding inline panels in the tree.
  bookOutlineId?: string | null
  volumeOutlineIds?: Record<number, string>
  onSelectOutline?: (target: OutlineEditTarget) => void
  onEditVolume?: (volumeId: string) => void
  // Active key (e.g. 'book:<id>' / 'volume:<id>' / 'chapter:<id>') for highlighting.
  selectedOutlineKey?: string | null
  onChanged?: () => void
}

const statusColors: Record<string, string> = {
  draft: 'bg-gray-200 text-gray-600',
  generating: 'bg-yellow-100 text-yellow-700',
  completed: 'bg-green-100 text-green-700',
  needs_review: 'bg-red-100 text-red-700',
}

// Dot indicator colors matching the pills above (visual only).
const statusDots: Record<string, string> = {
  draft: 'bg-gray-400',
  generating: 'bg-yellow-500 motion-safe:animate-pulse',
  completed: 'bg-green-500',
  needs_review: 'bg-red-500',
}

const statusLabels: Record<string, string> = {
  draft: '草稿',
  generating: '生成中',
  completed: '完成',
  needs_review: '需复核',
}

export function OutlineTree(props: OutlineTreeProps) {
  return <OutlineTreeSession key={props.projectId} {...props} />
}

function OutlineTreeSession({
  projectId,
  onSelectChapter,
  volumeOutlines,
  bookOutline,
  bookOutlineId,
  volumeOutlineIds,
  onSelectOutline,
  selectedOutlineKey,
  onEditVolume,
  onChanged,
}: OutlineTreeProps) {
  const { volumes, chapters, selectedChapterId, selectChapter } = useProjectStore()
  const [expandedNodes, setExpandedNodes] = useState<Set<string>>(new Set())
  // PR-OUTLINE-CENTER-EDIT: inline expand state removed. Outline buttons now
  // call onSelectOutline so the centre editor shows the content for editing.

  const [renamingVolumeId, setRenamingVolumeId] = useState<string | null>(null)
  const [renameVolumeValue, setRenameVolumeValue] = useState('')
  const [renamingChapterId, setRenamingChapterId] = useState<string | null>(null)
  const [renameChapterValue, setRenameChapterValue] = useState('')
  const [deleteVolume, setDeleteVolume] = useState<{ id: string; title: string; chapterCount: number } | null>(null)
  const [deleteChapter, setDeleteChapter] = useState<{ id: string; title: string } | null>(null)
  const [regenerateVolume, setRegenerateVolume] = useState<{ id: string; title: string; chapterCount: number } | null>(null)
  const [moving, setMoving] = useState(false), [moveError, setMoveError] = useState('')
  const [awaitingOrder, setAwaitingOrder] = useState<Chapter[] | null>(null)
  const moveInFlight = useRef(false), active = useRef(true), latestChapters = useRef(chapters)
  const awaitingOrderRef = useRef<Chapter[] | null>(null)
  latestChapters.current = chapters
  useEffect(() => { active.current = true; return () => { active.current = false } }, [])
  useEffect(() => {
    if (awaitingOrder && chapters !== awaitingOrder) {
      awaitingOrderRef.current = null
      setAwaitingOrder(null)
    }
  }, [chapters, awaitingOrder])

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

  // PR-WS-CHAPTER-MGMT: create a chapter at the end of a volume (client
  // computes next chapter_idx; backend has no duplicate-idx guard).
  const handleCreateChapter = async (volumeId: string) => {
    const siblings = chapters.filter((c) => (c.volume_id ?? c.volumeId) === volumeId)
    const nextIdx =
      siblings.reduce((m, c) => Math.max(m, c.chapter_idx ?? c.chapterIdx ?? 0), 0) + 1
    const title = window.prompt('新章节标题：', `第${nextIdx}章`)
    if (title === null) return
    try {
      await apiFetch(`/api/projects/${projectId}/chapters`, {
        method: 'POST',
        body: JSON.stringify({
          volume_id: volumeId,
          title: title.trim() || `第${nextIdx}章`,
          chapter_idx: nextIdx,
          outline_json: {},
        }),
      })
      onChanged?.()
    } catch (err) {
      console.error('新建章节失败:', err)
      alert('新建章节失败，请重试。')
    }
  }

  const moveChapter = async (chapter: Chapter, direction: 'up' | 'down') => {
    if (moveInFlight.current || awaitingOrderRef.current) return
    const volumeId = chapter.volume_id ?? chapter.volumeId
    const siblings = chapters
      .filter((c) => (c.volume_id ?? c.volumeId) === volumeId)
      .sort((a, b) => (a.chapter_idx ?? a.chapterIdx) - (b.chapter_idx ?? b.chapterIdx))
    const pos = siblings.findIndex((c) => c.id === chapter.id)
    const target = direction === 'up' ? siblings[pos - 1] : siblings[pos + 1]
    if (!target) {
      alert(direction === 'up' ? '已是本卷第一章' : '已是本卷最后一章')
      return
    }
    moveInFlight.current = true
    setMoving(true); setMoveError('')
    try {
      await moveAdjacentChapter(projectId, chapter, target)
    } catch (err) {
      if (active.current) setMoveError(err instanceof Error ? err.message : '移动未获确认，请刷新核对顺序。')
    } finally {
      moveInFlight.current = false
      if (active.current) {
        setMoving(false)
        awaitingOrderRef.current = latestChapters.current
        setAwaitingOrder(latestChapters.current)
        onChanged?.()
      }
    }
  }

  const sortedVolumes = [...volumes].sort(
    (a, b) => (a.volume_idx ?? a.volumeIdx) - (b.volume_idx ?? b.volumeIdx)
  )

  if (sortedVolumes.length === 0) {
    return (
      <div className="px-4 py-6 text-center">
        <div className="text-lg" aria-hidden>📚</div>
        <p className="mt-1 text-xs text-gray-400">暂无卷册。请先生成大纲。</p>
      </div>
    )
  }

  return (
    <div className="text-sm">
      {moving && <p role="status" className="px-3 py-2 text-xs text-blue-600">正在保存章节顺序…</p>}
      {moveError && <p role="alert" className="px-3 py-2 text-xs text-rose-600">{moveError}</p>}
      {awaitingOrder && <div className="px-3 py-2 text-xs text-stone-600">
        请先核对重新读取的章节顺序。
        <button onClick={() => onChanged?.()} className="ml-2 text-blue-600">重新读取章节</button>
      </div>}
      {/* PR-OUTLINE-CENTER-EDIT: clickable book-outline entry. Opens the
          centre editor; no inline expand. */}
      {bookOutline && bookOutlineId && (
        <div className="mb-2 px-3">
          <button
            type="button"
            onClick={() => onSelectOutline?.({
              type: 'book',
              outlineId: bookOutlineId,
              initialJson: bookOutline,
              title: '全书大纲',
            })}
            className={`flex items-center w-full px-1.5 py-1 text-xs rounded-md transition-colors duration-150 ${
              selectedOutlineKey === `book:${bookOutlineId}`
                ? 'bg-emerald-100 text-emerald-800'
                : 'text-emerald-700 hover:bg-emerald-50'
            }`}
          >
            <span className="mr-1">📖</span>
            <span className="font-medium">全书大纲</span>
          </button>
        </div>
      )}
      {sortedVolumes.map((volume) => {
        const volIdx = volume.volume_idx ?? volume.volumeIdx
        const volOutline = volumeOutlines?.[volIdx]
        const volOutlineId = volumeOutlineIds?.[volIdx]
        const volChapters = chapters
          .filter((ch) => (ch.volume_id ?? ch.volumeId) === volume.id)
          .sort((a, b) => (a.chapter_idx ?? a.chapterIdx) - (b.chapter_idx ?? b.chapterIdx))

        const totalWords = volChapters.reduce(
          (sum, ch) => sum + (ch.word_count ?? ch.wordCount ?? 0),
          0
        )

        return (
          <div key={volume.id} className="mb-1">
            <div className="flex items-center w-full px-3 py-1.5 hover:bg-gray-100 rounded-md transition-colors duration-150 group">
              <button
                onClick={() => toggleNode(volume.id)}
                className="flex-1 flex items-center text-left min-w-0"
              >
                <span
                  className={`mr-1 inline-block text-gray-400 text-[10px] transition-transform duration-150 motion-reduce:transition-none ${
                    expandedNodes.has(volume.id) ? 'rotate-90' : ''
                  }`}
                >
                  ▶
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
                    className="text-sm flex-1 px-1 border border-blue-300 rounded focus:outline-none focus:ring-2 focus:ring-blue-400/40"
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
                      label: '新建章节',
                      onClick: () => handleCreateChapter(volume.id),
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
              <div className="ml-4 border-l border-gray-200 pl-1.5 motion-safe:transition-opacity motion-safe:duration-200 motion-safe:starting:opacity-0">
                {(volOutline || onEditVolume) && (
                  <div className="mb-1">
                    {/* PR-OUTLINE-CENTER-EDIT: clickable volume-outline entry. */}
                    {volOutlineId || onEditVolume ? (
                      <button
                        type="button"
                        onClick={() => {
                          if (volOutlineId && volOutline) onSelectOutline?.({
                            type: 'volume', volumeId: volume.id, outlineId: volOutlineId,
                            initialJson: volOutline, title: `${volume.title} · 分卷大纲`, volumeIdx: volIdx,
                          })
                          else onEditVolume?.(volume.id)
                        }}
                        className={`flex items-center w-full px-3 py-1 text-xs rounded-md transition-colors duration-150 ${
                          selectedOutlineKey === `volume:${volOutlineId}`
                            ? 'bg-indigo-100 text-indigo-800'
                            : 'text-indigo-600 hover:bg-indigo-50'
                        }`}
                      >
                        <span className="mr-1">📑</span>
                        <span>{volOutline ? '本卷大纲' : '编写本卷大纲'}</span>
                      </button>
                    ) : (
                      <span className="flex items-center w-full px-3 py-1 text-xs text-gray-400">
                        <span className="mr-1">📑</span>
                        <span>本卷大纲</span>
                      </span>
                    )}
                  </div>
                )}
                {volChapters.map((chapter) => {
                  const wc = chapter.word_count ?? chapter.wordCount ?? 0
                  const st = chapter.status || 'draft'
                  return (
                    <React.Fragment key={chapter.id}>
                    <div
                      className={`flex items-center w-full px-3 py-1 rounded-md transition-colors duration-150 group ${
                        selectedChapterId === chapter.id ? 'bg-blue-50' : 'hover:bg-gray-50'
                      }`}
                    >
                      <button
                        onClick={() => handleSelectChapter(chapter.id)}
                        className="flex-1 flex items-center text-left min-w-0"
                      >
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
                            className="text-sm flex-1 px-1 border border-blue-300 rounded focus:outline-none focus:ring-2 focus:ring-blue-400/40"
                          />
                        ) : (
                          <span
                            className={`flex-1 truncate ${
                              selectedChapterId === chapter.id
                                ? 'font-medium text-blue-700'
                                : 'text-gray-600'
                            }`}
                          >
                            {chapterPositionLabel(chapter)} {chapter.title}
                          </span>
                        )}
                        <span className="flex items-center gap-1 ml-1">
                          {wc > 0 && (
                            <span className="text-[10px] text-gray-400">
                              {wc > 1000 ? `${(wc / 1000).toFixed(1)}k` : wc}
                            </span>
                          )}
                          <span
                            className={`inline-flex items-center gap-1 text-[9px] px-1.5 py-0.5 rounded-full ${
                              statusColors[st] || statusColors.draft
                            }`}
                          >
                            <span
                              aria-hidden
                              className={`h-1 w-1 shrink-0 rounded-full ${statusDots[st] || statusDots.draft}`}
                            />
                            {statusLabels[st] || st}
                          </span>
                        </span>
                      </button>
                      {/* PR-OL14: per-chapter outline toggle */}
                      {/* PR-OUTLINE-CENTER-EDIT (2026-05-04): clickable per-chapter outline entry. */}
                      {Boolean(chapter.outline_json || (chapter as unknown as { outlineJson?: unknown }).outlineJson) ? (
                        <button
                          title="在中间打开本章大纲进行编辑"
                          onClick={(e) => {
                            e.stopPropagation()
                            const oj = (chapter.outline_json ?? (chapter as unknown as { outlineJson?: unknown }).outlineJson) as Record<string, unknown> | string | null
                            const initialJson: Record<string, unknown> | null =
                              (typeof oj === 'object' && oj !== null) ? (oj as Record<string, unknown>)
                              : (typeof oj === 'string' ? { raw_text: oj } : null)
                            onSelectOutline?.({
                              type: 'chapter',
                              chapterId: chapter.id,
                              initialJson,
                              title: `${chapter.title || ''} · 章节大纲`,
                            })
                          }}
                          className={`text-[10px] px-1 rounded transition-colors duration-150 ml-1 ${
                            selectedOutlineKey === `chapter:${chapter.id}`
                              ? 'bg-amber-100 text-amber-800'
                              : 'text-amber-600 hover:bg-amber-50'
                          }`}
                        >
                          大纲
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
                              label: '上移',
                              disabled: moving || awaitingOrder !== null,
                              onClick: () => moveChapter(chapter, 'up'),
                            },
                            {
                              label: '下移',
                              disabled: moving || awaitingOrder !== null,
                              onClick: () => moveChapter(chapter, 'down'),
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
                    {/* PR-OUTLINE-CENTER-EDIT: inline chapter-outline panel removed.
                        点击 “大纲” 后会在中间区域以可编辑的大型编辑器打开。 */}
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
