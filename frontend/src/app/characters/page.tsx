'use client'

import { Suspense, useCallback, useEffect, useRef, useState } from 'react'
import { useSearchParams } from 'next/navigation'
import { apiFetch } from '@/lib/api'
import { useT } from '@/lib/i18n/I18nProvider'

type Character = {
  id: string
  project_id: string
  name: string
  profile_json: Record<string, unknown> | null
  created_at: string
}

type CharacterListResponse = { characters: Character[]; total: number }

function CharactersPageInner() {
  const t = useT()
  const searchParams = useSearchParams()
  const projectId = searchParams?.get('id') || ''
  const [chars, setChars] = useState<Character[]>([])
  const [selected, setSelected] = useState<Character | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [draft, setDraft] = useState<string>('')
  const refreshTimer = useRef<number | null>(null)

  const load = useCallback(async () => {
    if (!projectId) return
    setLoading(true)
    setError(null)
    try {
      const data = await apiFetch<CharacterListResponse>(
        `/api/projects/${projectId}/characters`,
      )
      setChars(data.characters)
      if (!selected && data.characters[0]) {
        setSelected(data.characters[0])
        setDraft(JSON.stringify(data.characters[0].profile_json ?? {}, null, 2))
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [projectId, selected])

  useEffect(() => {
    load()
  }, [load])

  useEffect(() => {
    return () => {
      if (refreshTimer.current !== null) window.clearTimeout(refreshTimer.current)
    }
  }, [])

  const pick = (c: Character) => {
    setSelected(c)
    setDraft(JSON.stringify(c.profile_json ?? {}, null, 2))
  }

  const save = async () => {
    if (!selected) return
    let parsed: Record<string, unknown>
    try {
      parsed = JSON.parse(draft)
    } catch {
      setError('profile_json 不是合法 JSON')
      return
    }
    try {
      // v1.9+: PG character writes are disabled (410). The sanctioned path
      // writes Neo4j (merged by name) and re-materializes PG (202 accepted).
      await apiFetch(`/api/projects/${projectId}/neo4j-settings/characters`, {
        method: 'POST',
        body: JSON.stringify({ name: selected.name, profile_json: parsed }),
      })
      // Optimistic local update; PG projection catches up shortly after.
      const updated = { ...selected, profile_json: parsed }
      setSelected(updated)
      setChars((xs) => xs.map((c) => (c.id === updated.id ? updated : c)))
      setNotice(t('character.saveSubmitted'))
      if (refreshTimer.current !== null) window.clearTimeout(refreshTimer.current)
      refreshTimer.current = window.setTimeout(async () => {
        try {
          const data = await apiFetch<CharacterListResponse>(
            `/api/projects/${projectId}/characters`,
          )
          setChars(data.characters)
          // Re-materialization may re-create PG rows, so match by name not id.
          const cur = data.characters.find((c) => c.name === updated.name)
          if (cur) setSelected(cur)
        } catch {
          /* keep optimistic state */
        } finally {
          setNotice(null)
        }
      }, 1500)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  if (!projectId) {
    return (
      <div className="p-8 text-sm text-gray-500">
        请在 URL 提供 <code>?id=&lt;projectId&gt;</code>
      </div>
    )
  }

  return (
    <div className="flex h-screen pt-12">
      <aside className="w-64 border-r bg-gray-50 overflow-y-auto">
        <div className="p-3 border-b font-semibold text-sm">
          角色列表（{chars.length}）
        </div>
        {loading && <div className="p-3 text-xs text-gray-400">加载中…</div>}
        <ul>
          {chars.map((c) => (
            <li key={c.id}>
              <button
                onClick={() => pick(c)}
                className={`w-full text-left px-3 py-2 text-sm hover:bg-blue-50 ${
                  selected?.id === c.id ? 'bg-blue-100 font-medium' : ''
                }`}
              >
                {c.name}
              </button>
            </li>
          ))}
        </ul>
      </aside>
      <main className="flex-1 overflow-y-auto p-6">
        {error && (
          <div className="mb-4 p-3 bg-red-50 text-red-700 text-sm rounded">
            {error}
          </div>
        )}
        {notice && (
          <div className="mb-4 p-3 bg-emerald-50 text-emerald-700 text-sm rounded">
            {notice}
          </div>
        )}
        {selected ? (
          <div className="max-w-3xl">
            <h1 className="text-2xl font-bold mb-2">{selected.name}</h1>
            <div className="text-xs text-gray-500 mb-1">
              ID: {selected.id} · 创建于{' '}
              {new Date(selected.created_at).toLocaleString()}
            </div>
            <div className="text-xs text-gray-400 mb-6">
              {t('character.nameLocked')}
            </div>
            <section className="mb-6">
              <h2 className="text-sm font-semibold mb-2">Profile JSON</h2>
              <textarea
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                className="w-full h-96 font-mono text-xs border rounded p-3"
                spellCheck={false}
              />
              <div className="mt-2 flex gap-2">
                <button
                  onClick={save}
                  className="px-4 py-2 bg-blue-600 text-white text-sm rounded"
                >
                  保存
                </button>
                <button
                  onClick={() =>
                    setDraft(
                      JSON.stringify(selected.profile_json ?? {}, null, 2),
                    )
                  }
                  className="px-4 py-2 bg-gray-200 text-sm rounded"
                >
                  重置
                </button>
              </div>
              <p className="mt-2 text-xs text-gray-500">
                保存会按名称合并写入知识图谱（Neo4j），随后回填数据库投影；下一次 ContextPack 重算会读取到新值。
              </p>
            </section>
          </div>
        ) : (
          <div className="text-gray-400 text-sm">请选择左侧角色</div>
        )}
      </main>
    </div>
  )
}

export default function CharactersPage() {
  return (
    <Suspense
      fallback={
        <div className="p-8 text-sm text-gray-400">加载…</div>
      }
    >
      <CharactersPageInner />
    </Suspense>
  )
}
