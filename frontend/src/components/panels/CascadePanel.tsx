'use client'

import React, { useState, useEffect, useCallback } from 'react'
import { apiFetch } from '@/lib/api'

interface AffectedChapter {
  id: string
  title: string
  chapterIdx: number
  impactLevel: 'high' | 'medium' | 'low'
}

interface CascadeAnalysis {
  affectedChapters: AffectedChapter[]
  editedChapterId: string
  reason: string
}

interface CascadePanelProps {
  projectId: string
  chapterId: string
}

const IMPACT_CONFIG = {
  high: { color: 'bg-red-100 text-red-700 border-red-200', label: 'High' },
  medium: { color: 'bg-yellow-100 text-yellow-700 border-yellow-200', label: 'Medium' },
  low: { color: 'bg-gray-100 text-gray-600 border-gray-200', label: 'Low' },
}

export function CascadePanel({ projectId, chapterId }: CascadePanelProps) {
  const [analysis, setAnalysis] = useState<CascadeAnalysis | null>(null)
  const [loading, setLoading] = useState(false)
  const [regenerating, setRegenerating] = useState(false)
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set())
  const [dismissed, setDismissed] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const fetchAnalysis = useCallback(async () => {
    if (!chapterId) return
    setLoading(true)
    setError(null)
    try {
      const data = await apiFetch<CascadeAnalysis>(
        `/api/chapters/${chapterId}/cascade-analysis`,
        { method: 'POST' }
      )
      setAnalysis(data)
      // By default, select all high-impact chapters
      const highImpact = new Set(
        data.affectedChapters
          .filter((c) => c.impactLevel === 'high')
          .map((c) => c.id)
      )
      setSelectedIds(highImpact)
    } catch {
      setAnalysis(null)
    } finally {
      setLoading(false)
    }
  }, [chapterId])

  useEffect(() => {
    setDismissed(false)
    fetchAnalysis()
  }, [fetchAnalysis])

  const toggleSelection = useCallback((id: string) => {
    setSelectedIds((prev) => {
      const next = new Set(prev)
      if (next.has(id)) {
        next.delete(id)
      } else {
        next.add(id)
      }
      return next
    })
  }, [])

  const selectAll = useCallback(() => {
    if (!analysis) return
    setSelectedIds(new Set(analysis.affectedChapters.map((c) => c.id)))
  }, [analysis])

  const handleRegenerateAll = useCallback(async () => {
    if (!analysis || regenerating) return
    setRegenerating(true)
    setError(null)
    try {
      const allIds = analysis.affectedChapters.map((c) => c.id)
      await apiFetch(`/api/projects/${projectId}/cascade-regenerate`, {
        method: 'POST',
        body: JSON.stringify({
          source_chapter_id: chapterId,
          chapter_ids: allIds,
        }),
      })
      setDismissed(true)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Regeneration failed')
    } finally {
      setRegenerating(false)
    }
  }, [analysis, regenerating, projectId, chapterId])

  const handleRegenerateSelected = useCallback(async () => {
    if (selectedIds.size === 0 || regenerating) return
    setRegenerating(true)
    setError(null)
    try {
      await apiFetch(`/api/projects/${projectId}/cascade-regenerate`, {
        method: 'POST',
        body: JSON.stringify({
          source_chapter_id: chapterId,
          chapter_ids: Array.from(selectedIds),
        }),
      })
      setDismissed(true)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Regeneration failed')
    } finally {
      setRegenerating(false)
    }
  }, [selectedIds, regenerating, projectId, chapterId])

  if (dismissed) return null

  if (loading) {
    return <p className="text-xs text-gray-400 px-4 py-2">Analyzing cascade impact...</p>
  }

  if (!analysis || analysis.affectedChapters.length === 0) {
    return null
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold text-gray-900">Cascade Regeneration</h3>
        <button
          onClick={() => setDismissed(true)}
          className="text-xs text-gray-400 hover:text-gray-600"
          title="Dismiss"
        >
          Dismiss
        </button>
      </div>

      {/* Info banner */}
      <div className="bg-amber-50 border border-amber-200 rounded-lg p-2.5 text-xs text-amber-800">
        <p className="font-medium mb-0.5">Chapter edited - downstream impact detected</p>
        <p className="text-amber-600">
          {analysis.affectedChapters.length} subsequent chapter
          {analysis.affectedChapters.length !== 1 ? 's' : ''} may need regeneration.
        </p>
      </div>

      {/* Affected chapters list */}
      <div className="space-y-1">
        <div className="flex items-center justify-between">
          <span className="text-[10px] text-gray-500 uppercase font-medium">
            Affected Chapters
          </span>
          <button
            onClick={selectAll}
            className="text-[10px] text-blue-600 hover:text-blue-700"
          >
            Select All
          </button>
        </div>

        {analysis.affectedChapters.map((chapter) => {
          const impact = IMPACT_CONFIG[chapter.impactLevel]
          const isSelected = selectedIds.has(chapter.id)

          return (
            <label
              key={chapter.id}
              className={`flex items-center gap-2 bg-white border rounded-lg p-2 text-xs cursor-pointer transition-colors ${
                isSelected ? 'border-blue-300 bg-blue-50/50' : 'border-gray-200'
              }`}
            >
              <input
                type="checkbox"
                checked={isSelected}
                onChange={() => toggleSelection(chapter.id)}
                className="rounded border-gray-300 text-blue-600 focus:ring-blue-500"
              />
              <span className="flex-1 text-gray-800 truncate">
                Ch.{chapter.chapterIdx} - {chapter.title}
              </span>
              <span
                className={`px-1.5 py-0.5 rounded text-[10px] font-medium ${impact.color}`}
              >
                {impact.label}
              </span>
            </label>
          )
        })}
      </div>

      {error && <p className="text-xs text-red-500">{error}</p>}

      {/* Action buttons */}
      <div className="space-y-1.5">
        <button
          onClick={handleRegenerateAll}
          disabled={regenerating}
          className="w-full px-3 py-1.5 text-xs bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed font-medium"
        >
          {regenerating ? 'Regenerating...' : 'Regenerate All'}
        </button>
        <button
          onClick={handleRegenerateSelected}
          disabled={regenerating || selectedIds.size === 0}
          className="w-full px-3 py-1.5 text-xs bg-white border border-blue-300 text-blue-700 rounded-lg hover:bg-blue-50 disabled:opacity-50 disabled:cursor-not-allowed font-medium"
        >
          Regenerate Selected ({selectedIds.size})
        </button>
      </div>
    </div>
  )
}
