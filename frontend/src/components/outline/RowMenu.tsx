'use client'

import React, { useEffect, useRef, useState } from 'react'

export interface MenuItem {
  label: string
  onClick: () => void
  danger?: boolean
}

export function RowMenu({ items }: { items: MenuItem[] }) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [open])

  return (
    <div ref={ref} className="relative" onClick={(e) => e.stopPropagation()}>
      <button
        onClick={() => setOpen((v) => !v)}
        className="w-6 h-6 flex items-center justify-center rounded hover:bg-gray-200 text-gray-500"
        aria-label="more"
      >
        ⋯
      </button>
      {open && (
        <div className="absolute right-0 mt-1 w-24 bg-white border border-gray-200 rounded-lg shadow-lg overflow-hidden z-20">
          {items.map((it, i) => (
            <button
              key={i}
              onClick={() => { setOpen(false); it.onClick() }}
              className={`block w-full text-left px-3 py-1.5 text-xs hover:bg-gray-50 ${it.danger ? 'text-red-600 hover:bg-red-50' : ''}`}
            >
              {it.label}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
