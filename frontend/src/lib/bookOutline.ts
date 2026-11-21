import { apiFetch, apiSSE } from '@/lib/api'

export interface BookOutline {
  id: string
  project_id: string
  level: string
  content_json: Record<string, unknown>
  is_confirmed: number
  version: number
  created_at?: string | null
}

export function outlineText(content: Record<string, unknown>): string {
  return typeof content.raw_text === 'string' ? content.raw_text : JSON.stringify(content, null, 2)
}

export function selectBookOutline<T extends BookOutline>(outlines: T[]): T | null {
  return outlines.filter(o => o.level === 'book' && !('volume_idx' in o.content_json))
    .sort((a, b) => b.is_confirmed - a.is_confirmed
      || (b.created_at || '').localeCompare(a.created_at || '') || b.version - a.version)[0] || null
}

// EOF and the staged `done` event alone do not prove a database save.
export function generateSavedBook(body: Record<string, unknown>, onChunk: (text: string) => void,
  onEvent: (event: Record<string, unknown>) => void,
  onController: (controller: AbortController) => void): Promise<BookOutline> {
  return new Promise<string>((resolve, reject) => {
    let savedId: string | null = null
    let completed = false
    let failed = false
    const controller = apiSSE('/api/generate/outline', { ...body, level: 'book', staged_stream: true },
      onChunk, () => {
        if (failed || !savedId || !completed) reject(new Error('大纲生成或保存未完成，请刷新检查后重试。'))
        else resolve(savedId)
      }, event => {
        if (event.error || event.event === 'error' || event.status === 'save_failed' || event.status === 'failed') failed = true
        if (event.status === 'saved' && typeof event.outline_id === 'string') savedId = event.outline_id
        if (event.status === 'completed') completed = true
        onEvent(event)
      }, () => { failed = true })
    onController(controller)
  }).then(async id => {
    const outline = await apiFetch<BookOutline>(`/api/projects/${body.project_id}/outlines/${id}`)
    if (outline.id !== id || outline.project_id !== body.project_id || outline.level !== 'book'
      || 'volume_idx' in outline.content_json || typeof outline.content_json.raw_text !== 'string'
      || !outline.content_json.raw_text.trim()) {
      throw new Error('大纲保存结果与本次生成不符，请刷新检查。')
    }
    return outline
  })
}
