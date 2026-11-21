import { ApiError, apiFetch } from '@/lib/api'
import { requestId } from '@/lib/requestId'
import { parseVolumeCandidate, parseVolumeOutlineReceipt, parseVolumeOutlineSource, volumeOutlineEditText, sameJson,
  type VolumeCandidate, type VolumeOutlineFormat, type VolumeOutlineReceipt, type VolumeOutlineSource, type VolumeOutlineWrite } from '@/lib/volumeOutlines'

type PendingWrite = { body: string; sent: boolean; rejected: boolean; draft: string }
export type VolumeOutlineState = {
  source: VolumeOutlineSource | null; remote: VolumeOutlineSource | null; text: string
  candidate: VolumeCandidate | null; previousDraft: string | null; pending: PendingWrite | null
  format: VolumeOutlineFormat; previousFormat: VolumeOutlineFormat
  initialized: boolean; loading: boolean; busy: boolean; error: string | null; storageError: string | null
  receipt: VolumeOutlineReceipt | null
}
type Dependencies = {
  read: (signal: AbortSignal) => Promise<unknown>
  save: (body: string, signal: AbortSignal) => Promise<unknown>
  storage: () => Pick<Storage, 'getItem' | 'setItem'>
  id: () => string
}
const rejectedCodes = new Set(['volume_outline_precondition_required', 'volume_outline_invalid', 'volume_outline_conflict',
  'volume_outline_unavailable', 'volume_outline_busy', 'volume_candidate_unavailable', 'volume_candidate_unverified', 'volume_candidate_changed'])

export class VolumeOutlineSession {
  private state: VolumeOutlineState = { source: null, remote: null, text: '', candidate: null, previousDraft: null,
    format: 'text', previousFormat: 'text',
    pending: null, initialized: false, loading: false, busy: false, error: null, storageError: null, receipt: null }
  private listeners = new Set<() => void>()
  private readSequence = 0
  private key: string
  constructor(readonly projectId: string, readonly volumeId: string, private deps: Dependencies = {
    read: (signal) => apiFetch(`/api/projects/${projectId}/volume-outlines/${volumeId}`, { signal }),
    save: (body, signal) => apiFetch(`/api/projects/${projectId}/volume-outlines/${volumeId}`, { method: 'PUT', body, signal }),
    storage: () => sessionStorage, id: requestId,
  }) { this.key = `aiwrite:volume-outline:v1:${encodeURIComponent(projectId)}:${encodeURIComponent(volumeId)}` }

