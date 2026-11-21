import { ApiError, apiFetch } from '@/lib/api'
import { requestId } from '@/lib/requestId'

export type GenerationInput = {
  project_id: string
  task_type: 'chapter' | 'outline_book' | 'outline_volume' | 'outline_chapter' | 'outline_from_reference'
  chapter_id?: string | null
  volume_idx?: number | null
  expected_content_text?: string
  user_input?: string
  style_id?: string | null
  structure_book_id?: string
  params?: Record<string, unknown>
}
export type GenerationReceipt = {
  task_id: string
  project_id: string
  task_type: string
  chapter_id: string | null
  volume_idx: number | null
  status: string
}
export type PendingGeneration = { id: string; body: string; rejected: boolean; sent: boolean }
export type SubmissionSnapshot = {
  pending: PendingGeneration | null
  busy: boolean
  error: string | null
  storageError: string | null
  receipt: GenerationReceipt | null
  initialized: boolean
}
type Dependencies = {
  send: (body: string, signal: AbortSignal) => Promise<unknown>
  storage: () => Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>
  id: () => string
}
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
const types = new Set(['chapter', 'outline_book', 'outline_volume', 'outline_chapter', 'outline_from_reference'])
const rejectedCodes = new Set(['generation_request_invalid', 'generation_request_id_required',
  'generation_project_unavailable', 'generation_chapter_unavailable', 'generation_volume_unavailable',
  'chapter_content_precondition_required', 'chapter_edit_conflict', 'outline_chain_incomplete', 'outline_target_required'])

function savedInput(pending: PendingGeneration, projectId: string): GenerationInput & { request_id: string } {
  const raw = JSON.parse(pending.body) as GenerationInput & { request_id: string }
  if (!raw || raw.project_id !== projectId || raw.request_id !== pending.id || !uuid.test(pending.id) || !types.has(raw.task_type) ||
      (raw.task_type === 'chapter' && (typeof raw.expected_content_text !== 'string' || typeof raw.chapter_id !== 'string' || !raw.chapter_id))) {
    throw new Error('本页保留的生成请求不完整，请保留记录并重新核对。')
  }
  return raw
}

export function generationReceipt(raw: unknown, pending: PendingGeneration, projectId: string): GenerationReceipt {
  const input = savedInput(pending, projectId)
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) throw new Error('未取得完整的生成任务回执。')
  const result = raw as Record<string, unknown>
  if (result.task_id !== pending.id || result.project_id !== projectId || result.task_type !== input.task_type ||
      result.chapter_id !== (input.chapter_id ?? null) || result.volume_idx !== (input.volume_idx ?? null) ||
      typeof result.status !== 'string' || !result.status.trim()) {
    throw new Error('生成回执与原请求不一致，请保留原请求并重新核对。')
  }
  return { task_id: pending.id, project_id: projectId, task_type: input.task_type,
    chapter_id: input.chapter_id ?? null, volume_idx: input.volume_idx ?? null, status: result.status }
}

/** One immutable request per project until its receipt or rejection is known. */
export class GenerationSubmissionSession {
  private state: SubmissionSnapshot = { pending: null, busy: false, error: null, storageError: null, receipt: null, initialized: false }
  private listeners = new Set<() => void>()
  private key: string
  constructor(readonly projectId: string, private dependencies: Dependencies = {
    send: (body, signal) => apiFetch('/api/generate/async', { method: 'POST', body, signal }),
    storage: () => sessionStorage,
    id: requestId,
  }) { this.key = `aiwrite:generation-request:v1:${encodeURIComponent(projectId)}` }

