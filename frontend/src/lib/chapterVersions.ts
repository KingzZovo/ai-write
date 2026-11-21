import { apiFetch } from '@/lib/api'

export type ChapterVersion = {
  id: string
  chapter_id: string
  parent_id: string | null
  branch_name: string
  content_text: string
  content_diff: string
  word_count: number
  created_at: string
  is_active: boolean
  source: string
  metadata: Record<string, unknown>
}

export type VersionSource = {
  chapter_id: string
  project_id: string
  title: string
  content_text: string
  word_count: number
}

export type VersionRestoration = {
  status: 'ok'
  chapter_id: string
  active_version_id: string
  content_text: string
  word_count: number
  backup_version_id: string | null
  changed: boolean
}

function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('版本响应不完整，请刷新核对。')
  return value as Record<string, unknown>
}

export function parseChapterVersion(value: unknown, chapterId: string): ChapterVersion {
  const item = record(value)
  if (item.chapter_id !== chapterId || typeof item.id !== 'string' || !item.id ||
      typeof item.content_text !== 'string' || typeof item.branch_name !== 'string' ||
      typeof item.is_active !== 'boolean' || typeof item.created_at !== 'string' ||
      typeof item.word_count !== 'number' || !Number.isFinite(item.word_count)) {
    throw new Error('版本响应与当前章节不匹配或不完整，请刷新核对。')
  }
  return {
    id: item.id, chapter_id: chapterId, parent_id: typeof item.parent_id === 'string' ? item.parent_id : null,
    branch_name: item.branch_name, content_text: item.content_text,
    content_diff: typeof item.content_diff === 'string' ? item.content_diff : '',
    word_count: item.word_count, created_at: item.created_at, is_active: item.is_active,
    source: typeof item.source === 'string' ? item.source : 'unknown',
    metadata: item.metadata && typeof item.metadata === 'object' && !Array.isArray(item.metadata)
      ? item.metadata as Record<string, unknown> : {},
  }
}

export async function loadChapterVersions(chapterId: string, signal?: AbortSignal): Promise<ChapterVersion[]> {
  const data = await apiFetch<unknown>(`/api/chapters/${chapterId}/versions`, { signal })
  if (!Array.isArray(data)) throw new Error('版本列表响应不完整，请刷新核对。')
  return data.map((item) => parseChapterVersion(item, chapterId))
}

export async function loadVersionSource(chapterId: string, signal?: AbortSignal): Promise<VersionSource> {
  const item = record(await apiFetch<unknown>(`/api/chapters/${chapterId}/versions/source`, { signal }))
  if (item.chapter_id !== chapterId || typeof item.project_id !== 'string' ||
      typeof item.title !== 'string' || typeof item.content_text !== 'string' ||
      typeof item.word_count !== 'number' || !Number.isFinite(item.word_count)) {
    throw new Error('保存稿响应与当前章节不匹配或不完整，请刷新核对。')
  }
  return item as VersionSource
}

export async function restoreChapterVersion(chapterId: string, version: ChapterVersion, source: VersionSource, force: boolean) {
  if (version.chapter_id !== chapterId || source.chapter_id !== chapterId) throw new Error('所选版本与当前章节不匹配。')
  const item = record(await apiFetch<unknown>(`/api/chapters/${chapterId}/versions/${version.id}/rollback`, {
    method: 'POST', body: JSON.stringify({ expected_content_text: source.content_text, force }),
  }))
  const changed = source.content_text !== version.content_text
  if (item.status !== 'ok' || item.chapter_id !== chapterId || item.active_version_id !== version.id ||
      item.content_text !== version.content_text || item.changed !== changed ||
      typeof item.word_count !== 'number' || !Number.isFinite(item.word_count) ||
      (changed ? typeof item.backup_version_id !== 'string' || !item.backup_version_id : item.backup_version_id !== null)) {
    throw new Error('未收到完整恢复确认，请保留所选版本并刷新保存稿核对。')
  }
  return item as VersionRestoration
}

export function versionSourceLabel(source: string) {
  const labels: Record<string, string> = {
    ai_generation: 'AI稿件', user_edit: '作者快照', branch: '分支', merge: '合并',
    version_restore_backup: '恢复前备份', variant_apply_backup: '采用候选前备份',
    author_edit_backup: '正文缩短前备份',
  }
  return labels[source] || '历史版本'
}
