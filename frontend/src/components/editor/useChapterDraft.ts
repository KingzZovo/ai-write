'use client'

import { useEffect, useMemo, useSyncExternalStore } from 'react'
import { getChapterDraftSession, hasPendingChapterDrafts, preserveChapterDrafts } from './chapterDraftSession'

const emptySnapshot = () => null
const noSubscription = () => () => {}

export function useChapterDraft(projectId: string | undefined, chapterId: string | null) {
  const session = useMemo(() => projectId && chapterId ? getChapterDraftSession(projectId, chapterId) : null, [projectId, chapterId])
  const state = useSyncExternalStore(session?.subscribe ?? noSubscription, session?.getSnapshot ?? emptySnapshot, emptySnapshot)

  useEffect(() => session?.attach(), [session])
  useEffect(() => {
    const beforeUnload = (event: BeforeUnloadEvent) => {
      if (!hasPendingChapterDrafts()) return
      preserveChapterDrafts()
      event.preventDefault()
      event.returnValue = ''
    }
    window.addEventListener('beforeunload', beforeUnload)
    return () => window.removeEventListener('beforeunload', beforeUnload)
  }, [])

  return { session, state }
}
