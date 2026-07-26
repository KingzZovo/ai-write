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
        className="w-6 h-6 flex items-center justify-center rounded-md text-gray-400 transition-colors duration-150 hover:bg-gray-200 hover:text-gray-600"
        aria-label="more"
      >
        ⋯
      </button>
      {open && (
        <div className="absolute right-0 mt-1 w-28 origin-top-right bg-white border border-gray-200 rounded-lg py-1 shadow-lg ring-1 ring-black/5 overflow-hidden z-20 motion-safe:transition-[opacity,transform] motion-safe:duration-150 motion-safe:starting:scale-95 motion-safe:starting:opacity-0">
          {items.map((it, i) => (
            <button
              key={i}
              onClick={() => { setOpen(false); it.onClick() }}
              className={`block w-full text-left px-3 py-1.5 text-xs transition-colors duration-150 ${it.danger ? 'text-red-600 hover:bg-red-50' : 'text-gray-700 hover:bg-gray-50'}`}
            >
              {it.label}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
