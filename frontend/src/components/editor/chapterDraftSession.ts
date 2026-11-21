import { ApiError } from '@/lib/api'
import { DraftStatus, ManuscriptSource, parseManuscript, readManuscript, saveManuscript } from '@/lib/chapterManuscripts'

export type ChapterDraftSnapshot = {
  projectId: string
  chapterId: string
  text: string
  source: ManuscriptSource | null
  comparison: ManuscriptSource | null
  statusToSave: DraftStatus | null
  loading: boolean
  saving: boolean
  streaming: boolean
  blocked: boolean
  error: string | null
  notice: string | null
  backupError: string | null
}

type DraftDependencies = {
  read: typeof readManuscript
  save: typeof saveManuscript
  storage?: Pick<Storage, 'getItem' | 'setItem' | 'removeItem'> | null
}

const sessions = new Map<string, ChapterDraftSession>()
const keyFor = (projectId: string, chapterId: string) => `aiwrite.chapter-draft.v1.${projectId}.${chapterId}`

/** One source and one serialized writer per chapter, retained across switches. */
export class ChapterDraftSession {
  private state: ChapterDraftSnapshot
  private listeners = new Set<() => void>()
  private revision = 0
  private request = 0
  private generation = 0
  private mounts = 0
  private initialized = false
  private restoredDraft = false
  private readController: AbortController | null = null
  private saveTimer: ReturnType<typeof setTimeout> | null = null
  private checkpointTimer: ReturnType<typeof setTimeout> | null = null
  private write: Promise<boolean> | null = null
  private storage: DraftDependencies['storage'] = null

  constructor(readonly projectId: string, readonly chapterId: string,
    private readonly dependencies: DraftDependencies = { read: readManuscript, save: saveManuscript }) {
    this.state = { projectId, chapterId, text: '', source: null, comparison: null, statusToSave: null,
      loading: true, saving: false, streaming: false, blocked: false, error: null, notice: null, backupError: null }
  }

