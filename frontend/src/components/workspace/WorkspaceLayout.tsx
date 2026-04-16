'use client'

import React, { useState } from 'react'

interface WorkspaceLayoutProps {
  sidebar: React.ReactNode
  editor: React.ReactNode
  panel: React.ReactNode
  /** Lightweight panel for mobile — only essential controls */
  mobilePanel?: React.ReactNode
}

export function WorkspaceLayout({ sidebar, editor, panel, mobilePanel }: WorkspaceLayoutProps) {
  const [mobileTab, setMobileTab] = useState<'sidebar' | 'editor' | 'panel'>('editor')

  return (
    <>
      {/* Desktop: three-column layout */}
      <div className="hidden md:flex h-screen pt-12 bg-gray-50">
        <aside className="w-64 border-r border-gray-200 bg-white overflow-y-auto flex-shrink-0">
          {sidebar}
        </aside>
        <main className="flex-1 overflow-y-auto">
          {editor}
        </main>
        <aside className="w-96 border-l border-gray-200 bg-white overflow-y-auto flex-shrink-0">
          {panel}
        </aside>
      </div>

      {/* Mobile: tab-based layout */}
      <div className="md:hidden flex flex-col h-screen pt-12 bg-gray-50">
        <div className="flex-1 overflow-y-auto">
          {mobileTab === 'sidebar' && (
            <div className="bg-white min-h-full">{sidebar}</div>
          )}
          {mobileTab === 'editor' && (
            <div className="min-h-full">{editor}</div>
          )}
          {mobileTab === 'panel' && (
            <div className="bg-white min-h-full">{mobilePanel || panel}</div>
          )}
        </div>

        <div className="flex border-t border-gray-200 bg-white safe-area-bottom">
          {([
            { key: 'sidebar' as const, label: '目录', icon: '📁' },
            { key: 'editor' as const, label: '编辑', icon: '✏️' },
            { key: 'panel' as const, label: '工具', icon: '⚙️' },
          ]).map((tab) => (
            <button
              key={tab.key}
              onClick={() => setMobileTab(tab.key)}
              className={`flex-1 py-2.5 text-center text-xs font-medium transition-colors ${
                mobileTab === tab.key
                  ? 'text-blue-600 bg-blue-50'
                  : 'text-gray-500'
              }`}
            >
              <div className="text-base mb-0.5">{tab.icon}</div>
              {tab.label}
            </button>
          ))}
        </div>
      </div>
    </>
  )
}
