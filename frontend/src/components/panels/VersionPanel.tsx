'use client'

import React, { useState, useEffect, useCallback } from 'react'
import { apiFetch } from '@/lib/api'

interface Version {
  id: string
  branchName: string
  parentId: string | null
  wordCount: number
  source: 'ai' | 'user'
  isActive: boolean
  createdAt: string
}

interface DiffLine {
  type: 'added' | 'removed' | 'unchanged'
  content: string
}

interface DiffResult {
  lines: DiffLine[]
}

interface VersionPanelProps {
  chapterId: string
}

export function VersionPanel({ chapterId }: VersionPanelProps) {
  const [versions, setVersions] = useState<Version[]>([])
  const [loading, setLoading] = useState(false)
  const [selectedVersionId, setSelectedVersionId] = useState<string | null>(null)
  const [diff, setDiff] = useState<DiffResult | null>(null)
  const [diffLoading, setDiffLoading] = useState(false)
  const [showBranchForm, setShowBranchForm] = useState(false)
  const [branchName, setBranchName] = useState('')
  const [creating, setCreating] = useState(false)
  const [switching, setSwitching] = useState(false)

  const fetchVersions = useCallback(async () => {
    if (!chapterId) return
    setLoading(true)
    try {
      const data = await apiFetch<Version[]>(
        `/api/chapters/${chapterId}/versions`
      )
      setVersions(data)
    } catch {
      // ignore
    } finally {
      setLoading(false)
    }
  }, [chapterId])

  useEffect(() => {
    fetchVersions()
    setSelectedVersionId(null)
    setDiff(null)
  }, [fetchVersions])

  const activeVersion = versions.find((v) => v.isActive)

  const handleSelectVersion = useCallback(
    async (versionId: string) => {
      if (selectedVersionId === versionId) {
        setSelectedVersionId(null)
        setDiff(null)
        return
      }
      setSelectedVersionId(versionId)
      if (!activeVersion || activeVersion.id === versionId) {
        setDiff(null)
        return
      }
      setDiffLoading(true)
      try {
        const data = await apiFetch<DiffResult>(
          `/api/chapters/${chapterId}/versions/diff?a=${activeVersion.id}&b=${versionId}`
        )
        setDiff(data)
      } catch {
        setDiff(null)
      } finally {
        setDiffLoading(false)
      }
    },
    [chapterId, activeVersion, selectedVersionId]
  )

  const handleCreateBranch = async () => {
    if (!branchName.trim() || creating) return
    setCreating(true)
    try {
      await apiFetch(`/api/chapters/${chapterId}/versions`, {
        method: 'POST',
        body: JSON.stringify({ branch_name: branchName.trim() }),
      })
      setBranchName('')
      setShowBranchForm(false)
      await fetchVersions()
    } catch {
      // ignore
    } finally {
      setCreating(false)
    }
  }

  const handleSwitchVersion = async (versionId: string) => {
    if (switching) return
    setSwitching(true)
    try {
      await apiFetch(`/api/chapters/${chapterId}/versions/${versionId}/activate`, {
        method: 'POST',
      })
      await fetchVersions()
      setSelectedVersionId(null)
      setDiff(null)
    } catch {
      // ignore
    } finally {
      setSwitching(false)
    }
  }

  const formatTime = (ts: string) => {
    try {
      const d = new Date(ts)
      return d.toLocaleDateString(undefined, {
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
      })
    } catch {
      return ts
    }
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold text-gray-900">版本历史</h3>
        <button
          onClick={() => setShowBranchForm(!showBranchForm)}
          className="text-xs text-blue-600 hover:text-blue-700"
        >
          + 分支
        </button>
      </div>

      {showBranchForm && (
        <div className="bg-gray-50 rounded-lg p-2.5 space-y-2">
          <input
            value={branchName}
            onChange={(e) => setBranchName(e.target.value)}
            placeholder="分支名称..."
            className="w-full px-2 py-1 text-xs border border-gray-200 rounded"
          />
          <div className="flex gap-1.5">
            <button
              onClick={handleCreateBranch}
              disabled={creating || !branchName.trim()}
              className="flex-1 px-2 py-1 text-xs bg-blue-600 text-white rounded hover:bg-blue-700 disabled:opacity-50"
            >
              {creating ? '创建中...' : '创建分支'}
            </button>
            <button
              onClick={() => {
                setShowBranchForm(false)
                setBranchName('')
              }}
              className="flex-1 px-2 py-1 text-xs bg-gray-200 text-gray-600 rounded"
            >
              取消
            </button>
          </div>
        </div>
      )}

      {loading ? (
        <p className="text-xs text-gray-400">加载版本中...</p>
      ) : versions.length === 0 ? (
        <p className="text-xs text-gray-400">暂无版本记录。</p>
      ) : (
        <div className="space-y-1.5">
          {versions.map((v) => (
            <div
              key={v.id}
              onClick={() => handleSelectVersion(v.id)}
              className={`bg-white border rounded-lg p-2.5 text-xs cursor-pointer transition-colors ${
                v.isActive
                  ? 'border-blue-400 bg-blue-50'
                  : selectedVersionId === v.id
                  ? 'border-blue-300 bg-blue-50/50'
                  : 'border-gray-200 hover:border-gray-300'
              }`}
            >
              <div className="flex items-center justify-between mb-1">
                <div className="flex items-center gap-1.5">
                  <span className="font-medium text-gray-800 truncate max-w-[120px]">
                    {v.branchName}
                  </span>
                  {v.isActive && (
                    <span className="px-1 py-0.5 rounded text-[10px] font-medium bg-blue-100 text-blue-700">
                      当前
                    </span>
                  )}
                </div>
                <span
                  className={`px-1 py-0.5 rounded text-[10px] font-medium ${
                    v.source === 'ai'
                      ? 'bg-purple-100 text-purple-700'
                      : 'bg-green-100 text-green-700'
                  }`}
                >
                  {v.source === 'ai' ? 'AI' : '用户'}
                </span>
              </div>
              <div className="flex items-center justify-between text-gray-400">
                <span>{(v.wordCount || 0).toLocaleString()} 字</span>
                <span>{formatTime(v.createdAt)}</span>
              </div>
              {selectedVersionId === v.id && !v.isActive && (
                <button
                  onClick={(e) => {
                    e.stopPropagation()
                    handleSwitchVersion(v.id)
                  }}
                  disabled={switching}
                  className="mt-2 w-full px-2 py-1 text-xs bg-blue-600 text-white rounded hover:bg-blue-700 disabled:opacity-50"
                >
                  {switching ? '切换中...' : '切换到此版本'}
                </button>
              )}
            </div>
          ))}
        </div>
      )}

      {/* Diff view */}
      {selectedVersionId && activeVersion && selectedVersionId !== activeVersion.id && (
        <div className="mt-3">
          <h4 className="text-xs font-medium text-gray-600 mb-1.5">
            差异: {activeVersion.branchName} 与{' '}
            {versions.find((v) => v.id === selectedVersionId)?.branchName}
          </h4>
          {diffLoading ? (
            <p className="text-xs text-gray-400">加载差异中...</p>
          ) : diff && diff.lines.length > 0 ? (
            <div className="bg-gray-50 rounded-lg border border-gray-200 overflow-hidden max-h-60 overflow-y-auto">
              {diff.lines.map((line, idx) => (
                <div
                  key={idx}
                  className={`px-2 py-0.5 text-xs font-mono whitespace-pre-wrap ${
                    line.type === 'added'
                      ? 'bg-green-50 text-green-800'
                      : line.type === 'removed'
                      ? 'bg-red-50 text-red-800'
                      : 'text-gray-600'
                  }`}
                >
                  <span className="select-none mr-1.5 text-gray-400">
                    {line.type === 'added' ? '+' : line.type === 'removed' ? '-' : ' '}
                  </span>
                  {line.content}
                </div>
              ))}
            </div>
          ) : (
            <p className="text-xs text-gray-400">未发现差异。</p>
          )}
        </div>
      )}
    </div>
  )
}
