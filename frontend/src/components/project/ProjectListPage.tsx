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
        const chs = await apiFetch<ChapterBrief[]>(`/api/projects/${p.id}/chapters`)
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
      <div className="max-w-6xl mx-auto px-6 py-8">
        <div className="flex items-center justify-between mb-6">
          <h1 className="text-2xl font-bold text-gray-900">我的项目</h1>
          <div className="flex items-center gap-2">
            <button
              onClick={() => router.push('/trash')}
              className="px-3 py-1.5 text-sm text-gray-600 hover:bg-gray-100 rounded-lg"
            >
              回收站
            </button>
            <button
              onClick={() => {
                if (selectMode) { setSelectMode(false); setSelectedIds(new Set()) }
                else setSelectMode(true)
              }}
              className="px-3 py-1.5 text-sm text-gray-600 hover:bg-gray-100 rounded-lg"
            >
              {selectMode ? '取消多选' : '多选'}
            </button>
            <button
              onClick={() => setShowNew(true)}
              className="px-4 py-1.5 text-sm bg-blue-600 text-white rounded-lg hover:bg-blue-700"
            >
              + 新建项目
            </button>
          </div>
        </div>

        {selectMode && (
          <div className="mb-4 flex items-center gap-3">
            <span className="text-sm text-gray-600">已选 {selectedIds.size} 项</span>
            <button
              onClick={() => setShowBulkDelete(true)}
              disabled={selectedIds.size === 0}
              className="px-3 py-1.5 text-sm bg-red-600 text-white rounded-lg hover:bg-red-700 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              删除选中
            </button>
          </div>
        )}

        {loading ? (
          <div className="py-20 text-center text-gray-400">加载中...</div>
        ) : projects.length === 0 ? (
          <div className="py-20 text-center">
            <p className="text-gray-500 mb-4">还没有项目，点击右上角&quot;+ 新建项目&quot;开始创作。</p>
          </div>
        ) : (
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-4">
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
