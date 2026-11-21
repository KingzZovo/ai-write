'use client'

import React, { useCallback, useEffect, useState } from 'react'
import { useRouter } from 'next/navigation'
import { apiFetch } from '@/lib/api'
import type { Project } from '@/stores/projectStore'
import { useProjectStore } from '@/stores/projectStore'
import { ProjectCard, type ProjectStats } from './ProjectCard'
import { NewProjectModal } from './NewProjectModal'
import { RenameProjectModal } from './RenameProjectModal'
import { DeleteProjectModal } from './DeleteProjectModal'
import { BulkDeleteModal } from './BulkDeleteModal'
import { ProjectSettingsModal } from './ProjectSettingsModal'

interface ListRes {
  projects: Project[]
  total: number
}

interface VolumeBrief { id: string; project_id: string }
interface ChapterBrief { id: string; volume_id: string; word_count: number }

export function ProjectListPage() {
  const router = useRouter()
  const { setProjects, setCurrentProject, projects } = useProjectStore()
  const [loading, setLoading] = useState(true)
  const [stats, setStats] = useState<Record<string, ProjectStats>>({})

  const [showNew, setShowNew] = useState(false)
  const [renameTarget, setRenameTarget] = useState<Project | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<Project | null>(null)
  const [settingsTarget, setSettingsTarget] = useState<Project | null>(null)

  const [selectMode, setSelectMode] = useState(false)
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set())
  const [showBulkDelete, setShowBulkDelete] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const data = await apiFetch<ListRes>('/api/projects')
      setProjects(data.projects)
    } finally {
      setLoading(false)
    }
  }, [setProjects])

  useEffect(() => { load() }, [load])

  // Lazy-compute stats per project (bounded concurrency: 3)
  useEffect(() => {
    let cancelled = false
    async function computeFor(p: Project) {
      try {
        const vols = await apiFetch<VolumeBrief[]>(`/api/projects/${p.id}/volumes`)
        const chs = await apiFetch<ChapterBrief[]>(`/api/projects/${p.id}/chapters?lightweight=true`)
        if (cancelled) return
        const totalWords = chs.reduce((s, c) => s + (c.word_count || 0), 0)
        setStats((prev) => ({
          ...prev,
          [p.id]: { volumeCount: vols.length, chapterCount: chs.length, totalWords },
        }))
      } catch { /* ignore per-project failure */ }
    }
    const queue = [...projects]
    async function worker() {
      while (queue.length > 0 && !cancelled) {
        const p = queue.shift()!
        await computeFor(p)
      }
    }
    Promise.all([worker(), worker(), worker()])
    return () => { cancelled = true }
  }, [projects])

  const toggleSelect = useCallback((id: string) => {
    setSelectedIds((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id); else next.add(id)
      return next
    })
  }, [])

  const handleOpen = useCallback((id: string) => {
    const p = projects.find((x) => x.id === id)
    if (p) setCurrentProject(p)
    router.push(`/workspace?id=${id}`)
  }, [projects, router, setCurrentProject])

  const handleBulkDelete = useCallback(async () => {
    for (const id of selectedIds) {
      await apiFetch(`/api/projects/${id}`, { method: 'DELETE' })
    }
    setSelectedIds(new Set())
    setSelectMode(false)
    setShowBulkDelete(false)
    await load()
  }, [selectedIds, load])

  return (
    <div className="min-h-screen pt-12 bg-gray-50">
      <div className="max-w-6xl mx-auto px-3 md:px-6 py-6 md:py-8">
        <div
          data-testid="project-list-header"
          className="flex flex-wrap items-center justify-between gap-y-3 mb-6"
        >
          <h1 className="text-xl md:text-2xl font-semibold tracking-tight text-gray-900">我的项目</h1>
          <div className="flex flex-wrap items-center gap-2">
            <button
              onClick={() => router.push('/trash')}
              className="px-3 py-1.5 text-sm font-medium text-gray-600 hover:text-gray-900 hover:bg-gray-200/60 rounded-lg transition-colors"
            >
              回收站
            </button>
            <button
              onClick={() => {
                if (selectMode) { setSelectMode(false); setSelectedIds(new Set()) }
                else setSelectMode(true)
              }}
              className="px-3 py-1.5 text-sm font-medium text-gray-600 hover:text-gray-900 hover:bg-gray-200/60 rounded-lg transition-colors"
            >
              {selectMode ? '取消多选' : '多选'}
            </button>
            <button
              onClick={() => setShowNew(true)}
              className="px-4 py-1.5 text-sm font-medium bg-brand-600 text-white rounded-lg hover:bg-brand-700 active:bg-brand-800 shadow-card transition-colors"
            >
              + 新建项目
            </button>
          </div>
        </div>

        {selectMode && (
          <div className="mb-4 flex items-center gap-3 animate-fade-in">
            <span className="text-sm text-gray-600">已选 {selectedIds.size} 项</span>
            <button
              onClick={() => setShowBulkDelete(true)}
              disabled={selectedIds.size === 0}
              className="px-3 py-1.5 text-sm font-medium bg-danger-600 text-white rounded-lg hover:bg-danger-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              删除选中
            </button>
          </div>
        )}

        {loading ? (
          <div
            data-testid="project-list-skeleton"
            className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-4"
          >
            {Array.from({ length: 8 }).map((_, i) => (
              <div key={i} className="rounded-xl border border-gray-200 bg-white p-4 shadow-card">
                <div className="skeleton h-4 w-2/3 rounded" />
                <div className="skeleton mt-2 h-4 w-14 rounded-full" />
                <div className="skeleton mt-3 h-3 w-full rounded" />
                <div className="skeleton mt-1.5 h-3 w-4/5 rounded" />
                <div className="mt-4 pt-3 border-t border-gray-100 flex gap-4">
                  <div className="skeleton h-3 w-10 rounded" />
                  <div className="skeleton h-3 w-10 rounded" />
                  <div className="skeleton h-3 w-14 rounded" />
                </div>
              </div>
            ))}
          </div>
        ) : projects.length === 0 ? (
          <div className="py-24 flex flex-col items-center text-center animate-fade-in">
            <div className="w-14 h-14 mb-4 rounded-2xl bg-brand-50 text-brand-500 flex items-center justify-center">
              <svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round">
                <path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z" />
                <path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z" />
              </svg>
            </div>
            <p className="text-body font-medium text-gray-700">开始你的第一部作品</p>
            <p className="mt-1 text-sm text-gray-500">还没有项目，点击右上角&quot;+ 新建项目&quot;开始创作。</p>
          </div>
        ) : (
          <div
            data-testid="project-list-grid"
            className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-4"
          >
            {projects.map((p) => (
              <ProjectCard
                key={p.id}
                project={p}
                stats={stats[p.id]}
                selectable={selectMode}
                selected={selectedIds.has(p.id)}
                onToggleSelect={toggleSelect}
                onOpen={handleOpen}
                onRename={setRenameTarget}
                onDelete={setDeleteTarget}
                onSettings={setSettingsTarget}
              />
            ))}
          </div>
        )}
      </div>

      {showNew && (
        <NewProjectModal
          onClose={() => setShowNew(false)}
          onCreated={async (created) => {
            setShowNew(false)
            await load()
            handleOpen(created.id)
          }}
        />
      )}
      {renameTarget && (
        <RenameProjectModal
          project={renameTarget}
          onClose={() => setRenameTarget(null)}
          onDone={async () => { setRenameTarget(null); await load() }}
        />
      )}
      {deleteTarget && (
        <DeleteProjectModal
          project={deleteTarget}
          onClose={() => setDeleteTarget(null)}
          onDone={async () => { setDeleteTarget(null); await load() }}
        />
      )}
      {showBulkDelete && (
        <BulkDeleteModal
          count={selectedIds.size}
          onClose={() => setShowBulkDelete(false)}
          onConfirm={handleBulkDelete}
        />
      )}
      {settingsTarget && (
        <ProjectSettingsModal
          project={settingsTarget}
          onClose={() => setSettingsTarget(null)}
          onDone={async () => { setSettingsTarget(null); await load() }}
        />
      )}
    </div>
  )
}
