import { apiFetch } from '@/lib/api'

export type VolumeOutlineSource = {
  schema: 1
  project_id: string
  volume_id: string
  volume_idx: number
  title: string
  outline: { id: string; parent_id: string | null; content_json: Record<string, unknown>; version: number; is_confirmed: number } | null
  selection: { id: string; hash: string }[]
}
export type VolumeCandidate = { task_id: string; text: string }
export type VolumeOutlineReceipt = {
  status: 'saved'; request_id: string; source: VolumeOutlineSource; changed: boolean
  preserved_chapters: number; candidate_task_id: string | null
  submitted_json: Record<string, unknown> | null; synced_chapters: number; unmaterialized_chapters: number
}
export type VolumeOutlineWrite = {
  request_id: string; expected_source: VolumeOutlineSource; text?: string; content_json?: Record<string, unknown>; candidate: VolumeCandidate | null
}
export type VolumeOutlineFormat = 'text' | 'structure'

export function sameJson(left: unknown, right: unknown): boolean {
  const stable = (value: unknown): unknown => Array.isArray(value) ? value.map(stable)
    : value && typeof value === 'object' ? Object.fromEntries(Object.entries(value).sort(([a], [b]) => a.localeCompare(b)).map(([key, child]) => [key, stable(child)])) : value
  return JSON.stringify(stable(left)) === JSON.stringify(stable(right))
}

function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('卷纲资料返回不完整，请重新读取。')
  return value as Record<string, unknown>
}
function positive(value: unknown): value is number { return typeof value === 'number' && Number.isSafeInteger(value) && value > 0 }

export function parseVolumeOutlineSource(raw: unknown, projectId: string, volumeId?: string): VolumeOutlineSource {
  const row = object(raw)
  if (row.schema !== 1 || row.project_id !== projectId || typeof row.volume_id !== 'string' || !row.volume_id ||
      (volumeId && row.volume_id !== volumeId) || !positive(row.volume_idx) || typeof row.title !== 'string' || !Array.isArray(row.selection)) {
    throw new Error('卷纲来源与当前作品或分卷不一致，请重新核对。')
  }
  if (row.outline !== null) {
    const outline = object(row.outline)
    object(outline.content_json)
    if (typeof outline.id !== 'string' || !outline.id || !positive(outline.version) ||
        ![0, 1].includes(outline.is_confirmed as number) ||
        (outline.parent_id !== null && typeof outline.parent_id !== 'string')) throw new Error('保存卷纲的版本信息不完整。')
  }
  for (const value of row.selection) {
    const item = object(value)
    if (typeof item.id !== 'string' || typeof item.hash !== 'string' || !/^[0-9a-f]{64}$/.test(item.hash)) {
      throw new Error('卷纲来源清单不完整。')
    }
  }
  if (new Set(row.selection.map((value) => object(value).id)).size !== row.selection.length ||
      (row.outline === null ? row.selection.length !== 0 : object(row.selection[0]).id !== object(row.outline).id)) {
    throw new Error('当前卷纲与来源清单不一致。')
  }
  return row as VolumeOutlineSource
}

export function parseVolumeCandidate(value: unknown): VolumeCandidate {
  const row = object(value)
  if (typeof row.task_id !== 'string' || !row.task_id || typeof row.text !== 'string' || !row.text.trim()) {
    throw new Error('卷纲候选尚未完整返回。')
  }
  return { task_id: row.task_id, text: row.text }
}

