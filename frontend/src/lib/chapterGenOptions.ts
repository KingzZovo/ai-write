// PR-GEN-UX Task 4 (2026-07-26): per-chapter generation options (指令 / 目标
// 字数). Persisted in localStorage keyed by chapter id so they survive right-
// panel tab switches (which unmount GeneratePanel) and page reloads.
// GeneratePanel edits them; DesktopWorkspace reads them when building the
// /api/generate/chapter payload.

export interface ChapterGenOptions {
  userInstruction: string
  targetWords: number | null
}

const KEY_PREFIX = 'chapter_gen_opts:'
const EMPTY: ChapterGenOptions = { userInstruction: '', targetWords: null }

export function getChapterGenOptions(
  chapterId: string | null | undefined,
): ChapterGenOptions {
  if (!chapterId || typeof window === 'undefined') return EMPTY
  try {
    const raw = localStorage.getItem(KEY_PREFIX + chapterId)
    if (!raw) return EMPTY
    const parsed = JSON.parse(raw) as Record<string, unknown>
    const words =
      typeof parsed.targetWords === 'number' && parsed.targetWords > 0
        ? Math.floor(parsed.targetWords)
        : null
    return {
      userInstruction:
        typeof parsed.userInstruction === 'string' ? parsed.userInstruction : '',
      targetWords: words,
    }
  } catch {
    return EMPTY
  }
}

export function saveChapterGenOptions(
  chapterId: string,
  opts: ChapterGenOptions,
): void {
  if (typeof window === 'undefined') return
  try {
    if (!opts.userInstruction.trim() && !opts.targetWords) {
      localStorage.removeItem(KEY_PREFIX + chapterId)
    } else {
      localStorage.setItem(KEY_PREFIX + chapterId, JSON.stringify(opts))
    }
  } catch {
    // Storage full / private mode: options just won't persist.
  }
}
