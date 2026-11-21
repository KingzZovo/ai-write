export type ArcPlanInput = {
  request_id: string; expected_source: { version: 1; project_id: string; source_hash: string }
  mode: 'start' | 'next'; idea: string; background: string; core_setup: string; opening_scene: string; target_chapters: number
}
export type ArcPlanSource = {
  source: ArcPlanInput['expected_source']; mode: ArcPlanInput['mode']; target_volume_idx: number
  previous_material: Record<string, unknown> | null
  book_material_text?: string | null
  cognition_material_text?: string | null
}
export type ArcPlanResult = {
  version: 1; project_id: string; request_id: string; request_hash: string; journal_hash: string
  volume_id: string; volume_idx: number; chapter_ids: string[]; outline_id: string; chapter_count: number
  arc: { title: string; target_chapters: number }
}
export type ArcPlanStatus = {
  request_id: string; project_id: string; request_hash: string; revision: number
  status: 'pending' | 'running' | 'needs_review' | 'completed' | 'cancelled'; phase: string
  target_chapters: number; target_volume_idx: number; accepted_chapters: number; execution_attempts: number
  error_code: string; error_message: string; result: ArcPlanResult | null; created_at: string; updated_at: string
}
export type ArcPlanDetail = ArcPlanStatus & {
  integrity_valid: boolean; can_resume: boolean; request: ArcPlanInput & { version: 1; project_id: string }
  accepted_outline: (Record<string, unknown> & { chapter_summaries: Record<string, unknown>[] }) | null
  source: unknown; journal: unknown; execution_history: unknown; original_result: unknown
  book_material_text?: string | null
  cognition_material_text?: string | null
}
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
const hash = /^[0-9a-f]{64}$/
const record = (value: unknown): value is Record<string, unknown> => !!value && typeof value === 'object' && !Array.isArray(value)
const positive = (value: unknown): value is number => Number.isSafeInteger(value) && (value as number) > 0
export const arcPlanPath = (projectId: string) => `/api/projects/${encodeURIComponent(projectId)}/arc-plans`
export const arcPlanLabels = { pending: '已受理，等待规划', running: '正在规划', needs_review: '保留内容，等待核对', completed: '已创建分卷与章草稿', cancelled: '作者已停止' }

function token(value: unknown, projectId: string): asserts value is ArcPlanInput['expected_source'] {
  if (!record(value) || value.version !== 1 || value.project_id !== projectId || typeof value.source_hash !== 'string' ||
      !hash.test(value.source_hash) || Object.keys(value).length !== 3) throw new Error('本书规划来源返回不完整，请重新读取。')
}

export function parseArcPlanInput(value: unknown, projectId: string): ArcPlanInput {
  if (!record(value) || typeof value.request_id !== 'string' || !uuid.test(value.request_id) ||
      !['start', 'next'].includes(String(value.mode)) || !positive(value.target_chapters) || value.target_chapters < 4 || value.target_chapters > 40 ||
      ['idea', 'background', 'core_setup', 'opening_scene'].some(key => typeof value[key] !== 'string') || !(value.idea as string).trim() ||
      Object.keys(value).some(key => !['request_id', 'expected_source', 'mode', 'idea', 'background', 'core_setup', 'opening_scene', 'target_chapters'].includes(key))) {
    throw new Error('原弧规划请求无法核对；请保留完整材料。')
  }
  token(value.expected_source, projectId)
  return value as ArcPlanInput
}

export function parseArcPlanSource(value: unknown, projectId: string): ArcPlanSource {
  if (!record(value) || !positive(value.target_volume_idx) || !['start', 'next'].includes(String(value.mode)) ||
      (value.previous_material !== null && !record(value.previous_material)) ||
      (value.mode === 'start') !== (value.previous_material === null)) throw new Error('弧规划来源返回不完整。')
  token(value.source, projectId)
  if (value.book_material_text !== undefined && value.book_material_text !== null && typeof value.book_material_text !== 'string') throw new Error('全书创作材料返回不完整。')
  if (value.cognition_material_text !== undefined && value.cognition_material_text !== null && typeof value.cognition_material_text !== 'string') throw new Error('人物认知原件返回不完整。')
  return value as ArcPlanSource
}

