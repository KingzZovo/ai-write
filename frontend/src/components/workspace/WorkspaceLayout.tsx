'use client'

import React from 'react'

interface WorkspaceLayoutProps {
  sidebar: React.ReactNode
  editor: React.ReactNode
  panel: React.ReactNode
}

export function WorkspaceLayout({ sidebar, editor, panel }: WorkspaceLayoutProps) {
  return (
    <div className="flex h-screen bg-gray-50">
      <aside className="w-64 border-r border-gray-200 bg-white overflow-y-auto flex-shrink-0">
        {sidebar}
      </aside>
      <main className="flex-1 overflow-y-auto">
        {editor}
      </main>
      <aside className="w-80 border-l border-gray-200 bg-white overflow-y-auto flex-shrink-0">
        {panel}
      </aside>
    </div>
  )
}
