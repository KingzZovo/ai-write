import { ApiError, apiFetch } from '@/lib/api'
import { requestId } from '@/lib/requestId'
import type { ManuscriptSource } from '@/lib/chapterManuscripts'
import { arcProgressPath, confirmArcReceipt, parseArcProgress, parseArcWrite,
  type ArcAction, type ArcProgressReceipt, type ArcProgressView, type ArcProgressWrite } from '@/lib/arcProgress'
import { sameJson } from '@/lib/volumeOutlines'

type Pending = { body: string; sent: boolean; rejected: boolean }
export type ArcProgressState = {
  view: ArcProgressView | null; notes: Record<string, string>; direction: string; closingNote: string
  pending: Pending | null; receipt: ArcProgressReceipt | null; initialized: boolean
  loading: boolean; busy: boolean; error: string | null; storageError: string | null; unreadableDraft: string | null
}
type Dependencies = {
  read: (signal: AbortSignal) => Promise<unknown>
  save: (body: string, signal: AbortSignal) => Promise<unknown>
  receipt: (id: string, signal: AbortSignal) => Promise<unknown>
  storage: () => Pick<Storage, 'getItem' | 'setItem'>; id: () => string
}
const rejectedCodes = new Set(['arc_progress_changed', 'arc_progress_needs_reconciliation', 'arc_progress_closed',
  'arc_progress_not_closed', 'arc_progress_not_completed', 'arc_progress_unavailable', 'arc_progress_busy',
  'arc_manuscript_unavailable', 'arc_manuscript_changed', 'arc_manuscript_empty', 'arc_confirmation_missing',
  'arc_progress_request_invalid', 'arc_progress_precondition_required', 'arc_manuscript_precondition_required',
  'arc_progress_history_full', 'arc_progress_no_pending_chapter'])

/** One immutable in-flight request per volume; receipts are history, never a current view. */
export class ArcProgressSession {
  private state: ArcProgressState = { view: null, notes: {}, direction: '', closingNote: '', pending: null, receipt: null,
    initialized: false, loading: false, busy: false, error: null, storageError: null, unreadableDraft: null }
  private listeners = new Set<() => void>()
  private readSequence = 0
  private readController: AbortController | null = null
  private minimumSequence = 0
  private restored = false
  private key: string
  constructor(readonly projectId: string, readonly volumeId: string, private deps: Dependencies = {
    read: signal => apiFetch(arcProgressPath(projectId, volumeId), { signal }),
    save: (body, signal) => apiFetch(arcProgressPath(projectId, volumeId), { method: 'POST', body, signal }),
    receipt: (id, signal) => apiFetch(`${arcProgressPath(projectId, volumeId)}/history/${encodeURIComponent(id)}`, { signal }),
    storage: () => sessionStorage, id: requestId,
  }) { this.key = `aiwrite:arc-progress:v1:${encodeURIComponent(projectId)}:${encodeURIComponent(volumeId)}` }

