'use client'

import { useEffect, useState } from 'react'
import Link from 'next/link'
import { usePathname } from 'next/navigation'
import { getToken, clearToken } from '@/lib/api'
import { useT } from '@/lib/i18n/I18nProvider'
import { LocaleSwitcher } from '@/components/LocaleSwitcher'
import type { MessageKey } from '@/lib/i18n/messages'

const NAV_LINKS: Array<{ href: string; key: MessageKey }> = [
  { href: '/workspace', key: 'nav.workspace' },
  { href: '/knowledge', key: 'nav.knowledge' },
  { href: '/styles', key: 'nav.styles' },
  { href: '/filter-words', key: 'nav.filterWords' },
  { href: '/prompts', key: 'nav.prompts' },
  { href: '/settings', key: 'nav.settings' },
]

export function Navbar() {
  const pathname = usePathname()
  const [authenticated, setAuthenticated] = useState(false)
  const t = useT()

  useEffect(() => {
    setAuthenticated(!!getToken())
  }, [pathname])

  if (!authenticated) return null

  function handleLogout() {
    clearToken()
    window.location.href = '/login'
  }

  return (
    <nav className="fixed top-0 left-0 right-0 h-12 bg-white border-b border-gray-200 z-50 flex items-center px-3 md:px-4">
      <Link
        href="/workspace"
        className="text-sm md:text-base font-bold text-gray-900 mr-4 md:mr-8 shrink-0"
      >
        {t('app.name')}
      </Link>

      <div className="flex-1 flex items-center justify-center gap-3 md:gap-6">
        {NAV_LINKS.map(({ href, key }) => {
          const isActive = pathname.startsWith(href)
          return (
            <Link
              key={href}
              href={href}
              className={`text-xs md:text-sm font-medium transition-colors ${
                isActive
                  ? 'text-blue-600'
                  : 'text-gray-600 hover:text-gray-900'
              }`}
            >
              {t(key)}
            </Link>
          )
        })}
      </div>

      <div className="flex items-center gap-2 md:gap-3 shrink-0">
        <LocaleSwitcher />
        <span className="text-xs md:text-sm text-gray-600 hidden sm:inline">king</span>
        <button
          onClick={handleLogout}
          className="text-xs md:text-sm text-gray-500 hover:text-red-600 transition-colors"
        >
          {t('auth.logout')}
        </button>
      </div>
    </nav>
  )
}
