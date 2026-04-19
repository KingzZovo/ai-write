'use client'

import React, { useEffect, useRef, useState } from 'react'
import type { Project } from '@/stores/projectStore'

export interface ProjectStats {
  volumeCount: number
  chapterCount: number
  totalWords: number
}

interface Props {
  project: Project
  stats?: ProjectStats
  selected?: boolean
  selectable?: boolean
  onToggleSelect?: (id: string) => void
  onOpen: (id: string) => void
  onRename: (project: Project) => void
  onDelete: (project: Project) => void
  onSettings: (project: Project) => void
}

function formatRelative(iso?: string): string {
  if (!iso) return ''
  const t = new Date(iso).getTime()
  if (Number.isNaN(t)) return ''
  const diff = (Date.now() - t) / 1000
  if (diff < 60) return '刚刚'
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`
  if (diff < 30 * 86400) return `${Math.floor(diff / 86400)} 天前`
  return new Date(iso).toLocaleDateString('zh-CN')
}

export function ProjectCard({
  project,
  stats,
  selected,
  selectable,
  onToggleSelect,
  onOpen,
  onRename,
  onDelete,
  onSettings,
}: Props) {
  const handleBodyClick = () => {
    if (selectable) onToggleSelect?.(project.id)
    else onOpen(project.id)
  }
  const stop = (e: React.MouseEvent) => e.stopPropagation()

  return (
    <div
      onClick={handleBodyClick}
      className={`relative rounded-xl border bg-white p-4 cursor-pointer transition-shadow hover:shadow-md ${
        selected ? 'border-blue-500 ring-2 ring-blue-300' : 'border-gray-200'
      }`}
    >
      {selectable && (
        <input
          type="checkbox"
          checked={!!selected}
          onChange={() => onToggleSelect?.(project.id)}
          onClick={stop}
          className="absolute top-3 left-3"
        />
      )}
      <div className={`${selectable ? 'pl-7' : ''} pr-8`}>
        <h3 className="text-base font-semibold text-gray-900 truncate">
          {project.title}
        </h3>
        {project.genre && (
          <span className="inline-block mt-1 px-2 py-0.5 text-[10px] bg-gray-100 text-gray-600 rounded">
            {project.genre}
          </span>
        )}
        {project.premise && (
          <p className="mt-2 text-xs text-gray-500 line-clamp-2">
            {project.premise}
          </p>
        )}
        <div className="mt-3 text-[11px] text-gray-400 flex items-center gap-2 flex-wrap">
          <span>{formatRelative(project.created_at)}</span>
          {stats && (
            <>
              <span>·</span>
              <span>{stats.volumeCount} 卷</span>
              <span>·</span>
              <span>{stats.chapterCount} 章</span>
              {stats.totalWords > 0 && (
                <>
                  <span>·</span>
                  <span>{stats.totalWords.toLocaleString()} 字</span>
                </>
              )}
            </>
          )}
        </div>
      </div>
      {!selectable && (
        <ProjectCardMenu
          onRename={() => onRename(project)}
          onSettings={() => onSettings(project)}
          onDelete={() => onDelete(project)}
        />
      )}
    </div>
  )
}

function ProjectCardMenu({
  onRename,
  onSettings,
  onDelete,
}: {
  onRename: () => void
  onSettings: () => void
  onDelete: () => void
}) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [open])

  return (
    <div ref={ref} className="absolute top-2 right-2" onClick={(e) => e.stopPropagation()}>
      <button
        onClick={() => setOpen((v) => !v)}
        className="w-7 h-7 flex items-center justify-center rounded hover:bg-gray-100 text-gray-500"
        aria-label="more"
      >
        ⋯
      </button>
      {open && (
        <div className="absolute right-0 mt-1 w-28 bg-white border border-gray-200 rounded-lg shadow-lg overflow-hidden z-10">
          <button
            onClick={() => { setOpen(false); onRename() }}
            className="block w-full text-left px-3 py-2 text-sm hover:bg-gray-50"
          >
            重命名
          </button>
          <button
            onClick={() => { setOpen(false); onSettings() }}
            className="block w-full text-left px-3 py-2 text-sm hover:bg-gray-50"
          >
            项目设置
          </button>
          <button
            onClick={() => { setOpen(false); onDelete() }}
            className="block w-full text-left px-3 py-2 text-sm text-red-600 hover:bg-red-50"
          >
            删除
          </button>
        </div>
      )}
    </div>
  )
}
