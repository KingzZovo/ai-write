import { apiFetch, ApiError } from '@/lib/api'
import { arcPlanPath, parseArcPlanDetail, parseArcPlanInput, parseArcPlanSource, parseArcPlanStatus, sameArcPlanInput,
  type ArcPlanDetail, type ArcPlanInput, type ArcPlanSource, type ArcPlanStatus } from '@/lib/arcPlanning'

type Draft = { idea: string; background: string; core_setup: string; opening_scene: string; target_chapters: string }
type Pending = { input: ArcPlanInput; rejected: boolean }
export type ArcPlanningState = {
  draft: Draft; pending: Pending | null; selected: string | null; source: ArcPlanSource | null
  status: ArcPlanStatus | null; detail: ArcPlanDetail | null; initialized: boolean; busy: boolean; loading: boolean
  sourceLoading: boolean; error: string | null; storageError: string | null; unreadable: string | null
}
type Dependencies = {
  request: (path: string, options?: RequestInit) => Promise<unknown>
  storage: () => Pick<Storage, 'getItem' | 'setItem'>; id: () => string
}
const rejectedCodes = new Set(['arc_planning_precondition_required', 'arc_planning_request_invalid', 'arc_planning_source_changed',
  'arc_planning_active', 'arc_planning_busy', 'arc_planning_source_invalid', 'arc_planning_project_unavailable',
  'arc_planning_previous_requires_review', 'arc_planning_request_conflict', 'arc_planning_book_material_invalid'])
const initialDraft = (): Draft => ({ idea: '', background: '', core_setup: '', opening_scene: '', target_chapters: '20' })

