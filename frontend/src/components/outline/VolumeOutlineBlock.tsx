'use client'

import { volumeOutlineContentText } from '@/lib/volumeOutlines'

export function VolumeOutlineBlock({ data }: { data: Record<string, unknown> }) {
  return <pre className="whitespace-pre-wrap text-gray-700 leading-relaxed" style={{ fontFamily: "'Noto Serif SC', serif" }}>
    {volumeOutlineContentText(data)}
  </pre>
}
