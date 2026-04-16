'use client'

import { useEffect } from 'react'
import { getToken } from '@/lib/api'

export default function Home() {
  useEffect(() => {
    // Direct browser redirect — more reliable than Next.js router
    if (getToken()) {
      window.location.href = '/workspace'
    } else {
      window.location.href = '/login'
    }
  }, [])

  return (
    <div className="flex items-center justify-center h-screen bg-gray-50">
      <div className="text-center">
        <h1 className="text-2xl font-bold text-gray-900 mb-2">AI Write</h1>
        <p className="text-gray-400 text-sm">Loading...</p>
      </div>
    </div>
  )
}
