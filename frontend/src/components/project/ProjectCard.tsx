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

// Chinese-novel-friendly word count: 12.6 万 instead of 126,000.
function formatWords(n: number): string {
  if (n >= 10000) {
    const w = n / 10000
    return `${w >= 100 ? Math.round(w) : w.toFixed(1).replace(/\.0$/, '')} 万`
  }
  return n.toLocaleString()
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
      className={`group relative rounded-xl border bg-white p-4 cursor-pointer shadow-card transition-all duration-200 hover:shadow-card-hover hover:-translate-y-0.5 ${
        selected
          ? 'border-brand-500 ring-2 ring-brand-200'
          : 'border-gray-200 hover:border-brand-200'
      }`}
    >
      {selectable && (
        <input
          type="checkbox"
          checked={!!selected}
          onChange={() => onToggleSelect?.(project.id)}
          onClick={stop}
          className="absolute top-3 left-3 w-4 h-4 accent-brand-600"
        />
      )}
      <div className={`${selectable ? 'pl-7' : ''} pr-8`}>
        <h3 className="text-base font-semibold text-gray-900 truncate transition-colors group-hover:text-brand-700">
          {project.title}
        </h3>
        {project.genre && (
          <span className="inline-block mt-1.5 px-2 py-0.5 text-[11px] bg-gray-100 text-gray-600 rounded-full">
            {project.genre}
          </span>
        )}
        {project.premise && (
          <p className="mt-2 text-xs leading-relaxed text-gray-500 line-clamp-2">
            {project.premise}
          </p>
        )}
        <div className="mt-3 pt-3 border-t border-gray-100 flex items-center justify-between gap-2">
          {stats ? (
            <div className="flex items-center gap-3">
              <CardStat value={String(stats.volumeCount)} label="卷" />
              <CardStat value={String(stats.chapterCount)} label="章" />
              {stats.totalWords > 0 && (
                <CardStat value={formatWords(stats.totalWords)} label="字" />
              )}
            </div>
          ) : (
            <div className="flex items-center gap-3">
              <span className="skeleton h-3.5 w-8 rounded" />
              <span className="skeleton h-3.5 w-8 rounded" />
              <span className="skeleton h-3.5 w-12 rounded" />
            </div>
          )}
          <span className="text-[11px] text-gray-400 shrink-0">
            {formatRelative(project.created_at)}
          </span>
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

function CardStat({ value, label }: { value: string; label: string }) {
  return (
    <span className="flex items-baseline gap-0.5">
      <span className="text-sm font-semibold text-gray-700 tabular-nums">{value}</span>
      <span className="text-[10px] text-gray-400">{label}</span>
    </span>
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
        className="px-2 h-7 flex items-center justify-center rounded-md hover:bg-gray-100 text-gray-500 hover:text-gray-700 text-xs font-medium transition-colors disabled:opacity-50"
        aria-label="export"
        title={t('project.export')}
      >
        {busy ? '…' : t('project.export')}
      </button>
      {open && (
        <div className="absolute right-0 mt-1 w-28 bg-white border border-gray-200 rounded-lg shadow-popover py-1 z-10 animate-dropdown">
          {EXPORT_FORMATS.map((fmt) => (
            <button
              key={fmt}
              onClick={() => download(fmt)}
              className="block w-[calc(100%-0.5rem)] mx-1 text-left px-2.5 py-1.5 text-xs font-medium tracking-wide text-gray-600 rounded-md hover:bg-gray-50 hover:text-gray-900 uppercase transition-colors"
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
        className="w-7 h-7 flex items-center justify-center rounded-md hover:bg-gray-100 text-gray-500 hover:text-gray-700 transition-colors"
        aria-label="more"
      >
        ⋯
      </button>
      {open && (
        <div className="absolute right-0 mt-1 w-32 bg-white border border-gray-200 rounded-lg shadow-popover py-1 z-10 animate-dropdown">
          {PROJECT_LINKS.map(({ path, key }) => (
            <Link
              key={key}
              href={path(projectId)}
              onClick={() => setOpen(false)}
              className="block mx-1 px-2.5 py-1.5 text-sm text-gray-700 rounded-md hover:bg-gray-50 transition-colors"
            >
              {t(key)}
            </Link>
          ))}
          <div className="my-1 border-t border-gray-100" />
          <button
            onClick={() => { setOpen(false); onRename() }}
            className="block w-[calc(100%-0.5rem)] mx-1 text-left px-2.5 py-1.5 text-sm text-gray-700 rounded-md hover:bg-gray-50 transition-colors"
          >
            重命名
          </button>
          <button
            onClick={() => { setOpen(false); onSettings() }}
            className="block w-[calc(100%-0.5rem)] mx-1 text-left px-2.5 py-1.5 text-sm text-gray-700 rounded-md hover:bg-gray-50 transition-colors"
          >
            项目设置
          </button>
          <button
            onClick={() => { setOpen(false); onDelete() }}
            className="block w-[calc(100%-0.5rem)] mx-1 text-left px-2.5 py-1.5 text-sm text-danger-600 rounded-md hover:bg-danger-50 transition-colors"
          >
            删除
          </button>
        </div>
      )}
    </div>
  )
}
