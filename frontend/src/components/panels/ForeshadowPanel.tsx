'use client'

import React, { useState, useEffect } from 'react'
import { apiFetch } from '@/lib/api'

interface Foreshadow {
  id: string
  type: 'major' | 'minor' | 'hint'
  description: string
  plantedChapter: number
  resolveConditions: string[]
  narrativeProximity: number
  status: 'planted' | 'ripening' | 'ready' | 'resolved'
  resolvedChapter: number | null
}

interface ForeshadowPanelProps {
  projectId: string
}

const STATUS_CONFIG = {
  planted: { color: 'bg-green-100 text-green-700', label: 'Planted' },
  ripening: { color: 'bg-yellow-100 text-yellow-700', label: 'Ripening' },
  ready: { color: 'bg-red-100 text-red-700', label: 'Ready' },
  resolved: { color: 'bg-gray-100 text-gray-500', label: 'Resolved' },
}

const TYPE_LABELS = { major: 'Major', minor: 'Minor', hint: 'Hint' }

export function ForeshadowPanel({ projectId }: ForeshadowPanelProps) {
  const [foreshadows, setForeshadows] = useState<Foreshadow[]>([])
  const [loading, setLoading] = useState(false)
  const [showForm, setShowForm] = useState(false)
  const [filter, setFilter] = useState<string>('active')

  const fetchForeshadows = async () => {
    if (!projectId) return
    setLoading(true)
    try {
      const data = await apiFetch<Foreshadow[]>(
        `/api/projects/${projectId}/foreshadows${filter === 'active' ? '?status=active' : ''}`
      )
      setForeshadows(data)
    } catch {
      // ignore
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    fetchForeshadows()
  }, [projectId, filter]) // eslint-disable-line react-hooks/exhaustive-deps

  const grouped = {
    ready: foreshadows.filter((f) => f.status === 'ready'),
    ripening: foreshadows.filter((f) => f.status === 'ripening'),
    planted: foreshadows.filter((f) => f.status === 'planted'),
    resolved: foreshadows.filter((f) => f.status === 'resolved'),
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold text-gray-900">Foreshadows</h3>
        <button
          onClick={() => setShowForm(!showForm)}
          className="text-xs text-blue-600 hover:text-blue-700"
        >
          + Add
        </button>
      </div>

      {/* Filter tabs */}
      <div className="flex gap-1">
        {['active', 'all'].map((f) => (
          <button
            key={f}
            onClick={() => setFilter(f)}
            className={`px-2 py-1 text-xs rounded ${
              filter === f ? 'bg-blue-100 text-blue-700' : 'text-gray-500 hover:bg-gray-100'
            }`}
          >
            {f === 'active' ? 'Active' : 'All'}
          </button>
        ))}
      </div>

      {/* Quick add form */}
      {showForm && <ForeshadowForm projectId={projectId} onCreated={() => { setShowForm(false); fetchForeshadows() }} />}

      {loading ? (
        <p className="text-xs text-gray-400">Loading...</p>
      ) : foreshadows.length === 0 ? (
        <p className="text-xs text-gray-400">No foreshadows yet.</p>
      ) : (
        <div className="space-y-3">
          {/* Ready - urgent */}
          {grouped.ready.length > 0 && (
            <Section title="Ready to Resolve" items={grouped.ready} />
          )}
          {/* Ripening */}
          {grouped.ripening.length > 0 && (
            <Section title="Ripening" items={grouped.ripening} />
          )}
          {/* Planted */}
          {grouped.planted.length > 0 && (
            <Section title="Planted" items={grouped.planted} />
          )}
          {/* Resolved */}
          {filter === 'all' && grouped.resolved.length > 0 && (
            <Section title="Resolved" items={grouped.resolved} />
          )}
        </div>
      )}
    </div>
  )
}

