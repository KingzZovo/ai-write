'use client'

import React, { useState, useEffect, useCallback } from 'react'
import { apiFetch } from '@/lib/api'

interface StyleProfile {
  id: string
  name: string
  description: string | null
  sentenceRatios: Record<string, number>
  dialogueRatio: number
  topWords: string[]
}

interface StylePanelProps {
  value?: string
  onChange?: (styleDescription: string) => void
}

export function StylePanel({ value, onChange }: StylePanelProps) {
  const [mode, setMode] = useState<'profile' | 'manual'>('profile')
  const [profiles, setProfiles] = useState<StyleProfile[]>([])
  const [selectedProfileId, setSelectedProfileId] = useState<string>('')
  const [manualDescription, setManualDescription] = useState(value ?? '')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    apiFetch<StyleProfile[]>('/api/style/profiles')
      .then((data) => {
        if (!cancelled) {
          setProfiles(data)
          if (data.length > 0 && !selectedProfileId) {
            setSelectedProfileId(data[0].id)
          }
          setLoading(false)
        }
      })
      .catch((err) => {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : 'Failed to load style profiles')
          setLoading(false)
        }
      })
    return () => {
      cancelled = true
    }
    // Only run on mount
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const selectedProfile = profiles.find((p) => p.id === selectedProfileId) ?? null

  const handleModeToggle = useCallback(
    (newMode: 'profile' | 'manual') => {
      setMode(newMode)
      if (newMode === 'manual') {
        onChange?.(manualDescription)
      } else if (selectedProfile) {
        onChange?.(selectedProfile.description ?? '')
      }
    },
    [manualDescription, selectedProfile, onChange]
  )

  const handleProfileChange = useCallback(
    (profileId: string) => {
      setSelectedProfileId(profileId)
      const profile = profiles.find((p) => p.id === profileId)
      if (profile && mode === 'profile') {
        onChange?.(profile.description ?? '')
      }
    },
    [profiles, mode, onChange]
  )

  const handleManualChange = useCallback(
    (text: string) => {
      setManualDescription(text)
      if (mode === 'manual') {
        onChange?.(text)
      }
    },
    [mode, onChange]
  )

  return (
    <div className="p-4 space-y-5">
      <div>
        <h3 className="text-sm font-semibold text-gray-900 mb-3">写作风格配置</h3>

        {/* Mode toggle */}
        <div className="flex rounded-lg bg-gray-100 p-0.5 mb-4">
          <button
            onClick={() => handleModeToggle('profile')}
            className={`flex-1 px-3 py-1.5 text-xs font-medium rounded-md transition-colors ${
              mode === 'profile'
                ? 'bg-white text-gray-900 shadow-sm'
                : 'text-gray-500 hover:text-gray-700'
            }`}
          >
            Style Profile
          </button>
          <button
            onClick={() => handleModeToggle('manual')}
            className={`flex-1 px-3 py-1.5 text-xs font-medium rounded-md transition-colors ${
              mode === 'manual'
                ? 'bg-white text-gray-900 shadow-sm'
                : 'text-gray-500 hover:text-gray-700'
            }`}
          >
            Manual
          </button>
        </div>
      </div>

      {mode === 'profile' && (
        <div className="space-y-4">
          {/* Profile selector */}
          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">
              Select Profile
            </label>
            {loading ? (
              <p className="text-xs text-gray-400">加载写法...</p>
            ) : error ? (
              <p className="text-xs text-red-500">{error}</p>
            ) : profiles.length === 0 ? (
              <p className="text-xs text-gray-400">暂无写法档案。</p>
            ) : (
              <select
                value={selectedProfileId}
                onChange={(e) => handleProfileChange(e.target.value)}
                className="w-full px-3 py-1.5 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent"
              >
                {profiles.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                  </option>
                ))}
              </select>
            )}
          </div>

          {/* Profile preview */}
          {selectedProfile && (
            <div className="space-y-3">
              {selectedProfile.description && (
                <div>
                  <label className="block text-xs font-medium text-gray-600 mb-1">
                    Description
                  </label>
                  <p className="text-xs text-gray-700 bg-gray-50 rounded-lg p-2.5 leading-relaxed">
                    {selectedProfile.description}
                  </p>
                </div>
              )}

              {/* Sentence ratios */}
              {Object.keys(selectedProfile.sentenceRatios).length > 0 && (
                <div>
                  <label className="block text-xs font-medium text-gray-600 mb-1.5">
                    Sentence Length Ratios
                  </label>
                  <div className="space-y-1.5">
                    {Object.entries(selectedProfile.sentenceRatios).map(
                      ([label, ratio]) => (
                        <div key={label} className="flex items-center gap-2">
                          <span className="text-xs text-gray-500 w-16 shrink-0 truncate">
                            {label}
                          </span>
                          <div className="flex-1 bg-gray-100 rounded-full h-1.5">
                            <div
                              className="bg-blue-500 h-1.5 rounded-full"
                              style={{ width: `${Math.min(ratio * 100, 100)}%` }}
                            />
                          </div>
                          <span className="text-xs text-gray-400 w-10 text-right">
                            {(ratio * 100).toFixed(0)}%
                          </span>
                        </div>
                      )
                    )}
                  </div>
                </div>
              )}

              {/* Dialogue ratio */}
              <div>
                <label className="block text-xs font-medium text-gray-600 mb-1">
                  Dialogue Ratio
                </label>
                <div className="flex items-center gap-2">
                  <div className="flex-1 bg-gray-100 rounded-full h-1.5">
                    <div
                      className="bg-purple-500 h-1.5 rounded-full"
                      style={{
                        width: `${Math.min(selectedProfile.dialogueRatio * 100, 100)}%`,
                      }}
                    />
                  </div>
                  <span className="text-xs text-gray-400 w-10 text-right">
                    {(selectedProfile.dialogueRatio * 100).toFixed(0)}%
                  </span>
                </div>
              </div>

              {/* Top words */}
              {selectedProfile.topWords.length > 0 && (
                <div>
                  <label className="block text-xs font-medium text-gray-600 mb-1.5">
                    Top Words
                  </label>
                  <div className="flex flex-wrap gap-1.5">
                    {selectedProfile.topWords.map((word, idx) => (
                      <span
                        key={idx}
                        className="inline-block px-2 py-0.5 bg-blue-50 text-blue-700 text-xs rounded"
                      >
                        {word}
                      </span>
                    ))}
                  </div>
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {mode === 'manual' && (
        <div>
          <label className="block text-xs font-medium text-gray-600 mb-1">
            Style Description
          </label>
          <textarea
            value={manualDescription}
            onChange={(e) => handleManualChange(e.target.value)}
            placeholder="描述目标写作风格…例如：简洁叙事、多对话、短句、文学调性"
            className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg resize-none h-32 focus:ring-2 focus:ring-blue-500 focus:border-transparent"
          />
          <p className="text-xs text-gray-400 mt-1">
            This description will be used as-is for style guidance during generation.
          </p>
        </div>
      )}
    </div>
  )
}
