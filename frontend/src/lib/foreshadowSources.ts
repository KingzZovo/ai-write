export type SourcePosition = { volume_idx?: number; chapter_idx?: number; global_idx?: number }
export type SourceLink = {
  id: string; revision: number; operation: 'link' | 'unlink'; chapter_id: string | null
  source_snapshot: SourcePosition | null; record_snapshot: { description?: string; type?: string }
  evidence: string | null; note: string; created_by: string | null; created_at: string
}
export type SourceContext = {
  foreshadow_id: string; description: string; type: string; record_present: boolean; record_version: string | null
  latest_link: SourceLink | null; history: SourceLink[]; has_more: boolean
  preview: { chapter_id: string; title: string; content: string; source_version: string; source_snapshot: SourcePosition } | null
}
export type SourceChapter = { id: string; title: string; chapter_idx: number; volume_idx: number; volume_title: string }
export type SourceChapterPage = { chapters: SourceChapter[]; has_more: boolean }

export function sourcePositionLabel(position: SourcePosition | null) {
  if (!position || !Number.isInteger(position.volume_idx) || !Number.isInteger(position.chapter_idx) ||
      position.volume_idx! <= 0 || position.chapter_idx! <= 0) return '章节位置待核对'
  return `第${position.volume_idx}卷第${position.chapter_idx}章`
}

export function validSourceContext(data: SourceContext, foreshadowId: string, chapterId: string) {
  const validLink = (link: SourceLink) => link && typeof link.id === 'string' && Number.isSafeInteger(link.revision) &&
    ['link', 'unlink'].includes(link.operation) && link.record_snapshot && typeof link.record_snapshot.description === 'string' &&
    typeof link.note === 'string' && typeof link.created_at === 'string' &&
    (link.evidence === null || typeof link.evidence === 'string') && (link.created_by === null || typeof link.created_by === 'string')
  return data?.foreshadow_id === foreshadowId && typeof data.record_present === 'boolean' &&
    (data.record_present ? /^[0-9a-f]{64}$/.test(data.record_version || '') : data.record_version === null) &&
    typeof data.description === 'string' && Array.isArray(data.history) && data.history.every(validLink) &&
    (data.latest_link === null || validLink(data.latest_link)) && typeof data.has_more === 'boolean' &&
    (chapterId ? (data.preview?.chapter_id === chapterId && typeof data.preview.content === 'string' &&
      /^[0-9a-f]{64}$/.test(data.preview.source_version)) : data.preview === null)
}

export function selectedSourceText(raw: string, displayed: string, start: number, end: number): string | null {
  // Textarea selection offsets use UTF-16 and its value normalizes CRLF/CR
  // to LF. Map those offsets back to the original saved string before citing.
  if (raw.replace(/\r\n?/g, '\n') !== displayed || !Number.isSafeInteger(start) ||
      !Number.isSafeInteger(end) || start < 0 || end <= start || end > displayed.length) return null
  let shownOffset = 0, from = -1, to = raw.length
  for (let rawOffset = 0; rawOffset < raw.length; rawOffset += 1) {
    if (shownOffset === start) from = rawOffset
    if (shownOffset === end) { to = rawOffset; break }
    if (raw[rawOffset] === '\r' && raw[rawOffset + 1] === '\n') rawOffset += 1
    shownOffset += 1
  }
  const splitsSurrogate = (offset: number) => offset > 0 && offset < raw.length &&
    raw.charCodeAt(offset - 1) >= 0xd800 && raw.charCodeAt(offset - 1) <= 0xdbff &&
    raw.charCodeAt(offset) >= 0xdc00 && raw.charCodeAt(offset) <= 0xdfff
  return from >= 0 && !splitsSurrogate(from) && !splitsSurrogate(to) ? raw.slice(from, to) : null
}
