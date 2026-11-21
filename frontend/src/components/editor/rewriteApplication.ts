export type RewriteOperation = 'condense' | 'expand' | 'restructure' | 'continue' | 'custom'
export interface RewriteSource {
  chapterId: string
  sourceText: string
  start: number
  end: number
  text: string
}

export function applyRewriteCandidate(source: RewriteSource, chapterId: string, currentText: string, candidate: string, operation: RewriteOperation): string | null {
  if (chapterId !== source.chapterId || currentText !== source.sourceText ||
      !Number.isInteger(source.start) || !Number.isInteger(source.end) ||
      source.start < 0 || source.end <= source.start || source.end > currentText.length ||
      currentText.slice(source.start, source.end) !== source.text || !candidate.trim()) return null
  const before = currentText.slice(0, operation === 'continue' ? source.end : source.start)
  return before + candidate + currentText.slice(source.end)
}
