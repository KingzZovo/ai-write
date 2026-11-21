/** Helpers for persisting style references into project settings.
 *
 * Distillation rework: when a book-bound style profile is selected, the
 * project settings should also carry the profile's reference book id so the
 * generation pipeline can resolve the book's structure / world dossiers even
 * if the backend resolution chain changes. Belt-and-suspenders with the
 * backend fix that auto-derives the book from `style_profile_id`.
 */

export interface StyleBindInfo {
  bind_level?: string | null
  bind_target_id?: string | null
}

/**
 * Reference book id for a style profile, or null when the profile is not
 * book-bound (global/chapter binds, missing bind fields, or no profile).
 */
export function styleReferenceBookId(profile: StyleBindInfo | null | undefined): string | null {
  if (!profile) return null
  if (profile.bind_level !== 'book') return null
  return typeof profile.bind_target_id === 'string' && profile.bind_target_id ? profile.bind_target_id : null
}
