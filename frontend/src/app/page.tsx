'use client'

import { useEffect, useState } from 'react'
import { useRouter } from 'next/navigation'
import { getToken } from '@/lib/api'

export default function Home() {
  const router = useRouter()
  const [checking, setChecking] = useState(true)

  useEffect(() => {
    // Only runs on client side
    const token = getToken()
    if (token) {
      router.replace('/workspace')
    } else {
      router.replace('/login')
    }
    setChecking(false)
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  if (checking) {
    return (
      <div className="flex items-center justify-center h-screen bg-gray-50">
        <div className="text-center">
          <h1 className="text-2xl font-bold text-gray-900 mb-2">AI Write</h1>
          <p className="text-gray-400 text-sm">Loading...</p>
        </div>
      </div>
    )
  }

  return null
}
