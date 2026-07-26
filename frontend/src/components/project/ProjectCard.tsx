'use client'

import React, { useEffect, useRef, useState } from 'react'
import Link from 'next/link'
import { apiDownload } from '@/lib/api'
import { useT } from '@/lib/i18n/I18nProvider'
import type { MessageKey } from '@/lib/i18n/messages'
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
        <div
          className="absolute top-2 right-2 flex items-center gap-1"
          onClick={stop}
        >
          <ExportMenu projectId={project.id} />
          <ProjectCardMenu
            projectId={project.id}
            onRename={() => onRename(project)}
            onSettings={() => onSettings(project)}
            onDelete={() => onDelete(project)}
          />
        </div>
      )}
    </div>
  )
}

// TXT first: the author reads output in the legado reader app.
const EXPORT_FORMATS = ['txt', 'epub', 'pdf', 'docx'] as const

function ExportMenu({ projectId }: { projectId: string }) {
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  const t = useT()

  useEffect(() => {
    if (!open) return
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [open])

  const download = async (fmt: string) => {
    setOpen(false)
    setBusy(true)
    try {
      await apiDownload(`/api/export/projects/${projectId}.${fmt}`)
    } catch (err) {
      alert(`${t('project.export.failed')}: ${err instanceof Error ? err.message : String(err)}`)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div ref={ref} className="relative">
      <button
        onClick={() => setOpen((v) => !v)}
        disabled={busy}
        className="px-1.5 h-7 flex items-center justify-center rounded hover:bg-gray-100 text-gray-500 text-xs disabled:opacity-50"
        aria-label="export"
        title={t('project.export')}
      >
        {busy ? '…' : t('project.export')}
      </button>
      {open && (
        <div className="absolute right-0 mt-1 w-24 bg-white border border-gray-200 rounded-lg shadow-lg overflow-hidden z-10">
          {EXPORT_FORMATS.map((fmt) => (
            <button
              key={fmt}
              onClick={() => download(fmt)}
              className="block w-full text-left px-3 py-2 text-sm hover:bg-gray-50 uppercase"
            >
              {fmt}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

// Project-scoped tool pages, linked from the card's context menu.
const PROJECT_LINKS: Array<{ path: (id: string) => string; key: MessageKey }> = [
  { path: (id) => `/characters?id=${id}`, key: 'project.link.characters' },
  { path: (id) => `/relationship-graph?id=${id}`, key: 'project.link.relationshipGraph' },
  { path: (id) => `/cascade-tasks?project_id=${id}`, key: 'project.link.cascadeTasks' },
  { path: (id) => `/changelog?id=${id}`, key: 'project.link.changelog' },
]

function ProjectCardMenu({
  projectId,
  onRename,
  onSettings,
  onDelete,
}: {
  projectId: string
  onRename: () => void
  onSettings: () => void
  onDelete: () => void
}) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  const t = useT()

  useEffect(() => {
    if (!open) return
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [open])

  return (
    <div ref={ref} className="relative">
      <button
        onClick={() => setOpen((v) => !v)}
        className="w-7 h-7 flex items-center justify-center rounded hover:bg-gray-100 text-gray-500"
        aria-label="more"
      >
        ⋯
      </button>
      {open && (
        <div className="absolute right-0 mt-1 w-28 bg-white border border-gray-200 rounded-lg shadow-lg overflow-hidden z-10">
          {PROJECT_LINKS.map(({ path, key }) => (
            <Link
              key={key}
              href={path(projectId)}
              onClick={() => setOpen(false)}
              className="block px-3 py-2 text-sm text-gray-700 hover:bg-gray-50"
            >
              {t(key)}
            </Link>
          ))}
          <div className="border-t border-gray-100" />
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
