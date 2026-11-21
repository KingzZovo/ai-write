export const cognitionKinds = ['learns', 'still_unknown', 'believes', 'suspects'] as const
export type CognitionKind = typeof cognitionKinds[number]
export const cognitionKindLabels: Record<CognitionKind, string> = {
  learns: '本章得知', still_unknown: '当时仍不知', believes: '当时确信（可能误信）', suspects: '当时怀疑',
}
export type CognitionChange = { character: string; evidence: string } & (
  { learns: string } | { still_unknown: string } | { believes: string } | { suspects: string }
)
export type CognitionChapter = {
  chapter_id: string; chapter_title: string; global_idx: number | null; status: string
  message: string; version: string | null; source_hash: string | null; attempts: number
  changes_count: number | null; can_retry: boolean; can_edit: boolean
  source_mode: string; revision_id: string | null
}
export type CognitionDetail = CognitionChapter & {
  changes: CognitionChange[] | null; model_changes: CognitionChange[] | null
  model_status: string; source_content: string | null
}

export function cognitionKind(change: CognitionChange): CognitionKind {
  const kind = cognitionKinds.find(key => key in change)
  if (!kind) throw new Error('unknown cognition kind')
  return kind
}

export function cognitionFact(change: CognitionChange): string {
  return (change as unknown as Record<string, string>)[cognitionKind(change)]
}

export function validCognitionChange(value: unknown): value is CognitionChange {
  if (!value || typeof value !== 'object') return false
  const item = value as Record<string, unknown>
  const kind = cognitionKinds.find(key => key in item)
  return !!kind && Object.keys(item).length === 3 &&
    ['character', kind, 'evidence'].every(key => typeof item[key] === 'string' &&
      (item[key] as string).trim().length > 0 && !(item[key] as string).includes('\0')) &&
    (item.character !== '__reader__' || kind === 'learns')
}
