'use client'

import dynamic from 'next/dynamic'
import { useEffect, useState } from 'react'

const DesktopWorkspace = dynamic(() => import('@/components/workspace/DesktopWorkspace'), {
  ssr: false,
  loading: () => (
    <div className="flex items-center justify-center h-screen pt-12 bg-gray-50">
      <p className="text-gray-400">加载工作区...</p>
    </div>
  ),
})

const MobileWorkspace = dynamic(() => import('@/components/workspace/MobileWorkspace'), {
  ssr: false,
  loading: () => (
    <div className="flex items-center justify-center h-screen pt-12 bg-gray-50">
      <p className="text-gray-400">加载工作区...</p>
    </div>
  ),
})

export default function WorkspacePage() {
  const [ready, setReady] = useState(false)
  const [mobile, setMobile] = useState(false)

  useEffect(() => {
    setMobile(window.innerWidth < 768)
    setReady(true)
  }, [])

  if (!ready) {
    return (
      <div className="flex items-center justify-center h-screen pt-12 bg-gray-50">
        <p className="text-gray-400">加载工作区...</p>
      </div>
    )
  }

  return mobile ? <MobileWorkspace /> : <DesktopWorkspace />
}