  getSnapshot = () => this.state
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener) } }
  private publish(patch: Partial<ArcProgressState>) {
    this.state = { ...this.state, ...patch }; for (const listener of this.listeners) listener()
  }
  private persist(): boolean {
    if (this.state.unreadableDraft !== null) return false
    try {
      const { notes, direction, closingNote, pending } = this.state
      this.deps.storage().setItem(this.key, JSON.stringify({ schema: 1, notes, direction, closingNote, pending, minimumSequence: this.minimumSequence }))
      this.publish({ storageError: null }); return true
    } catch {
      this.publish({ storageError: '进度输入暂存失败，请保留本页或下载原请求，恢复暂存后再提交。' }); return false
    }
  }
  attach = () => {
    if (!this.state.initialized) {
      let raw: string | null = null
      try {
        raw = this.deps.storage().getItem(this.key)
        if (raw) {
          const saved = JSON.parse(raw)
          if (!saved || saved.schema !== 1 || !saved.notes || typeof saved.notes !== 'object' || Array.isArray(saved.notes) ||
              Object.values(saved.notes).some(note => typeof note !== 'string') || typeof saved.direction !== 'string' ||
              typeof saved.closingNote !== 'string' || !Number.isSafeInteger(saved.minimumSequence) || saved.minimumSequence < 0) throw new Error('invalid draft')
          if (saved.pending !== null) {
            if (!saved.pending || typeof saved.pending.body !== 'string' || typeof saved.pending.sent !== 'boolean' || typeof saved.pending.rejected !== 'boolean') throw new Error('invalid pending')
            parseArcWrite(JSON.parse(saved.pending.body), this.projectId, this.volumeId)
          }
          this.minimumSequence = saved.minimumSequence; this.restored = true
          this.publish({ notes: saved.notes, direction: saved.direction, closingNote: saved.closingNote, pending: saved.pending })
        }
      } catch {
        this.publish({ unreadableDraft: raw ?? '', storageError: '无法核对浏览器中的原进度请求，请先保留完整记录；不会覆盖它或重新提交。' })
      }
      this.publish({ initialized: true })
    }
    void this.reload()
  }
  edit = (field: 'direction' | 'closingNote', value: string) => {
    this.restored = true; this.publish({ [field]: value }); this.persist()
  }
  editNote = (chapterId: string, value: string) => {
    this.publish({ notes: { ...this.state.notes, [chapterId]: value } }); this.persist()
  }
  recoverStorage = () => {
    if (this.state.busy) return
    if (this.state.unreadableDraft !== null) {
      this.publish({ initialized: false, unreadableDraft: null, storageError: null }); this.attach()
    } else this.persist()
  }
  private invalidateRead() {
    this.readSequence += 1; this.readController?.abort(); this.readController = null
  }
  reload = async () => {
    this.invalidateRead()
    const revision = this.readSequence, controller = new AbortController()
    this.readController = controller
    const timer = setTimeout(() => controller.abort(), 20_000)
    this.publish({ loading: true })
    try {
      const view = parseArcProgress(await this.deps.read(controller.signal), this.projectId, this.volumeId)
      if (revision !== this.readSequence) return
      const sequence = view.source.latest?.sequence ?? 0
      if (sequence < this.minimumSequence || (sequence === this.state.view?.source.latest?.sequence &&
          view.source.latest?.result_hash !== this.state.view.source.latest.result_hash)) throw new Error('读取的进度早于已确认记录，请重新读取。')
      this.minimumSequence = Math.max(sequence, this.minimumSequence)
      const direction = this.restored ? this.state.direction : view.progress.direction ?? ''
      this.restored = true
      this.publish({ view, direction, error: this.state.pending ? this.state.error : null })
    } catch (error) {
      if (revision === this.readSequence) this.publish({ view: null, error: error instanceof Error ? error.message : '进度读取失败，请重新核对。' })
    } finally {
      clearTimeout(timer)
      if (revision === this.readSequence) { this.readController = null; this.publish({ loading: false }) }
    }
  }
  submit = async (action: ArcAction, manuscript: ManuscriptSource | null = null, chapterId: string | null = null) => {
    const { view } = this.state
    if (!view || !this.state.initialized || this.state.pending || this.state.busy || this.state.loading || this.state.storageError) throw new Error('请先核对当前进度和已有请求。')
    if (action === 'acknowledge' && (!manuscript || manuscript.project_id !== this.projectId ||
        !view.source.chapters.some(c => c.id === manuscript.chapter_id && c.title === manuscript.title))) throw new Error('请先阅读本卷具体章节的保存稿。')
    const cid = manuscript?.chapter_id ?? chapterId
    const input: ArcProgressWrite = { request_id: this.deps.id(), expected_source: view.source, action,
      chapter_id: action === 'acknowledge' || action === 'retract' ? cid : null,
      expected_content_text: action === 'acknowledge' ? manuscript!.content_text : null,
      expected_title: action === 'acknowledge' ? manuscript!.title : null,
      author_note: (action === 'acknowledge' || action === 'retract') && cid ? this.state.notes[cid] ?? '' : '',
      direction: action === 'direction' ? this.state.direction : null,
      closing_note: action === 'finish' ? this.state.closingNote : '' }
    parseArcWrite(input, this.projectId, this.volumeId)
    this.publish({ pending: { body: JSON.stringify(input), sent: false, rejected: false }, error: null })
    return this.retry()
  }
  private accepted(value: unknown, input: ArcProgressWrite, response?: unknown) {
    const receipt = confirmArcReceipt(value, input)
    if (response !== undefined && !sameJson(receipt, response)) throw new Error('提交结果与持久回执不一致，请保留原请求。')
    this.minimumSequence = Math.max(this.minimumSequence, receipt.sequence)
    this.invalidateRead()
    this.publish({ pending: null, receipt, view: null, loading: false, error: null })
    this.persist()
    return receipt
  }
  retry = async (): Promise<ArcProgressReceipt> => {
    if (!this.state.pending || this.state.busy || this.state.unreadableDraft !== null) throw new Error('原请求不可重试，请先核对。')
    const original = this.state.pending, input = parseArcWrite(JSON.parse(original.body), this.projectId, this.volumeId)
    const pending = { ...original, sent: true, rejected: false }
    this.invalidateRead(); this.publish({ pending, busy: true, loading: false, error: null })
    if (!this.persist()) {
      this.publish({ pending: { ...original, rejected: original.rejected || !original.sent }, busy: false,
        error: original.sent ? '本次重试尚未发送，先前请求仍需核对。' : '请求尚未发送，请恢复暂存后重试。' })
      throw new Error(this.state.error!)
    }
    const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 45_000)
    let posted = false
    try {
      const response = await this.deps.save(pending.body, controller.signal)
      posted = true
      const receipt = this.accepted(await this.deps.receipt(input.request_id, controller.signal), input, response)
      await this.reload()
      return receipt
    } catch (error) {
      // Once POST returned, even a subsequent history error cannot claim rejection.
      const rejected = !posted && error instanceof ApiError && error.status >= 400 && error.status < 500 && rejectedCodes.has(error.code ?? '')
      this.publish({ pending: { ...pending, rejected }, error: error instanceof Error ? error.message : '进度保存尚未确认。' })
      this.persist(); throw error
    } finally { clearTimeout(timer); this.publish({ busy: false }) }
  }
  checkReceipt = async () => {
    if (!this.state.pending || this.state.busy) return
    const pending = this.state.pending, input = parseArcWrite(JSON.parse(pending.body), this.projectId, this.volumeId)
    const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 20_000)
    this.publish({ busy: true, error: null })
    try {
      this.accepted(await this.deps.receipt(input.request_id, controller.signal), input)
      await this.reload()
    } catch (error) {
      this.publish({ error: error instanceof ApiError && error.status === 404 ? '暂未找到原回执；请保留原请求，可重试同一请求。' : error instanceof Error ? error.message : '原回执仍未核对。' })
    } finally { clearTimeout(timer); this.publish({ busy: false }) }
  }
  dismissRejected = () => {
    if (!this.state.pending?.rejected || this.state.busy) return
    this.publish({ pending: null, error: null }); this.persist(); void this.reload()
  }
}

const sessions = new Map<string, ArcProgressSession>()
let unloadListenerInstalled = false
export function getArcProgressSession(projectId: string, volumeId: string) {
  if (typeof window === 'undefined') return new ArcProgressSession(projectId, volumeId)
  if (!unloadListenerInstalled) {
    window.addEventListener('beforeunload', event => {
      if ([...sessions.values()].some(session => session.getSnapshot().pending || session.getSnapshot().storageError)) {
        event.preventDefault(); event.returnValue = ''
      }
    })
    unloadListenerInstalled = true
  }
  const key = `${projectId}:${volumeId}`
  let session = sessions.get(key)
  if (!session) { session = new ArcProgressSession(projectId, volumeId); sessions.set(key, session) }
  return session
}
