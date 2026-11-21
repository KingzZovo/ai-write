'use client'

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { apiFetch } from '@/lib/api'

interface Counts { characters_created: number; world_rules_created: number; relationships_created: number }
type State = { status: 'idle' | 'running' | 'recovering' | 'failed' | 'completed'; counts?: Counts }
const idle: State = { status: 'idle' }
function validCounts(counts: Counts | undefined): counts is Counts {
  return !!counts && [counts.characters_created, counts.world_rules_created, counts.relationships_created]
    .every(value => Number.isInteger(value) && value >= 0)
}

export function useSettingsExtraction(projectId?: string, outlineId?: string | null,
  source?: Record<string, unknown> | null) {
  const key = useMemo(() => JSON.stringify([projectId, outlineId, source]), [projectId, outlineId, source])
  const currentKey = useRef(key)
  currentKey.current = key
  const mounted = useRef(true)
  const pending = useRef<{ key: string; controller: AbortController } | null>(null)
  const [pollEpoch, setPollEpoch] = useState(0)
  const [result, setResult] = useState<{ key: string; state: State }>({ key, state: idle })

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
      pending.current?.controller.abort()
      pending.current = null
    }
  }, [])
  useEffect(() => {
    if (pending.current && pending.current.key !== key) {
      pending.current.controller.abort()
      pending.current = null
    }
  }, [key])

  useEffect(() => {
    if (!projectId || !outlineId) return
    let stopped = false
    let timer: ReturnType<typeof setTimeout> | undefined
    const controller = new AbortController()
    const poll = async () => {
      let again = false
      try {
        const data = await apiFetch<{ status: string; counts?: Counts }>(
          `/api/projects/${projectId}/outlines/${outlineId}/extract-settings`, { signal: controller.signal })
        if (stopped || currentKey.current !== key) return
        if (pending.current?.key === key) { again = true; return }
        if (data.status === 'completed' && validCounts(data.counts)) {
          setResult({ key, state: { status: 'completed', counts: data.counts } })
        } else if (data.status === 'pending' || data.status === 'failed') {
          setResult({ key, state: { status: 'recovering' } })
          again = true
        } else if (data.status !== 'not_started') {
          setResult({ key, state: { status: 'failed' } })
        }
      } catch {
        if (!stopped && currentKey.current === key && pending.current?.key !== key) {
          setResult(previous => previous.key === key && previous.state.status === 'completed'
            ? previous : { key, state: { status: 'failed' } })
          again = true
        }
      } finally {
        if (again && !stopped) timer = setTimeout(() => { void poll() }, 5000)
      }
    }
    void poll()
    return () => { stopped = true; controller.abort(); if (timer) clearTimeout(timer) }
  }, [key, projectId, outlineId, pollEpoch])

  const start = useCallback(async () => {
    if (!projectId || !outlineId || !mounted.current || pending.current?.key === key) return
    pending.current?.controller.abort()
    const request = { key, controller: new AbortController() }
    pending.current = request
    const active = () => mounted.current && currentKey.current === key && pending.current === request
    setResult({ key, state: { status: 'running' } })
    try {
      const counts = await apiFetch<Counts>(`/api/projects/${projectId}/outlines/${outlineId}/extract-settings`,
        { method: 'POST', signal: request.controller.signal })
      if (!active()) return
      if (!validCounts(counts)) throw new Error('Invalid extraction response')
      setResult({ key, state: { status: 'completed', counts } })
    } catch {
      if (active()) setResult({ key, state: { status: 'failed' } })
    } finally {
      if (pending.current === request) {
        pending.current = null
        if (mounted.current && currentKey.current === key) setPollEpoch(value => value + 1)
      }
    }
  }, [projectId, outlineId, key])

  return { state: result.key === key ? result.state : idle, start }
}

export function SettingsExtractionStatus({ state, onStart }: { state: State; onStart: () => void }) {
  const text = state.status === 'running' ? '书纲已确认，正在提取人物、世界规则与人物关系…'
    : state.status === 'recovering' ? '提取结果已保存，正在等待同步或自动重试。'
    : state.status === 'failed' ? '设定提取状态未获确认。已确认的书纲仍保留，请核对后重试。'
    : state.status === 'completed' && state.counts
      ? `设定提取已完成：新增 ${state.counts.characters_created} 个人物、${state.counts.world_rules_created} 条世界规则、${state.counts.relationships_created} 条关系。`
      : '可从当前已确认书纲提取人物、世界规则与人物关系。'
  return <section aria-label="书纲设定提取" className="mx-6 my-3 flex items-center justify-between gap-4 rounded-lg border border-gray-200 bg-gray-50 px-4 py-3">
    <p role={state.status === 'failed' ? 'alert' : 'status'} className={`text-sm ${state.status === 'failed' ? 'text-red-700' : 'text-gray-700'}`}>{text}</p>
    <button type="button" onClick={onStart} disabled={state.status === 'running'}
      className="shrink-0 rounded-lg border border-gray-300 px-3 py-1.5 text-sm text-gray-700 disabled:opacity-50">
      {state.status === 'running' ? '提取中…' : state.status === 'recovering' ? '重试同步' : state.status === 'failed' ? '重试提取' : state.status === 'completed' ? '核对结果' : '提取设定'}
    </button>
  </section>
}
