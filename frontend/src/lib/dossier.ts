/** Pure helpers for the per-reference-book "book dossier" feature.
 *
 * Backend contract (distillation rework):
 * - POST /api/decompile/{book_id}/consolidate  → 202, starts consolidation
 * - GET  /api/decompile/{book_id}/dossier      → {book_id, status, dossier}
 *   (404 while the book is absent; `dossier` is null until done)
 * - reference_books.metadata_json.dossier_status tracks progress. It is an
 *   OBJECT marker: {state, updated_at, llm_calls?, error?} — older backends
 *   stored a plain string, so both shapes are accepted.
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

/** Normalized dossier_status marker. */
export interface DossierStatusInfo {
  state: string
  updatedAt: string | null
  message: string | null
}

const RUNNING_STATUSES = ['pending', 'queued', 'running', 'consolidating']
const ERROR_STATUSES = ['error', 'failed']

/** Normalize a dossier_status marker: plain string OR {state, updated_at, error}. */
export function parseDossierStatus(raw: unknown): DossierStatusInfo | null {
  if (typeof raw === 'string') {
    return raw ? { state: raw, updatedAt: null, message: null } : null
  }
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null
  const obj = raw as Record<string, unknown>
  const state =
    typeof obj.state === 'string' && obj.state
      ? obj.state
      : typeof obj.status === 'string' && obj.status
        ? obj.status
        : null
  if (!state) return null
  const err = obj.error ?? obj.message
  return {
    state,
    updatedAt: typeof obj.updated_at === 'string' && obj.updated_at ? obj.updated_at : null,
    message: typeof err === 'string' && err ? err : err != null ? JSON.stringify(err) : null,
  }
}

/** Full `dossier_status` marker out of a book's metadata_json (any shape). */
export function getDossierStatusInfo(metadata: unknown): DossierStatusInfo | null {
  if (!metadata || typeof metadata !== 'object' || Array.isArray(metadata)) return null
  return parseDossierStatus((metadata as Record<string, unknown>).dossier_status)
}

/** State string of `dossier_status` out of a book's metadata_json (any shape). */
export function getDossierStatus(metadata: unknown): string | null {
  return getDossierStatusInfo(metadata)?.state ?? null
}

/** True while consolidation is in flight (drives the ~5s polling loop). */
export function isDossierRunning(status: string | null | undefined): boolean {
  return !!status && RUNNING_STATUSES.includes(status)
}

/** True for terminal failure states. */
export function isDossierError(status: string | null | undefined): boolean {
  return !!status && ERROR_STATUSES.includes(status)
}

/** A dossier is renderable when at least one block or the timestamp exists. */
export function hasDossierContent(dossier: DossierResponse | null | undefined): boolean {
  if (!dossier || typeof dossier !== 'object') return false
  return Boolean(
    dossier.style_block || dossier.structure_block || dossier.world_block || dossier.consolidated_at,
  )
}

export interface ParsedDossierResponse {
  status: DossierStatusInfo | null
  dossier: DossierResponse | null
}

/** Parse a GET dossier payload: {book_id?, status, dossier} envelope or a bare
 *  dossier (legacy). `dossier` is null unless it has renderable content. */
export function parseDossierResponse(resp: unknown): ParsedDossierResponse {
  if (!resp || typeof resp !== 'object' || Array.isArray(resp)) {
    return { status: null, dossier: null }
  }
  const obj = resp as Record<string, unknown>
  if ('dossier' in obj || 'status' in obj) {
    const inner =
      obj.dossier && typeof obj.dossier === 'object' && !Array.isArray(obj.dossier)
        ? (obj.dossier as DossierResponse)
        : null
    return {
      status: parseDossierStatus(obj.status),
      dossier: hasDossierContent(inner) ? inner : null,
    }
  }
  const bare = obj as DossierResponse
  return { status: null, dossier: hasDossierContent(bare) ? bare : null }
}

/** "45s" / "2m 5s" elapsed since an ISO timestamp; '' when unparsable. */
export function formatElapsed(fromIso: string | null | undefined, now: number = Date.now()): string {
  if (!fromIso) return ''
  const t = new Date(fromIso).getTime()
  if (Number.isNaN(t)) return ''
  const secs = Math.max(0, Math.floor((now - t) / 1000))
  if (secs < 60) return `${secs}s`
  const m = Math.floor(secs / 60)
  const s = secs % 60
  return s > 0 ? `${m}m ${s}s` : `${m}m`
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

/** Number of source books recorded in an (author) dossier's source_counts. */
export function sourceBooksCount(dossier: DossierResponse | null | undefined): number | null {
  const counts = dossier?.source_counts
  if (!counts || typeof counts !== 'object' || Array.isArray(counts)) return null
  const n = (counts as Record<string, unknown>).books
  return typeof n === 'number' ? n : null
}