export function parseVolumeOutlineReceipt(value: unknown, input: VolumeOutlineWrite): VolumeOutlineReceipt {
  const row = object(value), expected = input.expected_source
  const source = parseVolumeOutlineSource(row.source, expected.project_id, expected.volume_id)
  if (row.status !== 'saved' || row.request_id !== input.request_id || typeof row.changed !== 'boolean' ||
      !Number.isSafeInteger(row.preserved_chapters) || (row.preserved_chapters as number) < 0 ||
      row.candidate_task_id !== (input.candidate?.task_id ?? null) || source.volume_idx !== expected.volume_idx ||
      source.title !== expected.title || source.outline?.is_confirmed !== 1 ||
      (input.content_json ? !sameJson(row.submitted_json, input.content_json) : source.outline.content_json.raw_text !== input.text) ||
      !Number.isSafeInteger(row.synced_chapters) || (row.synced_chapters as number) < 0 ||
      !Number.isSafeInteger(row.unmaterialized_chapters) || (row.unmaterialized_chapters as number) < 0) {
    throw new Error('卷纲保存回执与本次请求不一致，请保留编辑稿并核对原请求。')
  }
  return { status: 'saved', request_id: input.request_id, source, changed: row.changed,
    preserved_chapters: row.preserved_chapters as number, candidate_task_id: input.candidate?.task_id ?? null,
    submitted_json: input.content_json ?? null, synced_chapters: row.synced_chapters as number,
    unmaterialized_chapters: row.unmaterialized_chapters as number }
}

const labels: Record<string, string> = { summary: '本卷摘要', title: '标题', core_conflict: '核心冲突',
  emotional_arc: '情感变化', turning_points: '关键转折', main_progress: '主线推进', side_progress: '支线推进',
  chapter_summaries: '章节安排', chapter_idx: '章号', foreshadows: '伏笔安排', transition_to_next: '与下一卷衔接',
  characters: '人物', new_characters: '新登场人物', purpose: '本章作用', scene_beats: '场景安排',
  key_events: '主要事件', hook: '卷末安排', chapter_count: '章节数', word_count_target: '目标字数',
  target_word_count: '目标字数', key_scene: '关键场景', foreshadow_state: '线索状态' }
const identities = new Set(['volume_id', 'volume_idx', 'chapter_id', 'global_idx'])
const controls = new Set([...identities, 'title', 'chapter_idx', 'chapter_count', 'word_count_target', 'target_word_count', 'raw_text'])
function meaningful(value: unknown): boolean {
  if (Array.isArray(value)) return value.length > 0
  if (value && typeof value === 'object') return Object.keys(value).length > 0
  return Boolean(value)
}
function display(value: unknown): string {
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  if (Array.isArray(value)) return value.map((entry, index) => `${index + 1}. ${display(entry)}`).join('\n\n')
  if (!value || typeof value !== 'object') return ''
  const row = value as Record<string, unknown>
  const text = Object.entries(row).filter(([key]) => !key.startsWith('_') && !identities.has(key))
    .map(([key, entry]) => `${labels[key] || key}：\n${display(entry)}`).join('\n\n')
  return (row._order_unavailable || ('_chapter_id' in row && !row._chapter_id) ? '【原章节关联待核对】\n' : '') + text
}

/** Keep all narrative fields; saved author text owns later prose changes. */
export function volumeOutlineText(source: VolumeOutlineSource | null): string {
  return volumeOutlineContentText(source?.outline?.content_json)
}

export function volumeOutlineEditText(source: VolumeOutlineSource | null, format: VolumeOutlineFormat): string {
  return format === 'structure' ? JSON.stringify(source?.outline?.content_json ?? {}, null, 2) : volumeOutlineText(source)
}

export function volumeOutlineContentText(data: Record<string, unknown> | null | undefined): string {
  if (!data) return ''
  const raw = data.raw_text
  const structured = Object.entries(data).some(([key, value]) => !key.startsWith('_') && !controls.has(key) && meaningful(value))
  if (typeof raw === 'string' && (data._author_text_revision || !data.volume_id || !structured)) return raw
  const structuredData = { ...data }
  delete structuredData.raw_text
  return display(structuredData) || (typeof raw === 'string' ? raw : '')
}

export async function loadVolumeCandidate(projectId: string, taskId: string, signal: AbortSignal) {
  const raw = object(await apiFetch(`/api/projects/${encodeURIComponent(projectId)}/volume-outlines/candidates/${encodeURIComponent(taskId)}`, { signal }))
  const source = parseVolumeOutlineSource(raw.source, projectId)
  const candidate = parseVolumeCandidate(raw.candidate)
  if (candidate.task_id !== taskId) throw new Error('返回了另一份卷纲候选，请重新核对。')
  return { source, candidate }
}
