'use client'

import React from 'react'
import { useT } from '@/lib/i18n/I18nProvider'

export default function SettingsLayout({
  children,
}: {
  children: React.ReactNode
}) {
  const t = useT()
  return (
    <div className="min-h-screen pt-12 bg-gray-50">
      <header className="bg-white border-b border-gray-200">
        <div className="max-w-7xl mx-auto px-6 py-4 flex items-center justify-between">
          <div className="flex items-center gap-4">
            <a href="/" className="text-gray-400 hover:text-gray-600 text-sm">
              {t('nav.home')}
            </a>
            <span className="text-gray-300">/</span>
            <h1 className="text-lg font-semibold text-gray-900">{t('settings.page.title')}</h1>
          </div>
          <div className="flex items-center gap-4">
            <a
              href="/knowledge"
              className="text-sm text-gray-500 hover:text-gray-700"
            >
              {t('nav.knowledge')}
            </a>
            <a
              href="/workspace"
              className="text-sm text-blue-600 hover:text-blue-700"
            >
              {t('nav.workbench')}
            </a>
          </div>
        </div>
      </header>
      <main className="max-w-7xl mx-auto px-6 py-6">{children}</main>
    </div>
  )
}
