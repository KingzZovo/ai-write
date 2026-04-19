'use client'

import { useEffect, useState } from 'react'
import dynamic from 'next/dynamic'
import { getToken } from '@/lib/api'

const TrashListPage = dynamic(
  () => import('@/components/project/TrashListPage').then((m) => m.TrashListPage),
  { ssr: false }
)

export default function TrashPage() {
  const [ok, setOk] = useState(false)
  useEffect(() => {
    if (!getToken()) {
      window.location.href = '/login'
      return
    }
    setOk(true)
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
