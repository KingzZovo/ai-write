'use client'

import { useEffect, useSyncExternalStore } from 'react'
import dynamic from 'next/dynamic'
import { getToken } from '@/lib/api'

const emptySubscribe = () => () => {}

const ProjectListPage = dynamic(
  () => import('@/components/project/ProjectListPage').then((m) => m.ProjectListPage),
  {
    ssr: false,
    loading: () => (
      <div className="flex items-center justify-center h-screen pt-12 bg-gray-50">
        <p className="text-gray-400">加载中...</p>
      </div>
    ),
  }
)

export default function Home() {
  // Server snapshot (false) keeps the prerendered loading HTML; the client
  // snapshot reads the token so no effect-driven setState is needed.
  const checked = useSyncExternalStore(emptySubscribe, () => !!getToken(), () => false)
  useEffect(() => {
    if (!getToken()) window.location.href = '/login'
  }, [])
  if (!checked) {
    return (
      <div className="flex items-center justify-center h-screen bg-gray-50">
        <div className="text-center">
          <h1 className="text-2xl font-bold text-gray-900 mb-2">AI Write</h1>
          <p className="text-gray-400 text-sm">加载中...</p>
        </div>
      </div>
    )
  }
  return <ProjectListPage />
}
