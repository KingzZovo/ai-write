'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import { apiFetch } from '@/lib/api'

export interface PipelineChapter {
  chapter_id: string
  volume_idx: number | null
  chapter_idx: number
  state: string
  word_count: number
  error_message: string | null
}
export interface BookPipeline {
  id: string
  project_id: string
  volume_id: string | null
  state: string
  total_chapters: number
  completed_chapters: number
  needs_review_chapters: number
  current_chapter_id: string | null
  error_message: string | null
  created_at: string | null
  heartbeat_at: string | null
  can_rollback: boolean
  chapters: PipelineChapter[]
}
export interface PipelineVolume { id: string; title: string; volume_idx: number }
export interface PipelineOptions { target_words?: number; user_input: string; max_attempts: number }
export const pipelineActive = (state?: string) => ['queued', 'generating', 'pausing'].includes(state ?? '')

export function useBookPipeline(projectId: string) {
  const [runs, setRuns] = useState<BookPipeline[]>([])
  const [volumes, setVolumes] = useState<PipelineVolume[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const busyRef = useRef(false)
  const revision = useRef(0)
  const alive = useRef(false)
  const run = runs.find(item => item.id === selectedId) ?? null

  const accept = useCallback((next: BookPipeline) => {
    setRuns(previous => [next, ...previous.filter(item => item.id !== next.id)])
  }, [])

  const refresh = useCallback(async (signal?: AbortSignal) => {
    const current = ++revision.current
    try {
      const [history, availableVolumes] = await Promise.all([
        apiFetch<BookPipeline[]>(`/api/pipeline/project/${projectId}?limit=20`, { signal }),
        apiFetch<PipelineVolume[]>(`/api/projects/${projectId}/volumes`, { signal }),
      ])
      if (!alive.current || signal?.aborted || current !== revision.current) return
      setRuns(history)
      setVolumes(availableVolumes)
      setSelectedId(previous => history.some(item => item.id === previous) ? previous : history[0]?.id ?? null)
      setError('')
    } catch (err) {
      if (alive.current && !signal?.aborted && current === revision.current) setError(err instanceof Error ? err.message : '加载整书任务失败')
    } finally {
      if (alive.current && current === revision.current) setLoading(false)
    }
  }, [projectId])

  const invalidate = useCallback(() => { revision.current++ }, [])

  useEffect(() => {
    alive.current = true
    const controller = new AbortController()
    void refresh(controller.signal)
    return () => { alive.current = false; invalidate(); controller.abort() }
  }, [refresh, invalidate])

  const active = pipelineActive(run?.state)
  useEffect(() => {
    if (!active || !selectedId) return
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout>
    let stopped = false
    const poll = async () => {
      const current = revision.current
      try {
        if (!busyRef.current && document.visibilityState !== 'hidden') {
          const next = await apiFetch<BookPipeline>(`/api/pipeline/${selectedId}`, { signal: controller.signal })
          if (!stopped && current === revision.current) { accept(next); setError('') }
        }
      } catch (err) {
        if (!stopped && current === revision.current) setError(err instanceof Error ? err.message : '进度更新失败，将自动重试')
      } finally {
        if (!stopped) timer = setTimeout(poll, 3000)
      }
    }
    timer = setTimeout(poll, 3000)
    return () => { stopped = true; clearTimeout(timer); controller.abort() }
  }, [active, selectedId, accept])

  const mutate = async (path: string, body?: unknown) => {
    if (busyRef.current) return false
    busyRef.current = true
    const current = ++revision.current
    setBusy(true)
    setError('')
    try {
      const next = await apiFetch<BookPipeline>(path, { method: 'POST', ...(body ? { body: JSON.stringify(body) } : {}) })
      if (alive.current && current === revision.current) { accept(next); setSelectedId(next.id) }
      return true
    } catch (err) {
      if (alive.current && current === revision.current) setError(err instanceof Error ? err.message : '操作失败，请刷新后重试')
      return false
    } finally {
      busyRef.current = false
      if (alive.current) setBusy(false)
    }
  }

  return { runs, volumes, run, loading, busy, error, refresh, select: setSelectedId,
    create: (volumeId: string, options: PipelineOptions) => mutate('/api/pipeline', {
      project_id: projectId, volume_id: volumeId || null, options,
    }),
    command: (command: 'start' | 'pause' | 'rollback') => selectedId
      ? mutate(`/api/pipeline/${selectedId}/${command}`) : Promise.resolve(false),
  }
}