export function parseArcPlanStatus(value: unknown, projectId: string, requestId?: string): ArcPlanStatus {
  if (!record(value) || value.project_id !== projectId || typeof value.request_id !== 'string' || !uuid.test(value.request_id) ||
      (requestId !== undefined && value.request_id !== requestId) || typeof value.request_hash !== 'string' || !hash.test(value.request_hash) ||
      !positive(value.revision) || !Object.hasOwn(arcPlanLabels, String(value.status)) || typeof value.phase !== 'string' ||
      !positive(value.target_chapters) || value.target_chapters < 4 || value.target_chapters > 40 || !positive(value.target_volume_idx) ||
      !Number.isSafeInteger(value.accepted_chapters) || (value.accepted_chapters as number) < 0 || (value.accepted_chapters as number) > value.target_chapters ||
      !Number.isSafeInteger(value.execution_attempts) || (value.execution_attempts as number) < 0 || (value.execution_attempts as number) > 3 ||
      ['error_code', 'error_message', 'created_at', 'updated_at'].some(key => typeof value[key] !== 'string')) throw new Error('原任务进度返回不完整或属于其他请求。')
  if (value.status === 'completed') {
    const result = value.result
    if (!record(result) || result.version !== 1 || result.project_id !== projectId || result.request_id !== value.request_id ||
        result.request_hash !== value.request_hash || typeof result.journal_hash !== 'string' || !hash.test(result.journal_hash) ||
        result.chapter_count !== value.target_chapters || result.volume_idx !== value.target_volume_idx ||
        ['volume_id', 'outline_id'].some(key => typeof result[key] !== 'string' || !uuid.test(result[key] as string)) ||
        !Array.isArray(result.chapter_ids) || result.chapter_ids.length !== value.target_chapters ||
        result.chapter_ids.some(id => typeof id !== 'string' || !uuid.test(id)) || new Set(result.chapter_ids).size !== result.chapter_ids.length ||
        !record(result.arc) || typeof result.arc.title !== 'string' || result.arc.target_chapters !== value.target_chapters ||
        value.accepted_chapters !== value.target_chapters) throw new Error('创建回执无法核对，请查询原任务完整记录。')
  } else if (value.result !== null) throw new Error('任务状态与创建回执不一致。')
  return value as ArcPlanStatus
}

export function parseArcPlanDetail(value: unknown, projectId: string, requestId: string): ArcPlanDetail {
  const status = parseArcPlanStatus(value, projectId, requestId)
  const detail = value as Record<string, unknown>
  if (detail.book_material_text !== undefined && detail.book_material_text !== null && typeof detail.book_material_text !== 'string') throw new Error('全书创作材料返回不完整。')
  if (detail.cognition_material_text !== undefined && detail.cognition_material_text !== null && typeof detail.cognition_material_text !== 'string') throw new Error('人物认知原件返回不完整。')
  if (typeof detail.integrity_valid !== 'boolean' || typeof detail.can_resume !== 'boolean' ||
      (!detail.integrity_valid && detail.can_resume)) throw new Error('原请求核对结果不完整。')
  if (detail.integrity_valid) {
    if (!record(detail.request) || detail.request.version !== 1 || detail.request.project_id !== projectId) throw new Error('原请求不属于本书。')
    const input = { ...detail.request }; delete input.version; delete input.project_id
    const parsed = parseArcPlanInput(input, projectId)
    if (parsed.request_id !== requestId || parsed.target_chapters !== status.target_chapters) throw new Error('原请求章数或身份不一致。')
    const outline = detail.accepted_outline
    if (!record(outline) || !Array.isArray(outline.chapter_summaries) || outline.chapter_summaries.length !== status.accepted_chapters ||
        outline.chapter_summaries.some((chapter, index) => !record(chapter) || chapter.chapter_idx !== index + 1 ||
          typeof chapter.title !== 'string' || typeof chapter.summary !== 'string')) throw new Error('已完成规划返回不完整。')
  }
  return value as ArcPlanDetail
}

export function sameArcPlanInput(detail: ArcPlanDetail, input: ArcPlanInput): boolean {
  if (!detail.integrity_valid) return false
  const original = detail.request
  return original.request_id === input.request_id && original.mode === input.mode && original.idea === input.idea &&
    original.background === input.background && original.core_setup === input.core_setup && original.opening_scene === input.opening_scene &&
    original.target_chapters === input.target_chapters && original.expected_source.version === input.expected_source.version &&
    original.expected_source.project_id === input.expected_source.project_id && original.expected_source.source_hash === input.expected_source.source_hash
}

export function parseArcPlanPage(value: unknown, projectId: string): { items: ArcPlanStatus[]; next_cursor: string | null } {
  if (!record(value) || !Array.isArray(value.items) || value.items.length > 100 ||
      (value.next_cursor !== null && typeof value.next_cursor !== 'string')) throw new Error('规划历史返回不完整。')
  const items = value.items.map(row => parseArcPlanStatus(row, projectId))
  if (new Set(items.map(row => row.request_id)).size !== items.length ||
      (value.next_cursor !== null && value.next_cursor !== items.at(-1)?.request_id)) throw new Error('规划历史位置无法核对。')
  return { items, next_cursor: value.next_cursor as string | null }
}