function Section({ title, items }: { title: string; items: Foreshadow[] }) {
  return (
    <div>
      <h4 className="text-xs font-medium text-gray-500 mb-1">{title}</h4>
      <div className="space-y-1.5">
        {items.map((f) => (
          <ForeshadowCard key={f.id} foreshadow={f} />
        ))}
      </div>
    </div>
  )
}

function ForeshadowCard({ foreshadow: f }: { foreshadow: Foreshadow }) {
  const statusCfg = STATUS_CONFIG[f.status]
  const proximityWidth = Math.round(f.narrativeProximity * 100)

  return (
    <div className="bg-white border border-gray-200 rounded-lg p-2.5 text-xs">
      <div className="flex items-center gap-1.5 mb-1">
        <span className={`px-1.5 py-0.5 rounded text-[10px] font-medium ${statusCfg.color}`}>
          {statusCfg.label}
        </span>
        <span className="text-gray-400">{TYPE_LABELS[f.type]}</span>
        <span className="text-gray-300 ml-auto">Ch.{f.plantedChapter}</span>
      </div>
      <p className="text-gray-700 leading-relaxed">{f.description}</p>
      {f.status !== 'resolved' && (
        <div className="mt-1.5">
          <div className="flex items-center gap-1.5">
            <div className="flex-1 h-1 bg-gray-100 rounded-full overflow-hidden">
              <div
                className={`h-full rounded-full ${
                  f.narrativeProximity > 0.9
                    ? 'bg-red-500'
                    : f.narrativeProximity > 0.7
                    ? 'bg-yellow-500'
                    : 'bg-blue-400'
                }`}
                style={{ width: `${proximityWidth}%` }}
              />
            </div>
            <span className="text-gray-400 w-8 text-right">{proximityWidth}%</span>
          </div>
        </div>
      )}
      {f.resolvedChapter && (
        <p className="text-gray-400 mt-1">Resolved at Ch.{f.resolvedChapter}</p>
      )}
    </div>
  )
}

function ForeshadowForm({ projectId, onCreated }: { projectId: string; onCreated: () => void }) {
  const [desc, setDesc] = useState('')
  const [type, setType] = useState<'major' | 'minor' | 'hint'>('minor')
  const [conditions, setConditions] = useState('')
  const [submitting, setSubmitting] = useState(false)

  const handleSubmit = async () => {
    if (!desc.trim()) return
    setSubmitting(true)
    try {
      await apiFetch(`/api/projects/${projectId}/foreshadows`, {
        method: 'POST',
        body: JSON.stringify({
          description: desc,
          type,
          planted_chapter: 0,
          resolve_conditions: conditions
            .split('\n')
            .map((c) => c.trim())
            .filter(Boolean),
        }),
      })
      onCreated()
    } catch {
      // ignore
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="bg-gray-50 rounded-lg p-3 space-y-2">
      <select
        value={type}
        onChange={(e) => setType(e.target.value as 'major' | 'minor' | 'hint')}
        className="w-full px-2 py-1 text-xs border border-gray-200 rounded"
      >
        <option value="major">Major</option>
        <option value="minor">Minor</option>
        <option value="hint">Hint</option>
      </select>
      <textarea
        value={desc}
        onChange={(e) => setDesc(e.target.value)}
        placeholder="Describe the foreshadow..."
        className="w-full px-2 py-1 text-xs border border-gray-200 rounded resize-none h-16"
      />
      <textarea
        value={conditions}
        onChange={(e) => setConditions(e.target.value)}
        placeholder="Resolve conditions (one per line)..."
        className="w-full px-2 py-1 text-xs border border-gray-200 rounded resize-none h-12"
      />
      <button
        onClick={handleSubmit}
        disabled={submitting || !desc.trim()}
        className="w-full px-2 py-1.5 text-xs bg-blue-600 text-white rounded hover:bg-blue-700 disabled:opacity-50"
      >
        {submitting ? 'Creating...' : 'Create Foreshadow'}
      </button>
    </div>
  )
}
