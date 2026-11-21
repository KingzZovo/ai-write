'use client'

import { useState } from 'react'

const record = (value: unknown): value is Record<string, unknown> => !!value && typeof value === 'object' && !Array.isArray(value)

export function PlanningOriginal({ label, text }: { label: string; text: string }) {
  const [open, setOpen] = useState(false)
  return <details className="rounded border p-3" onToggle={event => setOpen(event.currentTarget.open)}>
    <summary className="cursor-pointer font-medium">{label}</summary>
    {open ? <pre className="mt-3 whitespace-pre-wrap break-words font-sans">{text}</pre> : null}
  </details>
}

export function PlanningAttemptInputs({ journal }: { journal: unknown }) {
  const [open, setOpen] = useState(false)
  if (!record(journal) || !Array.isArray(journal.stages) || journal.stages.length === 0) return null
  return <details className="rounded border p-3" onToggle={event => {
    if (event.target === event.currentTarget) setOpen(event.currentTarget.open)
  }}>
    <summary className="cursor-pointer font-medium">查看各轮实际创作材料</summary>
    {open ? <div className="mt-3 space-y-3">{journal.stages.flatMap((stage, stageIndex) => {
      if (!record(stage) || !Array.isArray(stage.attempts)) return []
      return stage.attempts.flatMap((attempt, attemptIndex) => {
        if (!record(attempt) || typeof attempt.input !== 'string' || !attempt.input) return []
        return [<PlanningOriginal key={`${stageIndex}:${attemptIndex}`} text={attempt.input}
          label={`第 ${stageIndex + 1} 阶段 · 第 ${attemptIndex + 1} 次实际输入`} />]
      })
    })}</div> : null}
  </details>
}
