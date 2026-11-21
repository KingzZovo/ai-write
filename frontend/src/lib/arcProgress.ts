import { apiFetch } from '@/lib/api'
import { sameJson } from '@/lib/volumeOutlines'

export const arcActionLabels = { acknowledge: '确认章节', retract: '撤销确认', reconcile: '采用当前安排',
  direction: '保存下一步方向', finish: '结束本弧', reopen: '继续本弧' } as const
export type ArcAction = keyof typeof arcActionLabels
export type ArcConfirmation = { request_id: string; content_hash: string; title: string; author_note: string }
export type ArcProgress = {
  schema: 1; volume_id: string; plan_hash: string; chapter_order: string[]
  acknowledged: Record<string, ArcConfirmation>; phase: 'active' | 'awaiting_direction' | 'completed'
  direction: string | null; direction_chapter_id: string | null; closed_early: boolean; closing_note: string; legacy_request_id: string | null
  completion_source_hash: string | null
}
export type ArcChapterSource = { id: string; title: string; chapter_idx: number; global_idx: number; content_hash: string | null }
export type ArcProgressSource = {
  schema: 1; project_id: string; volume_id: string; volume_idx: number; volume_title: string
  outline: { id: string; version: number; hash: string }
  latest: { request_id: string; sequence: number; result_hash: string } | null
  chapters: ArcChapterSource[]
}
export type ArcChapter = Omit<ArcChapterSource, 'chapter_idx' | 'global_idx'> & {
  chapter_idx: number | null; global_idx: number | null
  state: 'pending' | 'confirmed' | 'changed' | 'removed'; confirmation: ArcConfirmation | null
}
export type ArcProgressView = {
  source: ArcProgressSource; title: string; volume_title: string; progress: ArcProgress
  chapters: ArcChapter[]; issues: { code: string; message: string; chapter_id?: string }[]
  confirmed_count: number; chapter_count: number; next_chapter_id: string | null
  legacy: { unverified: boolean; reported_count: number; status: string; history_request_id: string | null }
}
export type ArcProgressWrite = {
  request_id: string; expected_source: ArcProgressSource; action: ArcAction; chapter_id: string | null
  expected_content_text: string | null; expected_title: string | null
  author_note: string; direction: string | null; closing_note: string
}
export type ArcProgressReceipt = {
  schema: 1; status: 'saved'; request_id: string; project_id: string; volume_id: string
  volume_title: string; volume_idx: number; sequence: number; action: ArcAction; chapter_id: string | null
  manuscript_hash: string | null; request_hash: string; before_hash: string; manuscript_snapshot_hash: string
  progress: ArcProgress
}
export type ArcHistoryDetail = {
  request_id: string; receipt: Record<string, unknown>; before: Record<string, unknown>
  manuscript: Record<string, unknown>; integrity_valid: boolean
}

const uuid = (value: unknown): value is string => typeof value === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(value)
const hash = (value: unknown): value is string => typeof value === 'string' && /^[0-9a-f]{64}$/.test(value)
const positive = (value: unknown): value is number => typeof value === 'number' && Number.isSafeInteger(value) && value > 0
const fail = (): never => { throw new Error('弧进度资料不完整或来源不一致，请保留原请求并重新核对。') }
function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return fail()
  return value as Record<string, unknown>
}
function ids(value: unknown): value is string[] {
  return Array.isArray(value) && value.length <= 40 && value.every(uuid) && new Set(value).size === value.length
}
function confirmation(value: unknown): ArcConfirmation {
  const row = object(value)
  if (!uuid(row.request_id) || !hash(row.content_hash) || typeof row.title !== 'string' || typeof row.author_note !== 'string') return fail()
  return row as ArcConfirmation
}
function progress(value: unknown, volumeId: string): ArcProgress {
  const row = object(value), acknowledged = object(row.acknowledged)
  if (row.schema !== 1 || row.volume_id !== volumeId || !hash(row.plan_hash) || !ids(row.chapter_order) ||
      !['active', 'awaiting_direction', 'completed'].includes(row.phase as string) || typeof row.closed_early !== 'boolean' ||
      typeof row.closing_note !== 'string' || (row.direction !== null && typeof row.direction !== 'string') ||
      (row.direction_chapter_id !== null && !uuid(row.direction_chapter_id)) ||
      (row.phase === 'completed' ? !hash(row.completion_source_hash) : row.completion_source_hash !== null) ||
      (row.legacy_request_id !== null && !uuid(row.legacy_request_id)) || Object.keys(acknowledged).length > 40) return fail()
  for (const [cid, value] of Object.entries(acknowledged)) { if (!uuid(cid)) return fail(); confirmation(value) }
  return row as ArcProgress
}

