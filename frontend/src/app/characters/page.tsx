'use client'

import { Suspense, useEffect, useState } from 'react'
import { useSearchParams } from 'next/navigation'
import { apiFetch } from '@/lib/api'
import { useT } from '@/lib/i18n/I18nProvider'
import { CharacterProfileEditor, type EditableCharacter } from '@/components/panels/CharacterProfileEditor'

function CharactersPageInner() {
  const searchParams = useSearchParams()
  const projectId = searchParams?.get('id') || ''
  return <CharactersProject key={projectId} projectId={projectId} />
}

function CharactersProject({ projectId }: { projectId: string }) {
  const t = useT()
  const [chars, setChars] = useState<EditableCharacter[]>([])
  const [selected, setSelected] = useState<EditableCharacter | null>(null)
  const [loading, setLoading] = useState(!!projectId)
  const [error, setError] = useState('')
  const [reload, setReload] = useState(0)
  useEffect(() => {
    if (!projectId) return
    const controller = new AbortController()
    let stopped = false
    setLoading(true)
    setError('')
    void apiFetch<{ characters: EditableCharacter[] }>(`/api/projects/${projectId}/characters`, { signal: controller.signal })
      .then(data => {
        if (stopped) return
        if (!Array.isArray(data.characters)) throw new Error('人物列表响应不完整。')
        setChars(data.characters)
        setSelected(previous => previous ? data.characters.find(c => c.name === previous.name) ?? null : data.characters[0] ?? null)
      })
      .catch(e => { if (!stopped) setError(e instanceof Error ? e.message : '读取失败。') })
      .finally(() => { if (!stopped) setLoading(false) })
    return () => { stopped = true; controller.abort() }
  }, [projectId, reload])

  if (!projectId) return <div className="p-8 text-sm text-gray-500">请先选择作品，再打开人物设定。</div>
  return <div className="flex h-screen pt-12">
    <aside className="w-64 overflow-y-auto border-r bg-gray-50">
      <div className="border-b p-3 text-sm font-semibold">角色列表（{chars.length}）</div>
      {loading && <div className="p-3 text-xs text-gray-400">加载中…</div>}
      <ul>{chars.map(c => <li key={c.id}>
        <button onClick={() => setSelected(c)} className={`w-full px-3 py-2 text-left text-sm hover:bg-blue-50 ${selected?.name === c.name ? 'bg-blue-100 font-medium' : ''}`}>{c.name}</button>
      </li>)}</ul>
    </aside>
    <main className="flex-1 overflow-y-auto p-6">
      {error && <div role="alert" className="mb-4 rounded bg-red-50 p-3 text-sm text-red-700">{error}
        <button onClick={() => setReload(value => value + 1)} className="ml-2 text-blue-600">重试读取</button>
      </div>}
      {selected ? <div className="max-w-3xl">
        <h1 className="mb-2 text-2xl font-bold">{selected.name}</h1>
        <p className="mb-4 text-xs text-gray-400">{t('character.nameLocked')}</p>
        <CharacterProfileEditor projectId={projectId} character={selected} json onSaved={saved => {
          setChars(previous => previous.map(c => c.name === saved.name ? saved : c))
          setSelected(previous => previous?.name === saved.name ? saved : previous)
        }} />
      </div> : <div className="text-sm text-gray-400">请选择左侧角色</div>}
    </main>
  </div>
}

export default function CharactersPage() {
  return <Suspense fallback={<div className="p-8 text-sm text-gray-400">加载…</div>}><CharactersPageInner /></Suspense>
}
