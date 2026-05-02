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
  const [menuOpen, setMenuOpen] = useState(false)
  const t = useT()

  useEffect(() => {
    setAuthenticated(!!getToken())
    setMenuOpen(false)
  }, [pathname])

  if (!authenticated) return null

  function handleLogout() {
    clearToken()
    window.location.href = '/login'
  }

  return (
    <nav className="safe-area-x fixed top-0 left-0 right-0 h-12 bg-white border-b border-gray-200 z-50 flex items-center md:px-4">
      {/* Hamburger (mobile only) */}
      <button
        type="button"
        aria-label="menu"
        aria-expanded={menuOpen}
        onClick={() => setMenuOpen((v) => !v)}
        data-testid="nav-hamburger"
        className="md:hidden mr-2 p-1.5 rounded hover:bg-gray-100 text-gray-700"
      >
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          {menuOpen ? (
            <>
              <line x1="18" y1="6" x2="6" y2="18" />
              <line x1="6" y1="6" x2="18" y2="18" />
            </>
          ) : (
            <>
              <line x1="3" y1="6" x2="21" y2="6" />
              <line x1="3" y1="12" x2="21" y2="12" />
              <line x1="3" y1="18" x2="21" y2="18" />
            </>
          )}
        </svg>
      </button>

      <Link
        href="/workspace"
        className="text-sm md:text-base font-bold text-gray-900 mr-4 md:mr-8 shrink-0"
      >
        {t('app.name')}
      </Link>

      {/* Desktop nav links */}
      <div className="hidden md:flex flex-1 items-center justify-center gap-6">
        {NAV_LINKS.map(({ href, key }) => {
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
              {t(key)}
            </Link>
          )
        })}
      </div>

      {/* Spacer to push right cluster to the edge on mobile */}
      <div className="flex-1 md:hidden" />

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

      {/* Mobile drawer */}
      {menuOpen && (
        <div
          data-testid="nav-mobile-drawer"
          className="safe-area-x md:hidden fixed left-0 right-0 top-12 bg-white border-b border-gray-200 shadow-popover"
        >
          <ul className="flex flex-col py-2">
            {NAV_LINKS.map(({ href, key }) => {
              const isActive = pathname.startsWith(href)
              return (
                <li key={href}>
                  <Link
                    href={href}
                    onClick={() => setMenuOpen(false)}
                    className={`block px-3 py-2 text-sm rounded ${
                      isActive
                        ? 'bg-blue-50 text-blue-600'
                        : 'text-gray-700 hover:bg-gray-50'
                    }`}
                  >
                    {t(key)}
                  </Link>
                </li>
              )
            })}
          </ul>
        </div>
      )}
    </nav>
  )
}
