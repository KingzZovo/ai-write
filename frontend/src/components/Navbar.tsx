'use client'

import { useEffect, useState } from 'react'
import Link from 'next/link'
import { usePathname, useRouter } from 'next/navigation'
import { getToken, clearToken } from '@/lib/api'

const NAV_LINKS = [
  { href: '/workspace', label: '工作区' },
  { href: '/knowledge', label: '知识库' },
  { href: '/settings', label: '设置' },
]

export function Navbar() {
  const pathname = usePathname()
  const router = useRouter()
  const [authenticated, setAuthenticated] = useState(false)

  useEffect(() => {
    setAuthenticated(!!getToken())
  }, [pathname])

  if (!authenticated) return null

  function handleLogout() {
    clearToken()
    router.push('/login')
  }

  return (
    <nav className="fixed top-0 left-0 right-0 h-12 bg-white border-b border-gray-200 z-50 flex items-center px-4">
      <Link
        href="/workspace"
        className="text-base font-bold text-gray-900 mr-8 shrink-0"
      >
        AI Write
      </Link>

      <div className="flex-1 flex items-center justify-center gap-6">
        {NAV_LINKS.map(({ href, label }) => {
          const isActive = pathname.startsWith(href)
          return (
            <Link
              key={href}
              href={href}
              className={`text-sm font-medium transition-colors ${
                isActive
                  ? 'text-blue-600'
                  : 'text-gray-600 hover:text-gray-900'
              }`}
            >
              {label}
            </Link>
          )
        })}
      </div>

      <div className="flex items-center gap-3 shrink-0">
        <span className="text-sm text-gray-600">king</span>
        <button
          onClick={handleLogout}
          className="text-sm text-gray-500 hover:text-red-600 transition-colors"
        >
          登出
        </button>
      </div>
    </nav>
  )
}