export function parseArcSource(value: unknown, projectId: string, volumeId: string): ArcProgressSource {
  const row = object(value), outline = object(row.outline)
  if (row.schema !== 1 || row.project_id !== projectId || row.volume_id !== volumeId || !uuid(volumeId) ||
      !uuid(projectId) || !positive(row.volume_idx) || typeof row.volume_title !== 'string' ||
      !uuid(outline.id) || !positive(outline.version) || !hash(outline.hash) || !Array.isArray(row.chapters) || row.chapters.length > 40) return fail()
  const chapters = row.chapters.map((value) => {
    const chapter = object(value)
    if (!uuid(chapter.id) || typeof chapter.title !== 'string' || !positive(chapter.chapter_idx) || !positive(chapter.global_idx) ||
        (chapter.content_hash !== null && !hash(chapter.content_hash))) return fail()
    return chapter as ArcChapterSource
  })
  if (new Set(chapters.map(c => c.id)).size !== chapters.length || new Set(chapters.map(c => c.global_idx)).size !== chapters.length ||
      chapters.some((c, i) => i > 0 && c.chapter_idx <= chapters[i-1].chapter_idx)) return fail()
  if (row.latest !== null) {
    const latest = object(row.latest)
    if (!uuid(latest.request_id) || !positive(latest.sequence) || !hash(latest.result_hash)) return fail()
  }
  return row as ArcProgressSource
}

export function parseArcProgress(value: unknown, projectId: string, volumeId: string): ArcProgressView {
  const row = object(value), source = parseArcSource(row.source, projectId, volumeId), saved = progress(row.progress, volumeId)
  const legacy = object(row.legacy)
  if (typeof row.title !== 'string' || row.volume_title !== source.volume_title || !Array.isArray(row.chapters) || row.chapters.length > 80 ||
      !Array.isArray(row.issues) || typeof legacy.unverified !== 'boolean' || !Number.isSafeInteger(legacy.reported_count) ||
      (legacy.reported_count as number) < 0 || typeof legacy.status !== 'string' || legacy.history_request_id !== saved.legacy_request_id) return fail()
  for (const value of row.issues) { const issue = object(value); if (typeof issue.code !== 'string' || typeof issue.message !== 'string') return fail() }
  const chapters = row.chapters.map((value) => {
    const chapter = object(value), cid = chapter.id
    if (!uuid(cid) || typeof chapter.title !== 'string' || !['pending', 'confirmed', 'changed', 'removed'].includes(chapter.state as string)) return fail()
    if (!sameJson(chapter.confirmation, saved.acknowledged[cid] ?? null)) return fail()
    const actual = source.chapters.find((item) => item.id === cid)
    if (chapter.state === 'removed') {
      if (actual || !chapter.confirmation || chapter.chapter_idx !== null || chapter.global_idx !== null || chapter.content_hash !== null) return fail()
    } else {
      if (!actual || chapter.title !== actual.title || chapter.chapter_idx !== actual.chapter_idx || chapter.global_idx !== actual.global_idx || chapter.content_hash !== actual.content_hash) return fail()
      const proof = saved.acknowledged[cid]
      const expected = !proof ? 'pending' : proof.title === actual.title && proof.content_hash === actual.content_hash ? 'confirmed' : 'changed'
      if (chapter.state !== expected) return fail()
    }
    return chapter as ArcChapter
  })
  if (new Set(chapters.map(c => c.id)).size !== chapters.length || source.chapters.some(c => !chapters.some(item => item.id === c.id)) ||
      Object.keys(saved.acknowledged).some(cid => !chapters.some(c => c.id === cid)) ||
      row.confirmed_count !== chapters.filter(c => c.state === 'confirmed').length || row.chapter_count !== source.chapters.length) return fail()
  const next = row.issues.length === 0 && saved.phase !== 'completed' ? chapters.find(c => c.state === 'pending')?.id ?? null : null
  if (row.next_chapter_id !== next) return fail()
  return row as ArcProgressView
}

