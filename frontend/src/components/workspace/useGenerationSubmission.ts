'use client'

import { useEffect, useMemo, useSyncExternalStore } from 'react'
import { getGenerationSubmissionSession } from '@/lib/generationSubmission'

const empty = () => null
const noSubscription = () => () => {}

export function useGenerationSubmission(projectId: string | null | undefined) {
  const session = useMemo(() => projectId ? getGenerationSubmissionSession(projectId) : null, [projectId])
  const state = useSyncExternalStore(session?.subscribe ?? noSubscription, session?.getSnapshot ?? empty, empty)
  useEffect(() => session?.attach(), [session])
  return { session, state }
}
