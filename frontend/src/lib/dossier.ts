/** Pure helpers for the per-reference-book "book dossier" feature.
 *
 * Backend contract (distillation rework):
 * - POST /api/decompile/{book_id}/consolidate  → 202, starts consolidation
 * - GET  /api/decompile/{book_id}/dossier      → DossierResponse (404 while absent)
 * - reference_books.metadata_json.dossier_status tracks progress
 *
 * Everything here is defensive: the endpoints may not exist yet and field
 * shapes may vary, so all readers accept `unknown`.
 */

export interface DossierResponse {
  style_block?: string | null
  structure_block?: string | null
  world_block?: string | null
  style_data?: unknown
  plot_data?: unknown
  world_data?: unknown
  consolidated_at?: string | null
  source_counts?: Record<string, unknown> | null
}

const RUNNING_STATUSES = ['pending', 'queued', 'running', 'consolidating']

/** Read `dossier_status` out of a book's metadata_json (any shape). */
export function getDossierStatus(metadata: unknown): string | null {
  if (!metadata || typeof metadata !== 'object') return null
  const raw = (metadata as Record<string, unknown>).dossier_status
  return typeof raw === 'string' && raw ? raw : null
}

/** True while consolidation is in flight (drives the ~5s polling loop). */
export function isDossierRunning(status: string | null | undefined): boolean {
  return !!status && RUNNING_STATUSES.includes(status)
}

/** A dossier is renderable when at least one block or the timestamp exists. */
export function hasDossierContent(dossier: DossierResponse | null | undefined): boolean {
  if (!dossier || typeof dossier !== 'object') return false
  return Boolean(
    dossier.style_block || dossier.structure_block || dossier.world_block || dossier.consolidated_at,
  )
}

/** Error message for a dossier section whose data is `{error: ...}`, else null. */
export function sectionError(data: unknown): string | null {
  if (!data || typeof data !== 'object' || Array.isArray(data)) return null
  const err = (data as Record<string, unknown>).error
  if (err == null) return null
  return typeof err === 'string' ? err : JSON.stringify(err)
}

/** "style 12 · plot 8" caption from a source_counts object (any shape). */
export function formatSourceCounts(counts: unknown): string {
  if (!counts || typeof counts !== 'object' || Array.isArray(counts)) return ''
  return Object.entries(counts as Record<string, unknown>)
    .filter(([, v]) => typeof v === 'number')
    .map(([k, v]) => `${k} ${v}`)
    .join(' · ')
}