  getSnapshot = () => this.state
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener) } }
  isDirty = () => this.state.source
    ? this.state.text !== this.state.source.content_text || (this.state.statusToSave !== null && this.state.statusToSave !== this.state.source.status)
    : this.restoredDraft
  hasPending = () => this.isDirty() || this.state.saving || this.state.streaming ||
    (this.state.blocked && this.state.source !== null)
  canEdit = () => !this.state.streaming && (this.state.source !== null || this.restoredDraft)
  preserve = () => this.checkpoint()
  canGenerate = () => this.state.source !== null && !this.isDirty() && !this.state.loading && !this.state.saving && !this.state.blocked && !this.state.streaming

  private publish(patch: Partial<ChapterDraftSnapshot>, persist = true) {
    this.state = { ...this.state, ...patch }
    if (persist && this.initialized && !this.checkpointTimer) {
      this.checkpointTimer = setTimeout(() => { this.checkpointTimer = null; this.checkpoint() }, 300)
    }
    for (const listener of this.listeners) listener()
  }

  private checkpoint() {
    if (!this.initialized) return
    if (this.checkpointTimer) { clearTimeout(this.checkpointTimer); this.checkpointTimer = null }
    try {
      if (!this.storage) {
        if (this.hasPending()) throw new Error('storage unavailable')
        return
      }
      const key = keyFor(this.projectId, this.chapterId)
      if (this.hasPending() || this.state.blocked) {
        this.storage.setItem(key, JSON.stringify({ schema: 1, text: this.state.text, source: this.state.source,
          statusToSave: this.state.statusToSave, projectId: this.projectId, chapterId: this.chapterId }))
      } else this.storage.removeItem(key)
      if (this.state.backupError) this.publish({ backupError: null }, false)
    } catch {
      if (!this.state.backupError) this.publish({ backupError: '浏览器暂存不可用，当前稿件仍在本页，请下载备份。' }, false)
    }
  }

  attach() {
    this.mounts += 1
    // React may set up the same effect again after a cleanup (Strict Mode).
    // Re-register the retained instance so its in-flight writer stays unique.
    sessions.set(keyFor(this.projectId, this.chapterId), this)
    if (!this.initialized) {
      this.initialized = true
      try { this.storage = this.dependencies.storage === undefined ? window.sessionStorage : this.dependencies.storage } catch { this.storage = null }
      try {
        const raw = this.storage?.getItem(keyFor(this.projectId, this.chapterId))
        const saved = raw ? JSON.parse(raw) : null
        if (saved?.schema === 1 && saved.projectId === this.projectId && saved.chapterId === this.chapterId && typeof saved.text === 'string') {
          let source: ManuscriptSource | null = null
          try { source = parseManuscript(saved.source, this.projectId, this.chapterId) } catch { /* Keep the text even if its old source is damaged. */ }
          this.restoredDraft = true
          this.publish({ text: saved.text, source, blocked: true,
            statusToSave: saved.statusToSave === 'draft' || saved.statusToSave === 'needs_review' ? saved.statusToSave : null,
            notice: '已找回浏览器暂存稿，正在核对当前保存稿。' }, false)
        }
      } catch { this.publish({ backupError: '未能读取浏览器暂存，请保留另存的稿件。' }, false) }
    }
    if (!this.state.saving && !this.state.streaming) void this.reload()
    let attached = true
    return () => { if (attached) { attached = false; this.detach() } }
  }

  private detach() {
    this.mounts = Math.max(0, this.mounts - 1)
    if (this.mounts > 0) return
    this.stopTimer()
    this.request += 1
    this.readController?.abort()
    this.readController = null
    this.checkpoint()
    // Retain pending writes and drafts. Clean, unused chapters need not keep
    // an entire long book in browser memory merely because they were visited.
    if (!this.hasPending() && !this.state.blocked) this.release()
  }

  private release() {
    const key = keyFor(this.projectId, this.chapterId)
    if (sessions.get(key) === this) sessions.delete(key)
  }

  private stopTimer() {
    if (this.saveTimer) clearTimeout(this.saveTimer)
    this.saveTimer = null
  }

  private scheduleSave() {
    this.stopTimer()
    if (this.mounts && this.isDirty() && this.state.source && !this.state.blocked && !this.state.loading && !this.state.streaming && !this.state.saving) {
      this.saveTimer = setTimeout(() => { this.saveTimer = null; void this.save() }, 3000)
    }
  }

  edit(text: string) {
    if (this.state.streaming || (!this.state.source && !this.restoredDraft)) return
    this.revision += 1
    this.publish({ text, notice: null })
    this.scheduleSave()
  }

  async reload(): Promise<boolean> {
    if (this.state.saving || this.state.streaming) return false
    this.stopTimer()
    const request = ++this.request
    this.readController?.abort()
    const abort = new AbortController()
    this.readController = abort
    const revision = this.revision
    // After an unknown save, returning to the old baseline is still an
    // author choice: the server may already contain the attempted new body.
    const hadDraft = this.isDirty() || this.restoredDraft || (this.state.blocked && this.state.source !== null)
    this.publish({ loading: true, error: null })
    try {
      const source = await this.dependencies.read(this.projectId, this.chapterId, abort.signal)
      if (request !== this.request) return false
      const matches = source.content_text === this.state.text &&
        (this.state.statusToSave === null || this.state.statusToSave === source.status)
      if (matches || (!hadDraft && revision === this.revision && !this.isDirty())) {
        this.restoredDraft = false
        this.publish({ source, text: source.content_text, comparison: null, statusToSave: null,
          loading: false, blocked: false, notice: '已核对当前保存稿。' })
      } else {
        this.publish({ source: this.state.source ?? source, comparison: source, loading: false, blocked: true,
          notice: '已保留编辑区稿件，请对照当前保存稿后决定如何保存。' })
      }
      this.checkpoint()
      return true
    } catch (cause) {
      if (request === this.request) this.publish({ loading: false, blocked: true,
        error: cause instanceof Error ? cause.message : '保存稿暂不可读，请保留编辑区稿件。' })
      return false
    } finally {
      if (request === this.request) this.readController = null
      if (!this.mounts && !this.state.loading && !this.hasPending() && !this.state.blocked) this.release()
    }
  }

  save(force = false): Promise<boolean> {
    if (this.write) return this.write
    if (!this.state.source || this.state.blocked || this.state.loading || this.state.streaming) return Promise.resolve(false)
    if (!this.isDirty()) return Promise.resolve(true)
    this.stopTimer()
    const source = this.state.source, text = this.state.text, status = this.state.statusToSave
    const revision = this.revision
    this.publish({ saving: true, error: null, notice: null })
    this.checkpoint()
    this.write = (async () => {
      try {
        const receipt = await this.dependencies.save(source, text, force, status)
        this.restoredDraft = false
        // Keep edits made while this particular body was being saved.
        this.publish({ source: receipt.manuscript, statusToSave: this.revision === revision ? null : this.state.statusToSave,
          notice: receipt.backup_version_id ? '正文已保存，替换前的全文已保留在版本历史中。' : null })
        return true
      } catch (cause) {
        const conflict = cause instanceof ApiError && cause.code === 'chapter_edit_conflict'
        this.publish({ blocked: true, error: conflict ? '保存稿已变化，当前编辑稿已保留，请读取保存稿核对。'
          : cause instanceof Error ? cause.message : '保存未获确认，请读取保存稿核对。' })
        return false
      } finally {
        this.write = null
        this.publish({ saving: false })
        this.checkpoint()
        this.scheduleSave()
        if (!this.mounts && !this.hasPending() && !this.state.blocked) this.release()
      }
    })()
    return this.write
  }

  saveReviewedDraft(force = false) {
    const source = this.state.comparison
    if (!source || this.state.loading || this.state.saving || this.state.streaming) return Promise.resolve(false)
    this.publish({ source, comparison: null, blocked: false, error: null })
    return this.save(force)
  }

  useSavedDraft() {
    const source = this.state.comparison
    if (!source || this.state.loading || this.state.saving || this.state.streaming) return
    this.revision += 1
    this.restoredDraft = false
    this.publish({ source, text: source.content_text, comparison: null, statusToSave: null,
      blocked: false, error: null, notice: '编辑区已载入核对过的保存稿。' })
    this.checkpoint()
  }

  beginGeneration(): number | null {
    if (!this.canGenerate()) return null
    this.stopTimer()
    const generation = ++this.generation
    this.publish({ streaming: true, text: '', notice: null })
    return generation
  }

  replaceGenerated(generation: number, text: string) {
    if (generation !== this.generation || !this.state.streaming) return
    this.revision += 1
    this.publish({ text })
  }

  async finishGeneration(generation: number, status: DraftStatus | null = null) {
    if (generation !== this.generation || !this.state.streaming) return
    this.generation += 1
    const text = this.state.text || this.state.source?.content_text || ''
    this.publish({ streaming: false, text,
      statusToSave: text !== this.state.source?.content_text ? status : null })
    this.checkpoint()
    await this.reload()
  }
}

export function getChapterDraftSession(projectId: string, chapterId: string) {
  const key = keyFor(projectId, chapterId)
  let session = sessions.get(key)
  if (!session) { session = new ChapterDraftSession(projectId, chapterId); sessions.set(key, session) }
  return session
}

export function hasPendingChapterDrafts() {
  return [...sessions.values()].some((session) => session.hasPending())
}

export function preserveChapterDrafts() {
  for (const session of sessions.values()) session.preserve()
}
