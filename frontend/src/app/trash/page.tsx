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
        <p className="text-gray-400">加载中...</p>
      </div>
    )
  }
  return <TrashListPage />
}