/** A browser refresh retains the exact submitted request, never a new UUID. */
export class ArcPlanningSession {
  private state: ArcPlanningState = { draft: initialDraft(), pending: null, selected: null, source: null, status: null,
    detail: null, initialized: false, busy: false, loading: false, sourceLoading: false, error: null, storageError: null, unreadable: null }
  private listeners = new Set<() => void>()
  private readVersion = 0
  private readController: AbortController | null = null
  private sourceVersion = 0
  private sourceController: AbortController | null = null
  private key: string
  constructor(readonly projectId: string, private deps: Dependencies = {
    request: apiFetch, storage: () => sessionStorage, id: () => crypto.randomUUID(),
  }) { this.key = `aiwrite:arc-planning:v1:${encodeURIComponent(projectId)}` }
  getSnapshot = () => this.state
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener) } }
  private publish(patch: Partial<ArcPlanningState>) {
    this.state = { ...this.state, ...patch }; for (const listener of this.listeners) listener()
  }
  private persist(): boolean {
    if (this.state.unreadable !== null) return false
    try {
      const { draft, pending, selected } = this.state
      this.deps.storage().setItem(this.key, JSON.stringify({ schema: 1, draft, pending, selected }))
      this.publish({ storageError: null }); return true
    } catch {
      this.publish({ storageError: '原材料暂存失败，请保留本页或下载原请求，恢复暂存后再提交。' }); return false
    }
  }
  attach = () => {
    if (!this.state.initialized) {
      let raw: string | null = null
      try {
        raw = this.deps.storage().getItem(this.key)
        if (raw !== null) {
          const saved = JSON.parse(raw)
          if (!saved || saved.schema !== 1 || !saved.draft || Object.keys(initialDraft()).some(key => typeof saved.draft[key] !== 'string') ||
              (saved.selected !== null && (typeof saved.selected !== 'string' || !/^[0-9a-f-]{36}$/.test(saved.selected)))) throw new Error('invalid draft')
          if (saved.pending !== null) {
            if (!saved.pending || typeof saved.pending.rejected !== 'boolean') throw new Error('invalid pending')
            parseArcPlanInput(saved.pending.input, this.projectId)
            if (saved.selected !== saved.pending.input.request_id) throw new Error('wrong pending selection')
          }
          this.publish({ draft: saved.draft, pending: saved.pending, selected: saved.selected })
        }
      } catch {
        this.publish({ unreadable: raw ?? '', storageError: '无法核对暂存的原规划请求，请先下载原记录；不会覆盖它或另建编号。' })
      }
      this.publish({ initialized: true })
    }
    if (this.state.selected && this.state.unreadable === null) void this.reload(true)
  }
  edit = (field: keyof Draft, value: string) => {
    this.publish({ draft: { ...this.state.draft, [field]: value } }); this.persist()
  }
  recoverStorage = () => {
    if (this.state.busy) return
    if (this.state.unreadable !== null) { this.publish({ initialized: false, unreadable: null, storageError: null }); this.attach() }
    else this.persist()
  }
  private async request(path: string, options: RequestInit = {}) {
    const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 30_000)
    try { return await this.deps.request(path, { ...options, signal: options.signal ?? controller.signal }) }
    finally { clearTimeout(timer) }
  }
  reloadSource = async () => {
    this.sourceController?.abort()
    const controller = new AbortController(), version = ++this.sourceVersion
    this.sourceController = controller
    const timer = setTimeout(() => controller.abort(), 20_000)
    this.publish({ sourceLoading: true, error: null })
    try {
      const source = parseArcPlanSource(await this.deps.request(`${arcPlanPath(this.projectId)}/source`, { signal: controller.signal }), this.projectId)
      if (version === this.sourceVersion) this.publish({ source })
    } catch (cause) {
      if (version === this.sourceVersion) this.publish({ source: null, error: cause instanceof Error ? cause.message : '当前来源读取失败。' })
    } finally { clearTimeout(timer); if (version === this.sourceVersion) this.publish({ sourceLoading: false }) }
  }
  private mergeStatus(status: ArcPlanStatus): boolean {
    if (this.state.selected !== status.request_id || this.state.status && this.state.status.revision > status.revision) return false
    this.publish({ status }); return true
  }
  private acceptDetail(detail: ArcPlanDetail) {
    const pending = this.state.pending
    if (pending && pending.input.request_id === detail.request_id && !sameArcPlanInput(detail, pending.input)) {
      throw new Error('原请求内容或来源未能核对，输入仍保留。')
    }
    if (!this.mergeStatus(detail)) return
    this.publish({ detail })
    if (pending && pending.input.request_id === detail.request_id) {
      this.publish({ pending: null, source: null })
      if (!this.persist()) this.publish({ pending })
    }
  }
  reload = async (full = false) => {
    const identity = this.state.pending?.input.request_id ?? this.state.selected
    if (!identity) return
    this.readController?.abort()
    const controller = new AbortController(), version = ++this.readVersion
    this.readController = controller
    const timer = setTimeout(() => controller.abort(), 20_000)
    this.publish({ loading: true, error: null })
    try {
      const prefix = `${arcPlanPath(this.projectId)}/${encodeURIComponent(identity)}`
      if (full || this.state.pending) {
        const detail = parseArcPlanDetail(await this.deps.request(prefix + '/detail', { signal: controller.signal }), this.projectId, identity)
        if (version === this.readVersion) this.acceptDetail(detail)
      } else {
        const status = parseArcPlanStatus(await this.deps.request(prefix, { signal: controller.signal }), this.projectId, identity)
        if (version !== this.readVersion || !this.mergeStatus(status)) return
        if (['completed', 'needs_review', 'cancelled'].includes(status.status) && this.state.detail?.revision !== status.revision) {
          const detail = parseArcPlanDetail(await this.deps.request(prefix + '/detail', { signal: controller.signal }), this.projectId, identity)
          if (version === this.readVersion) this.acceptDetail(detail)
        }
      }
    } catch (cause) {
      if (version === this.readVersion) this.publish({ error: cause instanceof Error ? cause.message : '原任务读取失败，输入仍保留。' })
    } finally { clearTimeout(timer); if (version === this.readVersion) this.publish({ loading: false }) }
  }
  select = async (identity: string) => {
    if (this.state.pending || this.state.busy) return
    this.readVersion++; this.readController?.abort()
    this.publish({ selected: identity, status: null, detail: null, error: null, loading: false }); this.persist()
    await this.reload(true)
  }
  submit = async () => {
    if (this.state.pending) return this.retry()
    if (this.state.busy || this.state.storageError || this.state.unreadable !== null) throw new Error('请先核对原请求暂存。')
    if (this.state.status && ['pending', 'running'].includes(this.state.status.status)) throw new Error('当前弧规划尚未结束，请先查看、恢复或停止原任务。')
    const source = this.state.source
    if (!source) throw new Error('请先读取当前分卷与前弧来源。')
    const draft = this.state.draft
    const input = parseArcPlanInput({ ...draft, target_chapters: Number(draft.target_chapters), request_id: this.deps.id(),
      expected_source: structuredClone(source.source), mode: source.mode }, this.projectId)
    this.readVersion++; this.readController?.abort()
    this.publish({ pending: { input, rejected: false }, selected: input.request_id, status: null, detail: null, error: null, loading: false })
    if (!this.persist()) throw new Error('原请求未能暂存，尚未提交。')
    return this.retry()
  }
  retry = async () => {
    const pending = this.state.pending
    if (!pending || this.state.busy || this.state.unreadable !== null) return
    if (!this.persist()) throw new Error('原请求未能暂存，尚未重试。')
    this.publish({ busy: true, error: null })
    try {
      const raw = await this.request(arcPlanPath(this.projectId), { method: 'POST', body: JSON.stringify(pending.input) })
      const status = parseArcPlanStatus(raw, this.projectId, pending.input.request_id)
      this.mergeStatus(status)
      const detail = parseArcPlanDetail(await this.request(`${arcPlanPath(this.projectId)}/${pending.input.request_id}/detail`), this.projectId, pending.input.request_id)
      this.acceptDetail(detail)
    } catch (cause) {
      if (cause instanceof ApiError && [404, 409, 422, 428].includes(cause.status) && rejectedCodes.has(cause.code ?? '')) {
        this.publish({ pending: { ...pending, rejected: true } }); this.persist()
      }
      this.publish({ error: cause instanceof Error ? cause.message : '提交结果尚未确认，原请求已保留。' })
      throw cause
    } finally { this.publish({ busy: false }) }
  }
  releaseRejected = () => {
    if (!this.state.pending?.rejected || this.state.busy) return
    this.publish({ pending: null, selected: null, source: null, status: null, detail: null, error: null }); this.persist()
  }
  command = async (action: 'resume' | 'cancel') => {
    const identity = this.state.selected
    if (!identity || this.state.busy || this.state.pending || this.state.storageError || this.state.unreadable !== null) return
    this.publish({ busy: true, error: null })
    try {
      const status = parseArcPlanStatus(await this.request(`${arcPlanPath(this.projectId)}/${encodeURIComponent(identity)}/${action}`, { method: 'POST' }), this.projectId, identity)
      this.mergeStatus(status)
      const detail = parseArcPlanDetail(await this.request(`${arcPlanPath(this.projectId)}/${identity}/detail`), this.projectId, identity)
      this.acceptDetail(detail)
    } catch (cause) { this.publish({ error: cause instanceof Error ? cause.message : '操作结果尚未确认，请刷新原任务。' }) }
    finally { this.publish({ busy: false }) }
  }
}

const sessions = new Map<string, ArcPlanningSession>()
export function getArcPlanningSession(projectId: string) {
  if (typeof window === 'undefined') return new ArcPlanningSession(projectId)
  let session = sessions.get(projectId)
  if (!session) { session = new ArcPlanningSession(projectId); sessions.set(projectId, session) }
  return session
}
