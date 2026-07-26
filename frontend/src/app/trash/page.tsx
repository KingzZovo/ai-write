'use client'

import { useEffect, useSyncExternalStore } from 'react'
import dynamic from 'next/dynamic'
import { getToken } from '@/lib/api'

const emptySubscribe = () => () => {}

const TrashListPage = dynamic(
  () => import('@/components/project/TrashListPage').then((m) => m.TrashListPage),
  { ssr: false }
)

export default function TrashPage() {
  // Server snapshot (false) keeps the prerendered loading HTML; the client
  // snapshot reads the token so no effect-driven setState is needed.
  const ok = useSyncExternalStore(emptySubscribe, () => !!getToken(), () => false)
  useEffect(() => {
    if (!getToken()) window.location.href = '/login'
  }, [])
  if (!ok) {
    return (
      <div className="flex items-center justify-center h-screen pt-12 bg-gray-50">
        <p className="flex items-center gap-2 text-sm text-gray-400 animate-fade-in">
          <span
            aria-hidden
            className="w-4 h-4 rounded-full border-2 border-gray-300 border-t-brand-500 animate-spin"
          />
          加载中...
        </p>
      </div>
    )
  }
  return <TrashListPage />
}
