'use client'

import { useEffect } from 'react'
import { useRouter } from 'next/navigation'
import { getToken } from '@/lib/api'

export default function Home() {
  const router = useRouter()

  useEffect(() => {
    if (getToken()) {
      router.replace('/workspace')
    } else {
      router.replace('/login')
    }
  }, [router])

  return (
    <div className="flex items-center justify-center h-screen">
      <p className="text-gray-400">Redirecting...</p>
    </div>
  )
}
