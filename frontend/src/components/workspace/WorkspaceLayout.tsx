'use client'

import React, { useCallback, useEffect, useState } from 'react'
import { useT } from '@/lib/i18n/I18nProvider'

interface WorkspaceLayoutProps {
  sidebar: React.ReactNode
  editor: React.ReactNode
  panel: React.ReactNode
  /** Lightweight panel for mobile -- only essential controls */
  mobilePanel?: React.ReactNode
  /**
   * When provided, sidebar/panel collapse state is persisted per-project.
   * chunk-23: different projects can remember different layouts; omitted
   * projectId falls back to the shared global key for backward compat.
   */
  projectId?: string
}

const LS_SIDEBAR_BASE = 'ai-write.workspace.sidebar-collapsed'
const LS_PANEL_BASE = 'ai-write.workspace.panel-collapsed'

/** chunk-23: compose per-project key when projectId is provided. */
function composeKey(base: string, projectId?: string): string {
  if (projectId && projectId.length > 0) return `${base}:${projectId}`
  return base
}

function usePersistedFlag(key: string, defaultValue = false) {
  const [value, setValue] = useState<boolean>(defaultValue)

  useEffect(() => {
    try {
      const raw = window.localStorage.getItem(key)
      if (raw === '1') setValue(true)
      else if (raw === '0') setValue(false)
      else setValue(defaultValue)
    } catch {
      /* ignore */
    }
  }, [key, defaultValue])

  const set = useCallback(
    (next: boolean) => {
      setValue(next)
      try {
        window.localStorage.setItem(key, next ? '1' : '0')
      } catch {
        /* ignore */
      }
    },
    [key]
  )

  return [value, set] as const
}

export function WorkspaceLayout({ sidebar, editor, panel, mobilePanel, projectId }: WorkspaceLayoutProps) {
  const t = useT()
  const [mobileTab, setMobileTab] = useState<'sidebar' | 'editor' | 'panel'>('editor')

  const sidebarKey = composeKey(LS_SIDEBAR_BASE, projectId)
  const panelKey = composeKey(LS_PANEL_BASE, projectId)
  const [sidebarCollapsed, setSidebarCollapsed] = usePersistedFlag(sidebarKey)
  const [panelCollapsed, setPanelCollapsed] = usePersistedFlag(panelKey)

  // chunk-23: auto-collapse sidebar + panel when viewport is below md breakpoint (768px).
  // Only flips to collapsed on narrow screens; never auto-expands on growth so users keep agency.
  useEffect(() => {
    if (typeof window === 'undefined' || !window.matchMedia) return
    const mq = window.matchMedia('(max-width: 767px)')
    const apply = (matches: boolean) => {
      if (matches) {
        setSidebarCollapsed(true)
        setPanelCollapsed(true)
      }
    }
    apply(mq.matches)
    const handler = (e: MediaQueryListEvent) => apply(e.matches)
    mq.addEventListener('change', handler)
    return () => mq.removeEventListener('change', handler)
  }, [setSidebarCollapsed, setPanelCollapsed])

  // chunk-23: keyboard shortcuts. [ toggles sidebar; ] toggles panel.
  // Ignored when focus is inside an editable surface so typing square
  // brackets in the editor is unaffected.
  useEffect(() => {
    if (typeof window === 'undefined') return
    const handler = (e: KeyboardEvent) => {
      if (e.metaKey || e.ctrlKey || e.altKey) return
      const t2 = e.target as HTMLElement | null
      if (t2) {
        const tag = t2.tagName
        if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return
        if (t2.isContentEditable) return
      }
      if (e.key === '[') {
        e.preventDefault()
        setSidebarCollapsed(!sidebarCollapsed)
      } else if (e.key === ']') {
        e.preventDefault()
        setPanelCollapsed(!panelCollapsed)
      }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [sidebarCollapsed, panelCollapsed, setSidebarCollapsed, setPanelCollapsed])

  return (
    <>
      {/* Desktop: three-column layout with collapsible side panels */}
      <div className="hidden md:flex h-screen pt-12 bg-gray-50" data-testid="workspace-desktop">
        {sidebarCollapsed ? (
          <button
            type="button"
            onClick={() => setSidebarCollapsed(false)}
            aria-label={t('workspace.sidebar.expand')}
            title={`${t('workspace.sidebar.expand')} ([)`}
            data-testid="workspace-sidebar-toggle"
            className="w-6 border-r border-gray-200 bg-white hover:bg-gray-50 flex items-center justify-center text-gray-500"
          >
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="9 18 15 12 9 6" /></svg>
          </button>
        ) : (
          <aside className="relative w-64 border-r border-gray-200 bg-white overflow-y-auto flex-shrink-0" data-testid="workspace-sidebar">
            <button
              type="button"
              onClick={() => setSidebarCollapsed(true)}
              aria-label={t('workspace.sidebar.collapse')}
              title={`${t('workspace.sidebar.collapse')} ([)`}
              data-testid="workspace-sidebar-toggle"
              className="absolute top-2 right-2 p-1 rounded hover:bg-gray-100 text-gray-400 hover:text-gray-700 z-10"
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="15 18 9 12 15 6" /></svg>
            </button>
            {sidebar}
          </aside>
        )}

        <main className="flex-1 overflow-y-auto min-w-0">
          {editor}
        </main>

        {panelCollapsed ? (
          <button
            type="button"
            onClick={() => setPanelCollapsed(false)}
            aria-label={t('workspace.panel.expand')}
            title={`${t('workspace.panel.expand')} (])`}
            data-testid="workspace-panel-toggle"
            className="w-6 border-l border-gray-200 bg-white hover:bg-gray-50 flex items-center justify-center text-gray-500"
          >
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="15 18 9 12 15 6" /></svg>
          </button>
        ) : (
          <aside className="relative w-96 border-l border-gray-200 bg-white overflow-y-auto flex-shrink-0" data-testid="workspace-panel">
            <button
              type="button"
              onClick={() => setPanelCollapsed(true)}
              aria-label={t('workspace.panel.collapse')}
              title={`${t('workspace.panel.collapse')} (])`}
              data-testid="workspace-panel-toggle"
              className="absolute top-2 left-2 p-1 rounded hover:bg-gray-100 text-gray-400 hover:text-gray-700 z-10"
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="9 18 15 12 9 6" /></svg>
            </button>
            {panel}
          </aside>
        )}
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
            { key: 'sidebar' as const, labelKey: 'workspace.tab.sidebar' as const, icon: '\uD83D\uDCC1' },
            { key: 'editor' as const, labelKey: 'workspace.tab.editor' as const, icon: '\u270F\uFE0F' },
            { key: 'panel' as const, labelKey: 'workspace.tab.panel' as const, icon: '\u2699\uFE0F' },
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
              {t(tab.labelKey)}
            </button>
          ))}
        </div>
      </div>
    </>
  )
}