  getSnapshot = () => this.state
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener) } }
  private publish(patch: Partial<VolumeOutlineState>) {
    this.state = { ...this.state, ...patch }
    for (const listener of this.listeners) listener()
  }
  private persist() {
    try {
      const { source, text, candidate, previousDraft, previousFormat, format, pending } = this.state
      this.deps.storage().setItem(this.key, JSON.stringify({ schema: 1, source, text, candidate, previousDraft, previousFormat, format, pending }))
      this.publish({ storageError: null })
      return true
    } catch {
      this.publish({ storageError: '卷纲编辑稿暂存失败，请保留本页或下载全文，恢复暂存后再保存。' })
      return false
    }
  }
  private pendingInput(pending: PendingWrite): VolumeOutlineWrite {
    const body = JSON.parse(pending.body) as VolumeOutlineWrite
    if (!body || typeof body.request_id !== 'string' || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(body.request_id) ||
        (typeof body.text === 'string') === (body.content_json !== undefined)) {
      throw new Error('原卷纲保存请求不完整，请保留记录并核对。')
    }
    if (body.content_json !== undefined && (!body.content_json || typeof body.content_json !== 'object' || Array.isArray(body.content_json) || body.candidate !== null)) {
      throw new Error('原结构卷纲请求不完整，请保留记录并核对。')
    }
    parseVolumeOutlineSource(body.expected_source, this.projectId, this.volumeId)
    if (body.candidate !== null) parseVolumeCandidate(body.candidate)
    if (body.text !== undefined ? body.text !== pending.draft : !sameJson(JSON.parse(pending.draft), body.content_json)) {
      throw new Error('原卷纲请求与暂存的提交稿不一致，请保留记录并核对。')
    }
    return body
  }

  attach = (initialSource?: VolumeOutlineSource) => {
    if (this.state.initialized) { void this.reload(); return }
    try {
      const raw = this.deps.storage().getItem(this.key)
      if (raw) {
        const saved = JSON.parse(raw)
        if (saved.schema !== 1 || typeof saved.text !== 'string' || (saved.previousDraft !== null && typeof saved.previousDraft !== 'string')) {
          throw new Error('invalid stored outline draft')
        }
        const source = saved.source === null ? null : parseVolumeOutlineSource(saved.source, this.projectId, this.volumeId)
        const candidate = saved.candidate === null ? null : parseVolumeCandidate(saved.candidate)
        const format = saved.format ?? 'text', previousFormat = saved.previousFormat ?? 'text'
        if (!['text', 'structure'].includes(format) || !['text', 'structure'].includes(previousFormat) || (format === 'structure' && candidate)) {
          throw new Error('invalid stored outline format')
        }
        if (!source && (saved.pending !== null || candidate || saved.text || saved.previousDraft !== null)) {
          throw new Error('stored outline draft has no source')
        }
        if (saved.pending !== null) {
          if (!saved.pending || typeof saved.pending.body !== 'string' || typeof saved.pending.sent !== 'boolean' || typeof saved.pending.rejected !== 'boolean' || typeof saved.pending.draft !== 'string') {
            throw new Error('invalid stored outline request')
          }
          const input = this.pendingInput(saved.pending)
          if (!sameJson(input.expected_source, source)) throw new Error('stored request has a different baseline')
        }
        this.publish({ source, text: saved.text, candidate, previousDraft: saved.previousDraft, pending: saved.pending, format, previousFormat })
      }
      this.publish({ initialized: true })
      if (initialSource) this.acceptRead(parseVolumeOutlineSource(initialSource, this.projectId, this.volumeId))
      else void this.reload()
    } catch {
      this.publish({ initialized: true, storageError: '本页的卷纲暂存记录未能读取，请保留当前页并核对浏览器暂存。' })
    }
  }

  private acceptRead(source: VolumeOutlineSource) {
    if (this.state.source && (this.state.pending || this.state.text !== volumeOutlineEditText(this.state.source, this.state.format) || this.state.candidate)) {
      this.publish({ remote: sameJson(source, this.state.source) ? null : source, error: null })
    } else {
      this.publish({ source, remote: null, text: volumeOutlineEditText(source, this.state.format), error: null })
      this.persist()
    }
  }

  reload = async () => {
    if (!this.state.source && this.state.storageError) return
    const serial = ++this.readSequence, controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), 20_000)
    this.publish({ loading: true })
    try {
      const source = parseVolumeOutlineSource(await this.deps.read(controller.signal), this.projectId, this.volumeId)
      if (serial === this.readSequence) this.acceptRead(source)
    } catch (error) {
      if (serial === this.readSequence) this.publish({ error: error instanceof Error ? error.message : '卷纲读取失败。' })
    } finally {
      clearTimeout(timer)
      if (serial === this.readSequence) this.publish({ loading: false })
    }
  }

  edit = (text: string) => { if (this.state.source) { this.publish({ text }); this.persist() } }

  changeFormat = (format: VolumeOutlineFormat) => {
    if (!this.state.source || this.state.pending || this.state.busy || this.state.candidate ||
        this.state.text !== volumeOutlineEditText(this.state.source, this.state.format)) return
    this.publish({ format, text: volumeOutlineEditText(this.state.source, format), error: null })
    this.persist()
  }

  recoverStorage = () => {
    if (this.state.busy) return
    if (this.state.source || this.state.pending) { this.persist(); return }
    this.publish({ initialized: false, storageError: null })
    this.attach()
  }

  useCandidate = (candidate: VolumeCandidate) => {
    if (this.state.pending || this.state.busy || !this.state.source) return
    if (sameJson(this.state.candidate, candidate) && this.state.text === candidate.text) return
    this.publish({ text: candidate.text, candidate, format: 'text', previousDraft: this.state.text, previousFormat: this.state.format, error: null })
    this.persist()
  }

  restorePreviousDraft = () => {
    if (this.state.pending || this.state.previousDraft === null || this.state.busy) return
    this.publish({ text: this.state.previousDraft, previousDraft: this.state.text, format: this.state.previousFormat,
      previousFormat: this.state.format, candidate: null, error: null })
    this.persist()
  }

  useRemoteSource = () => {
    if (!this.state.remote || this.state.busy || (this.state.pending && !this.state.pending.rejected)) return
    this.readSequence += 1
    this.publish({ source: this.state.remote, remote: null, pending: null, error: null, loading: false })
    this.persist()
  }

  dismissRejected = () => {
    if (this.state.busy || !this.state.pending?.rejected) return
    this.publish({ pending: null, error: null })
    this.persist()
  }

  save = async () => {
    if (!this.state.source || this.state.busy || this.state.pending || this.state.remote) throw new Error('请先核对当前卷纲和已有保存请求。')
    let content: Record<string, unknown> | undefined
    if (this.state.format === 'structure') {
      try {
        content = JSON.parse(this.state.text)
        if (!content || typeof content !== 'object' || Array.isArray(content)) throw new Error('invalid structure')
      } catch {
        this.publish({ error: '结构卷纲格式不完整，编辑稿已保留。请核对完整对象后再保存。' })
        throw new Error(this.state.error!)
      }
    }
    const input: VolumeOutlineWrite = { request_id: this.deps.id(), expected_source: this.state.source,
      ...(content ? { content_json: content } : { text: this.state.text }), candidate: this.state.candidate }
    this.publish({ pending: { body: JSON.stringify(input), sent: false, rejected: false, draft: this.state.text }, error: null })
    return this.retry()
  }

  retry = async (): Promise<VolumeOutlineReceipt> => {
    if (!this.state.pending || this.state.busy) throw new Error('原卷纲保存请求不存在或正在提交。')
    const original = this.state.pending, input = this.pendingInput(original)
    const pending = { ...original, sent: true, rejected: false }
    this.readSequence += 1
    this.publish({ pending, busy: true, loading: false, error: null })
    if (!this.persist()) {
      const message = original.sent ? '本次重试尚未发送，先前保存仍未确认，请保留原请求。' : '请求尚未发送，请恢复暂存后重试。'
      this.publish({ pending: { ...original, rejected: original.rejected || !original.sent }, busy: false, error: message })
      throw new Error(message)
    }
    const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 45_000)
    try {
      const receipt = parseVolumeOutlineReceipt(await this.deps.save(pending.body, controller.signal), input)
      this.readSequence += 1
      this.publish({ source: receipt.source, pending: null,
        remote: this.state.remote && !sameJson(this.state.remote, receipt.source) ? this.state.remote : null, receipt,
        text: this.state.text === pending.draft ? volumeOutlineEditText(receipt.source, this.state.format) : this.state.text,
        candidate: this.state.candidate?.task_id === input.candidate?.task_id ? null : this.state.candidate,
        error: null, loading: false })
      this.persist()
      return receipt
    } catch (error) {
      const rejected = error instanceof ApiError && error.status >= 400 && error.status < 500 && rejectedCodes.has(error.code ?? '')
      this.publish({ pending: { ...pending, rejected }, error: error instanceof Error ? error.message : '卷纲保存尚未确认。' })
      this.persist()
      throw error
    } finally { clearTimeout(timer); this.publish({ busy: false }) }
  }
}

const sessions = new Map<string, VolumeOutlineSession>()
let unloadListenerInstalled = false
export function getVolumeOutlineSession(projectId: string, volumeId: string) {
  if (typeof window === 'undefined') return new VolumeOutlineSession(projectId, volumeId)
  if (!unloadListenerInstalled) {
    window.addEventListener('beforeunload', (event) => {
      for (const session of sessions.values()) {
        const state = session.getSnapshot()
        if (state.pending || state.storageError || (state.source && state.text !== volumeOutlineEditText(state.source, state.format))) {
          event.preventDefault(); event.returnValue = ''; break
        }
      }
    })
    unloadListenerInstalled = true
  }
  const key = `${projectId}:${volumeId}`
  let session = sessions.get(key)
  if (!session) { session = new VolumeOutlineSession(projectId, volumeId); sessions.set(key, session) }
  return session
}