  getSnapshot = () => this.state
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener) } }
  private publish(patch: Partial<SubmissionSnapshot>) {
    this.state = { ...this.state, ...patch }
    for (const listener of this.listeners) listener()
  }

  attach = () => {
    if (this.state.initialized) return
    this.restore()
  }

  restore = () => {
    if (this.state.busy || this.state.pending) return
    try {
      const raw = this.dependencies.storage().getItem(this.key)
      let pending: PendingGeneration | null = null
      if (raw) {
        const saved = JSON.parse(raw) as { schema?: number; pending?: PendingGeneration }
        if (saved?.schema !== 1 || !saved.pending || typeof saved.pending.body !== 'string' ||
            typeof saved.pending.rejected !== 'boolean' || typeof saved.pending.sent !== 'boolean') {
          throw new Error('invalid pending generation request')
        }
        savedInput(saved.pending, this.projectId)
        pending = saved.pending
      }
      this.publish({ pending, initialized: true, storageError: null,
        error: pending ? '本页保留了一份尚未确认的生成请求，请先核对或重试原请求。' : null })
    } catch {
      this.publish({ initialized: true, storageError: '未能读取本页保留的生成请求，请恢复浏览器暂存后重新读取。' })
    }
  }

  private persist(pending: PendingGeneration | null) {
    try {
      const storage = this.dependencies.storage()
      if (pending) storage.setItem(this.key, JSON.stringify({ schema: 1, pending }))
      else storage.removeItem(this.key)
      this.publish({ storageError: null })
      return true
    } catch {
      this.publish({ storageError: '本次请求未能在浏览器暂存。请保留当前页面和请求记录，恢复暂存后再操作。' })
      return false
    }
  }

  submit = async (input: GenerationInput): Promise<GenerationReceipt> => {
    this.attach()
    if (this.state.busy || this.state.pending) throw new Error('请先核对已有的生成请求。')
    if (this.state.storageError) throw new Error(this.state.storageError)
    if (input.project_id !== this.projectId) throw new Error('作品已变化，请重新选择生成章节。')
    const id = this.dependencies.id()
    const pending = { id, body: JSON.stringify({ ...input, request_id: id }), rejected: false, sent: false }
    savedInput(pending, this.projectId)
    this.publish({ pending, receipt: null, error: null })
    return this.retry()
  }

  retry = async (): Promise<GenerationReceipt> => {
    if (!this.state.pending || this.state.busy) throw new Error('原请求不存在或正在提交。')
    const original = this.state.pending
    const pending = { ...original, rejected: false, sent: true }
    this.publish({ busy: true, pending, error: null })
    // Persist the exact body before any request can be sent. A refresh then
    // always retries the same UUID and author source, even after lost replies.
    if (!this.persist(pending)) {
      const message = original.sent ? '本次重试尚未发送，先前提交的结果仍未确认，请保留原请求。' : '请求尚未发送，请恢复浏览器暂存后重试。'
      this.publish({ busy: false, pending: { ...original, rejected: original.rejected || !original.sent }, error: message })
      throw new Error(message)
    }
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), 45_000)
    try {
      const receipt = generationReceipt(await this.dependencies.send(pending.body, controller.signal), pending, this.projectId)
      this.persist(null)
      this.publish({ pending: null, receipt, error: null })
      return receipt
    } catch (error) {
      const rejected = error instanceof ApiError && error.status >= 400 && error.status < 500 && rejectedCodes.has(error.code ?? '')
      const next = { ...pending, rejected }
      this.persist(next)
      this.publish({ pending: next, error: error instanceof Error ? error.message : '生成提交未获确认，请核对原请求。' })
      throw error
    } finally {
      clearTimeout(timer)
      this.publish({ busy: false })
    }
  }

  dismissRejected = () => {
    if (this.state.busy || !this.state.pending?.rejected) return
    if (this.persist(null)) this.publish({ pending: null, error: null })
  }
}

const sessions = new Map<string, GenerationSubmissionSession>()
export function getGenerationSubmissionSession(projectId: string) {
  if (typeof window === 'undefined') return new GenerationSubmissionSession(projectId)
  let session = sessions.get(projectId)
  if (!session) { session = new GenerationSubmissionSession(projectId); sessions.set(projectId, session) }
  return session
}
