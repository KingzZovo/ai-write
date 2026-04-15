'use client'

import React, { useState, useEffect, useCallback, useRef } from 'react'
import { apiFetch } from '@/lib/api'

interface TokenStats {
  totalInputTokens: number
  totalOutputTokens: number
  totalTokens: number
  cacheHits: number
  cacheMisses: number
  cacheHitRate: number
}

export function TokenDashboard() {
  const [stats, setStats] = useState<TokenStats | null>(null)
  const [loading, setLoading] = useState(false)
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const fetchStats = useCallback(async () => {
    try {
      const data = await apiFetch<TokenStats>('/api/stats/tokens')
      setStats(data)
    } catch {
      // ignore - stats endpoint may not be available
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    setLoading(true)
    fetchStats()

    intervalRef.current = setInterval(fetchStats, 30000)
    return () => {
      if (intervalRef.current) {
        clearInterval(intervalRef.current)
      }
    }
  }, [fetchStats])

  const formatNumber = (n: number): string => {
    return n.toLocaleString()
  }

  return (
    <div className="space-y-2">
      <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wide">Token Usage</h3>

      {loading && !stats ? (
        <p className="text-xs text-gray-400">Loading...</p>
      ) : stats ? (
        <div className="space-y-2">
          {/* Token counts */}
          <div className="grid grid-cols-3 gap-1.5">
            <div className="bg-gray-50 rounded p-1.5 text-center">
              <div className="text-xs font-medium text-gray-800">
                {formatNumber(stats.totalInputTokens)}
              </div>
              <div className="text-[10px] text-gray-400">Input</div>
            </div>
            <div className="bg-gray-50 rounded p-1.5 text-center">
              <div className="text-xs font-medium text-gray-800">
                {formatNumber(stats.totalOutputTokens)}
              </div>
              <div className="text-[10px] text-gray-400">Output</div>
            </div>
            <div className="bg-gray-50 rounded p-1.5 text-center">
              <div className="text-xs font-medium text-gray-800">
                {formatNumber(stats.totalTokens)}
              </div>
              <div className="text-[10px] text-gray-400">Total</div>
            </div>
          </div>

          {/* Cache hit rate */}
          <div>
            <div className="flex items-center justify-between mb-0.5">
              <span className="text-[10px] text-gray-500">Cache Hit Rate</span>
              <span className="text-[10px] font-medium text-gray-700">
                {(stats.cacheHitRate * 100).toFixed(1)}%
              </span>
            </div>
            <div className="w-full h-1.5 bg-gray-100 rounded-full overflow-hidden">
              <div
                className="h-full rounded-full bg-blue-500 transition-all"
                style={{ width: `${Math.min(stats.cacheHitRate * 100, 100)}%` }}
              />
            </div>
            <div className="flex justify-between mt-0.5">
              <span className="text-[10px] text-gray-400">
                Hits: {formatNumber(stats.cacheHits)}
              </span>
              <span className="text-[10px] text-gray-400">
                Misses: {formatNumber(stats.cacheMisses)}
              </span>
            </div>
          </div>
        </div>
      ) : (
        <p className="text-xs text-gray-400">No token data available.</p>
      )}
    </div>
  )
}
