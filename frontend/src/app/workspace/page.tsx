'use client'

import dynamic from 'next/dynamic'
import { Component, Suspense, useEffect, useState } from 'react'
import type { ReactNode } from 'react'

// Error boundary to catch runtime crashes
class ErrorBoundary extends Component<{children: ReactNode}, {error: string | null}> {
  constructor(props: {children: ReactNode}) {
    super(props)
    this.state = { error: null }
  }
  static getDerivedStateFromError(error: Error) {
    return { error: error.message }
  }
  render() {
    if (this.state.error) {
      return (
        <div className="flex items-center justify-center h-screen pt-12 bg-gray-50">
          <div className="text-center max-w-md p-6">
            <h2 className="text-lg font-bold text-red-600 mb-2">工作区加载失败</h2>
            <p className="text-sm text-gray-500 mb-4">{this.state.error}</p>
            <button onClick={() => window.location.reload()}
              className="px-4 py-2 bg-blue-600 text-white rounded-lg text-sm">
              刷新页面
            </button>
          </div>
        </div>
      )
    }
    return this.props.children
  }
}

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

  return (
    <Suspense fallback={
      <div className="flex items-center justify-center h-screen pt-12 bg-gray-50">
        <p className="text-gray-400">加载工作区...</p>
      </div>
    }>
      <ErrorBoundary>
        {mobile ? <MobileWorkspace /> : <DesktopWorkspace />}
      </ErrorBoundary>
    </Suspense>
  )
}
