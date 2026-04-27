'use client'

/**
 * v1.7 X5: standalone /cascade-tasks page.
 *
 * Reads ?project_id=... (and optional ?chapter_id=...) from the URL
 * and renders the CascadeTasksPanel against the v1.7 X5 read-only API.
 *
 * Kept as a standalone route (not mounted inside /workspace) to avoid
 * coupling with the existing workspace shell while the panel is still
 * iterating. Operators can drop in via
 *   /cascade-tasks?project_id=<uuid>
 * to inspect upstream-fix queue state.
 */

import { Suspense } from 'react'
import { useSearchParams } from 'next/navigation'
import { CascadeTasksPanel } from '@/components/panels/CascadeTasksPanel'

function CascadeTasksContent() {
  const searchParams = useSearchParams()
  const projectId = searchParams?.get('project_id') || ''
  const chapterId = searchParams?.get('chapter_id') || undefined

  if (!projectId) {
    return (
      <div className="max-w-3xl mx-auto p-6">
        <h1 className="text-lg font-semibold text-gray-900 mb-2">Cascade Tasks</h1>
        <p className="text-sm text-gray-600">
          Provide <code className="bg-gray-100 px-1 rounded">?project_id=&lt;uuid&gt;</code>{' '}
          in the URL to view cascade task queue state.
        </p>
      </div>
    )
  }

  return (
    <div className="max-w-6xl mx-auto p-6">
      <CascadeTasksPanel projectId={projectId} chapterId={chapterId} />
    </div>
  )
}

export default function CascadeTasksPage() {
  return (
    <Suspense fallback={<p className="p-6 text-sm text-gray-500">Loading…</p>}>
      <CascadeTasksContent />
    </Suspense>
  )
}
