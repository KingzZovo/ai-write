import { describe, expect, it, vi } from 'vitest'
import fixture from './fixtures/arcPlanning.contract.json'
import { ApiError } from '@/lib/api'
import { ArcPlanningSession } from '@/components/arc/arcPlanningSession'

const pid = fixture.after.project_id, rid = fixture.input.request_id
const deferred = <T,>() => {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(done => { resolve = done })
  return { promise, resolve }
}

function harness() {
  let stored: string | null = null, writeFails = false
  let current: unknown = fixture.before
  let postError: Error | null = null, detailError: Error | null = null
  let nextStatus: Promise<unknown> | null = null
  const deps = {
    id: vi.fn(() => rid),
    storage: () => ({ getItem: () => stored, setItem: (_key: string, value: string) => {
      if (writeFails) throw new Error('quota exceeded')
      stored = value
    } }),
    request: vi.fn(async (path: string, options?: RequestInit): Promise<unknown> => {
      if (path.endsWith('/source')) return { source: fixture.input.expected_source, mode: 'start', target_volume_idx: 6, previous_material: null }
      if (options?.method === 'POST') {
        if (options.body) {
          expect(JSON.parse(stored!).pending.input).toEqual(JSON.parse(options.body as string))
          if (postError) throw postError
        }
        return path.endsWith('/arc-plans') ? fixture.pending : current
      }
      if (path.endsWith('/detail')) { if (detailError) throw detailError; return current }
      if (nextStatus) { const next = nextStatus; nextStatus = null; return next }
      return current
    }),
  }
  const session = new ArcPlanningSession(pid, deps)
  session.attach()
  const ready = async () => {
    for (const key of ['idea', 'background', 'core_setup', 'opening_scene'] as const) session.edit(key, fixture.input[key])
    session.edit('target_chapters', String(fixture.input.target_chapters))
    await session.reloadSource()
  }
  return { session, deps, ready, stored: () => stored,
    storageFails: (value: boolean) => { writeFails = value }, rawStorage: (value: string) => { stored = value },
    postFails: (value: Error | null) => { postError = value }, detailFails: (value: Error | null) => { detailError = value },
    current: (value: unknown) => { current = value }, nextStatus: (value: Promise<unknown>) => { nextStatus = value } }
}

describe('fixed original arc request session', () => {
  it('persists the entire request before POST and clears it only after matching durable detail', async () => {
    const h = harness(); await h.ready(); await h.session.submit()
    expect(h.session.getSnapshot().pending).toBeNull()
    expect(h.session.getSnapshot().detail?.request.background).toBe(fixture.input.background)
    expect(h.deps.id).toHaveBeenCalledTimes(1)
  })

  it('keeps the original UUID, material and source after an unknown result even if the author edits later', async () => {
    const h = harness(); await h.ready(); h.postFails(new ApiError('未确认', 503, 'arc_planning_unconfirmed'))
    await expect(h.session.submit()).rejects.toThrow()
    h.session.edit('background', '作者后来补写的背景。')
    h.postFails(null); await h.session.retry()
    const posts = h.deps.request.mock.calls.filter(([, options]) => options?.body)
    expect(posts.map(([, options]) => JSON.parse(options!.body as string))).toEqual([fixture.input, fixture.input])
    expect(h.session.getSnapshot().draft.background).toBe('作者后来补写的背景。')
    expect(h.deps.id).toHaveBeenCalledTimes(1)
  })

  it('does not treat a successful POST alone as original request confirmation', async () => {
    const h = harness(); await h.ready(); h.detailFails(new ApiError('详情暂不可读', 503))
    await expect(h.session.submit()).rejects.toThrow()
    expect(h.session.getSnapshot().pending?.input).toEqual(fixture.input)
    expect(JSON.parse(h.stored()!).pending.input).toEqual(fixture.input)
  })

  it('restores and checks the same full original request after refresh', async () => {
    const h = harness(); await h.ready(); h.postFails(new ApiError('未确认', 503))
    await expect(h.session.submit()).rejects.toThrow()
    const restored = new ArcPlanningSession(pid, h.deps)
    restored.attach(); await restored.reload(true)
    expect(restored.getSnapshot().selected).toBe(rid)
    expect(restored.getSnapshot().pending).toBeNull()
    expect(h.deps.id).toHaveBeenCalledTimes(1)
  })

  it('stops before POST if the original request cannot be stored', async () => {
    const h = harness(); await h.ready(); h.storageFails(true)
    await expect(h.session.submit()).rejects.toThrow()
    expect(h.deps.request.mock.calls.some(([, options]) => options?.method === 'POST')).toBe(false)
    expect(h.session.getSnapshot().pending?.input.request_id).toBe(rid)
  })

  it('preserves unreadable local data and does not create another UUID', async () => {
    const h = harness(); h.rawStorage('{broken original')
    const session = new ArcPlanningSession(pid, h.deps); session.attach()
    await expect(session.submit()).rejects.toThrow()
    expect(h.stored()).toBe('{broken original')
    expect(h.deps.id).not.toHaveBeenCalled()
  })

  it('allows explicit release of a rejected request while retaining every draft field', async () => {
    const h = harness(); await h.ready(); h.postFails(new ApiError('前弧已变化', 409, 'arc_planning_source_changed'))
    await expect(h.session.submit()).rejects.toThrow()
    const before = h.session.getSnapshot().draft
    expect(h.session.getSnapshot().pending?.rejected).toBe(true)
    h.session.releaseRejected()
    expect(h.session.getSnapshot().pending).toBeNull()
    expect(h.session.getSnapshot().source).toBeNull()
    expect(h.session.getSnapshot().draft).toEqual(before)
  })

  it('does not release a request whose result is still unknown', async () => {
    const h = harness(); await h.ready(); h.postFails(new ApiError('未确认', 503))
    await expect(h.session.submit()).rejects.toThrow()
    h.session.releaseRejected()
    expect(h.session.getSnapshot().pending?.input).toEqual(fixture.input)
  })

  it('does not downgrade a completed receipt when an old poll finishes later', async () => {
    const h = harness(); await h.ready(); await h.session.submit()
    const old = deferred<unknown>(); h.nextStatus(old.promise)
    const reading = h.session.reload()
    h.current(fixture.after); await h.session.command('resume')
    old.resolve(fixture.pending); await reading
    expect(h.session.getSnapshot().status?.status).toBe('completed')
    expect(h.session.getSnapshot().detail?.result?.volume_id).toBe(fixture.after.result.volume_id)
  })

  it('cannot clear pending input using a different source in a durable-looking response', async () => {
    const h = harness(); await h.ready()
    const different = structuredClone(fixture.before)
    different.request.expected_source.source_hash = 'f'.repeat(64)
    h.current(different)
    await expect(h.session.submit()).rejects.toThrow()
    expect(h.session.getSnapshot().pending?.input.expected_source).toEqual(fixture.input.expected_source)
  })

  it('does not replace a live original task with another newly numbered submission', async () => {
    const h = harness(); await h.ready(); await h.session.submit()
    await h.session.reloadSource()
    await expect(h.session.submit()).rejects.toThrow('当前弧规划尚未结束')
    expect(h.deps.id).toHaveBeenCalledTimes(1)
    expect(h.session.getSnapshot().selected).toBe(rid)
  })
})