export function parseArcWrite(value: unknown, projectId: string, volumeId: string): ArcProgressWrite {
  const row = object(value)
  parseArcSource(row.expected_source, projectId, volumeId)
  if (!uuid(row.request_id) || typeof row.action !== 'string' || !Object.hasOwn(arcActionLabels, row.action) ||
      typeof row.author_note !== 'string' || typeof row.closing_note !== 'string') return fail()
  const needsChapter = row.action === 'acknowledge' || row.action === 'retract'
  if (needsChapter ? !uuid(row.chapter_id) : row.chapter_id !== null) return fail()
  if (row.action === 'acknowledge' ? typeof row.expected_title !== 'string' || typeof row.expected_content_text !== 'string' || !row.expected_content_text.trim()
    : row.expected_title !== null || row.expected_content_text !== null) return fail()
  if (row.action === 'direction' ? typeof row.direction !== 'string' || !row.direction.trim() : row.direction !== null) return fail()
  if ((row.action !== 'finish' && row.closing_note) || (!needsChapter && row.author_note)) return fail()
  return row as ArcProgressWrite
}

export function parseArcHistoryDetail(value: unknown, requestId: string): ArcHistoryDetail {
  const row = object(value)
  if (row.request_id !== requestId || typeof row.integrity_valid !== 'boolean') return fail()
  // Corrupt evidence is still downloadable. It can never clear a pending write.
  if (row.integrity_valid) { object(row.receipt); object(row.before); object(row.manuscript) }
  return row as ArcHistoryDetail
}

export function confirmArcReceipt(value: unknown, input: ArcProgressWrite): ArcProgressReceipt {
  const detail = parseArcHistoryDetail(value, input.request_id)
  if (!detail.integrity_valid) return fail()
  const row = object(detail.receipt), source = input.expected_source, saved = progress(row.progress, source.volume_id)
  if (row.schema !== 1 || row.status !== 'saved' || row.request_id !== input.request_id || row.project_id !== source.project_id ||
      row.volume_id !== source.volume_id || row.volume_idx !== source.volume_idx || row.volume_title !== source.volume_title ||
      row.sequence !== (source.latest?.sequence ?? 0) + 1 || row.action !== input.action || row.chapter_id !== input.chapter_id ||
      !hash(row.request_hash) || !hash(row.before_hash) || !hash(row.manuscript_snapshot_hash) ||
      !sameJson(detail.before.source, source) || detail.before.author_note !== input.author_note) return fail()
  if (input.action === 'acknowledge') {
    const manuscript = detail.manuscript, proof = saved.acknowledged[input.chapter_id!]
    if (manuscript.chapter_id !== input.chapter_id || manuscript.title !== input.expected_title || manuscript.content_text !== input.expected_content_text ||
        !hash(row.manuscript_hash) || manuscript.content_hash !== row.manuscript_hash || !proof || proof.request_id !== input.request_id ||
        proof.title !== input.expected_title || proof.content_hash !== row.manuscript_hash || proof.author_note !== input.author_note) return fail()
  } else if (row.manuscript_hash !== null || Object.keys(detail.manuscript).length !== 0) return fail()
  if ((input.action === 'direction' && saved.direction !== input.direction) ||
      (input.action === 'finish' && (saved.phase !== 'completed' || saved.closing_note !== input.closing_note)) ||
      (input.action === 'retract' && Object.hasOwn(saved.acknowledged, input.chapter_id!)) ||
      (input.action === 'reopen' && (saved.phase !== 'active' || saved.closed_early)) ||
      (input.action === 'reconcile' && !sameJson(saved.chapter_order, source.chapters.map(c => c.id)))) return fail()
  return row as ArcProgressReceipt
}

export const arcProgressPath = (projectId: string, volumeId?: string) => `/api/projects/${encodeURIComponent(projectId)}/arc-progress${volumeId ? '/' + encodeURIComponent(volumeId) : ''}`
export async function readArcProgress(projectId: string, volumeId: string, signal: AbortSignal) {
  return parseArcProgress(await apiFetch(arcProgressPath(projectId, volumeId), { signal }), projectId, volumeId)
}

export function downloadArcRecord(text: string, filename: string) {
  const url = URL.createObjectURL(new Blob([text], { type: 'application/json;charset=utf-8' }))
  const link = document.createElement('a'); link.href = url; link.download = filename
  document.body.appendChild(link); link.click(); link.remove(); URL.revokeObjectURL(url)
}
