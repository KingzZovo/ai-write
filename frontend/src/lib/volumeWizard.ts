import { apiFetch, apiSSE } from '@/lib/api'
import { selectVolumeOutlines } from './volumeOutlineSelection'

export interface WizardVolume extends Record<string, unknown> { id: string; volume_idx: number }
export interface WizardChapter extends Record<string, unknown> { id: string; volume_id: string; chapter_idx: number }
interface WizardOutline { id: string; level: string; version: number; is_confirmed: number; content_json: Record<string, unknown>; created_at?: string | null }
export interface WizardState { volumes: WizardVolume[]; chapters: WizardChapter[]; outlines: WizardOutline[] }

export function generateSavedVolume(body: Record<string, unknown>, onChunk: (text: string) => void,
  onController: (controller: AbortController) => void): Promise<string> {
  return new Promise((resolve, reject) => {
    let savedId: string | null = null
    let completed = false
    let failure: Error | null = null
    const controller = apiSSE('/api/generate/outline', body, onChunk, () => {
      if (failure) reject(failure)
      else if (!savedId || !completed) reject(new Error('未收到分卷保存确认，请刷新检查后再操作。'))
      else resolve(savedId)
    }, event => {
      if (event.error || event.status === 'save_failed' || event.status === 'failed') {
        failure = new Error(typeof event.error === 'string' ? event.error : '分卷生成或保存失败')
      }
      if (event.status === 'saved' && typeof event.outline_id === 'string') savedId = event.outline_id
      if (event.status === 'completed') completed = true
    }, error => { failure = error })
    onController(controller)
  })
}

async function load(projectId: string): Promise<WizardState> {
  const base = `/api/projects/${projectId}`
  const [volumes, chapters, outlines] = await Promise.all([
    apiFetch<WizardVolume[]>(`${base}/volumes`), apiFetch<WizardChapter[]>(`${base}/chapters`),
    apiFetch<WizardOutline[]>(`${base}/outlines`),
  ])
  return { volumes, chapters, outlines }
}

export function volumeOutline(state: WizardState, volume: WizardVolume): WizardOutline | undefined {
  return selectVolumeOutlines(state.outlines, state.volumes)[volume.id]
}

function complete(state: WizardState, volume: WizardVolume, savedId?: string): boolean {
  const outline = volumeOutline(state, volume)
  const chapters = state.chapters.filter(c => c.volume_id === volume.id).sort((a, b) => a.chapter_idx - b.chapter_idx)
  return !!outline && (!savedId || outline.id === savedId) && chapters.length > 0
    && chapters.length === outline.content_json.chapter_count
    && chapters.every((c, i) => c.chapter_idx === i + 1)
}

export async function runVolumeWizard(options: {
  projectId: string; bookId: string; count: number; userInput: string;
  stopped: () => boolean; onController: (controller: AbortController) => void;
  onProgress: (text: string) => void; onState: (state: WizardState) => void;
}): Promise<WizardState | null> {
  if (!Number.isInteger(options.count) || options.count < 1) throw new Error('卷数必须是正整数')
  let state = await load(options.projectId)
  if (options.stopped()) return null
  options.onState(state)
  for (let idx = 1; idx <= options.count; idx++) {
    if (options.stopped()) return null
    const matches = state.volumes.filter(v => v.volume_idx === idx)
    if (matches.length > 1) throw new Error(`第 ${idx} 卷存在重复记录，请先整理。`)
    const existing = matches[0]
    if (existing && state.chapters.some(c => c.volume_id === existing.id)) {
      if (!complete(state, existing)) throw new Error(`第 ${idx} 卷已有章节，但卷纲或章号不完整，请先检查。`)
      options.onProgress(`第 ${idx} 卷已有完整卷章，已保留。`)
      continue
    }
    let preview = ''
    options.onProgress(`正在生成第 ${idx}/${options.count} 卷大纲...`)
    const savedId = await generateSavedVolume({ project_id: options.projectId, level: 'volume',
      volume_idx: idx, parent_outline_id: options.bookId, user_input: options.userInput,
      planned_volume_count: options.count,
    }, chunk => { preview += chunk; options.onProgress(`正在生成第 ${idx}/${options.count} 卷大纲...\n${preview.slice(-200)}`) }, options.onController)
    if (options.stopped()) return null
    state = await load(options.projectId)
    if (options.stopped()) return null
    options.onState(state)
    const volumes = state.volumes.filter(v => v.volume_idx === idx)
    if (volumes.length !== 1 || !complete(state, volumes[0], savedId)) {
      throw new Error(`第 ${idx} 卷保存结果尚未核实，请刷新检查。`)
    }
  }
  return state
}
