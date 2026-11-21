import { apiFetch } from '@/lib/api'

export type ManuscriptSource = {
  project_id: string
  chapter_id: string
  title: string
  content_text: string
  word_count: number
  status: string
}

export type DraftStatus = 'draft' | 'needs_review'
export type ManuscriptReceipt = {
  status: 'saved'
  manuscript: ManuscriptSource
  changed: boolean
  backup_version_id: string | null
}

function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('正文响应不完整，请保留编辑区稿件并重新核对。')
  return value as Record<string, unknown>
}

export function parseManuscript(value: unknown, projectId: string, chapterId: string): ManuscriptSource {
  const item = record(value)
  if (item.project_id !== projectId || item.chapter_id !== chapterId || typeof item.content_text !== 'string' ||
      typeof item.title !== 'string' || typeof item.status !== 'string' || !item.status ||
      typeof item.word_count !== 'number' || !Number.isSafeInteger(item.word_count) || item.word_count < 0) {
    throw new Error('正文响应与当前章节不匹配或不完整，请重新核对。')
  }
  // Python len counts Unicode code points, not JavaScript UTF-16 units.
  let length = 0
  for (const _character of item.content_text) length += 1
  if (item.word_count !== length) throw new Error('保存稿字数与完整正文不一致，请重新核对。')
  return item as ManuscriptSource
}

export async function readManuscript(projectId: string, chapterId: string, signal?: AbortSignal) {
  return parseManuscript(await apiFetch<unknown>(`/api/projects/${projectId}/chapters/${chapterId}/content`, { signal }), projectId, chapterId)
}

export async function saveManuscript(source: ManuscriptSource, text: string, force = false, status: DraftStatus | null = null): Promise<ManuscriptReceipt> {
  const raw = record(await apiFetch<unknown>(`/api/projects/${source.project_id}/chapters/${source.chapter_id}/content`, {
    method: 'PUT', body: JSON.stringify({ content_text: text, expected_content_text: source.content_text,
      force, ...(status ? { status } : {}) }),
  }))
  const saved = parseManuscript(raw.manuscript, source.project_id, source.chapter_id)
  const changed = source.content_text !== text
  if (raw.status !== 'saved' || saved.content_text !== text || (status !== null && saved.status !== status) || raw.changed !== changed ||
      (force && changed ? typeof raw.backup_version_id !== 'string' || !raw.backup_version_id : raw.backup_version_id !== null)) {
    throw new Error('未收到这份稿件的完整保存确认，请保留编辑区稿件并重新核对。')
  }
  return { status: 'saved', manuscript: saved, changed, backup_version_id: raw.backup_version_id as string | null }
}
