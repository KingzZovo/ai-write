import { apiFetch } from '@/lib/api'

/**
 * Calculates a simple diff ratio between two strings.
 * Returns a value between 0 and 1 representing the proportion of change.
 */
function diffRatio(a: string, b: string): number {
  if (a === b) return 0
  if (a.length === 0 || b.length === 0) return 1
  const maxLen = Math.max(a.length, b.length)
  const lenDiff = Math.abs(a.length - b.length)
  // Simple heuristic: compare length difference plus character-level differences
  const minLen = Math.min(a.length, b.length)
  let charDiffs = 0
  for (let i = 0; i < minLen; i++) {
    if (a[i] !== b[i]) charDiffs++
  }
  return (lenDiff + charDiffs) / maxLen
}

class SyncManager {
  private debounceTimer: ReturnType<typeof setTimeout> | null = null
  private lastSyncedText: string = ''
  private syncing: boolean = false

  /**
   * Called on every editor change. Debounces 3 seconds, then syncs
   * if text changed significantly (> 5% diff from last synced text).
   */
  onContentChange(chapterId: string, newText: string): void {
    if (this.debounceTimer) {
      clearTimeout(this.debounceTimer)
    }

    this.debounceTimer = setTimeout(() => {
      this.maybeSync(chapterId, newText)
    }, 3000)
  }

  private async maybeSync(chapterId: string, newText: string): Promise<void> {
    if (this.syncing) return

    const ratio = diffRatio(this.lastSyncedText, newText)
    if (ratio <= 0.05) return // Less than 5% change, skip

    this.syncing = true
    try {
      // Extract project ID from the chapter path or use a stored reference.
      // The sync endpoint uses the chapter ID directly.
      await apiFetch(`/api/chapters/${chapterId}/sync`, {
        method: 'POST',
        body: JSON.stringify({ content: newText }),
      })
      this.lastSyncedText = newText
    } catch (err) {
      console.error('Sync failed:', err)
    } finally {
      this.syncing = false
    }
  }

  /**
   * Resets the sync manager state, e.g. when switching chapters.
   */
  reset(initialText: string = ''): void {
    if (this.debounceTimer) {
      clearTimeout(this.debounceTimer)
      this.debounceTimer = null
    }
    this.lastSyncedText = initialText
    this.syncing = false
  }
}

export const syncManager = new SyncManager()
