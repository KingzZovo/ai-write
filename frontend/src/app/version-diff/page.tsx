'use client'

import { Suspense } from 'react'
import { useSearchParams } from 'next/navigation'
import { VersionDiffView } from '@/components/versions/VersionDiffView'

function DiffInner() {
  const chapterId = useSearchParams()?.get('chapter_id') || ''
  return chapterId ? <VersionDiffView chapterId={chapterId} /> : <p className="p-8 text-sm text-gray-500">请从作品的版本历史打开对比页。</p>
}

export default function VersionDiffPage() {
  return <Suspense fallback={<p className="p-8 text-sm text-gray-400">加载…</p>}><DiffInner /></Suspense>
}
