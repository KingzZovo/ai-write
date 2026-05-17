'use client'

import { useEffect, useState } from 'react'
import { apiFetch } from '@/lib/api'
import type { Project } from '@/stores/projectStore'

const GENRES = [
  { label: '玄幻', code: 'xuanhuan' },
  { label: '仙侠', code: 'xianxia' },
  { label: '都市', code: 'dushi' },
  { label: '言情', code: 'yanqing' },
  { label: '悬疑', code: 'xuanyi' },
  { label: '科幻', code: 'kehuan' },
  { label: '历史', code: 'lishi' },
  { label: '其他', code: '' },
] as const

const MODULES = [
  { key: 'show_not_tell',       label: '展示而非讲述' },
  { key: 'scene_immersion',     label: '场景沉浸感' },
  { key: 'dialogue_craft',      label: '对话技巧' },
  { key: 'tension_control',     label: '张力控制' },
  { key: 'micro_tension',       label: '微观张力' },
  { key: 'emotional_resonance', label: '情感共鸣' },
  { key: 'info_weaving',        label: '信息编织' },
] as const

const DEFAULT_MODULES = ['show_not_tell', 'micro_tension', 'info_weaving']

interface StyleInfo { id: string; name: string; rules_json?: any[]; is_active?: boolean }
interface StructureInfo { book_id: string; book_title: string; arc_pattern?: string; structure_summary?: string }

export function NewProjectModal({
  onClose,
  onCreated,
}: {
  onClose: () => void
  onCreated: (p: Project) => void
}) {
  const [title, setTitle] = useState('')
  const [genreLabel, setGenreLabel] = useState<string>(GENRES[0].label)
  const [genreCode, setGenreCode] = useState<string>(GENRES[0].code)
  const [premise, setPremise] = useState('')
  const [styleId, setStyleId] = useState<string>('')
  const [structureBookId, setStructureBookId] = useState<string>('')
  const [activeModules, setActiveModules] = useState<string[]>([...DEFAULT_MODULES])
  const [styles, setStyles] = useState<StyleInfo[]>([])
  const [structures, setStructures] = useState<StructureInfo[]>([])
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    let cancelled = false
    Promise.all([
      apiFetch<StyleInfo[]>('/api/styles').catch(() => [] as StyleInfo[]),
      apiFetch<StructureInfo[]>('/api/styles/structures').catch(() => [] as StructureInfo[]),
    ]).then(([s, st]) => {
      if (cancelled) return
      setStyles(s)
      setStructures(st)
      const active = s.find(x => x.is_active)
      if (active) setStyleId(active.id)
    })
    return () => { cancelled = true }
  }, [])

  const toggleModule = (key: string) => {
    setActiveModules(prev => prev.includes(key) ? prev.filter(k => k !== key) : [...prev, key])
  }

  const submit = async () => {
    if (!title.trim() || busy) return
    setBusy(true)
    try {
      const settings_json: Record<string, unknown> = {
        writing_guide: { active_modules: activeModules, genre_code: genreCode || null },
        style_reference: { profile_id: styleId || null, reference_book_id: null },
        style_profile_id: styleId || null,
        plot_structure: { structure_book_id: structureBookId || null },
      }
      const project = await apiFetch<Project>('/api/projects', {
        method: 'POST',
        body: JSON.stringify({
          title: title.trim(),
          genre: genreLabel,
          genre_profile_code: genreCode || null,
          premise: premise.trim() || null,
          settings_json,
        }),
      })
      onCreated(project)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
      <div className="bg-white rounded-2xl shadow-xl w-full max-w-lg p-6 max-h-[90vh] overflow-y-auto">
        <h3 className="text-lg font-bold text-gray-900 mb-4">新建项目</h3>
        <div className="space-y-4">
          {/* 书名 */}
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">
              书名 <span className="text-red-500">*</span>
            </label>
            <input
              type="text"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="输入小说名称"
              className="w-full px-3 py-2 text-sm border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500"
              autoFocus
            />
          </div>
          {/* 类型 */}
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">类型</label>
            <select
              value={genreLabel}
              onChange={(e) => {
                const g = GENRES.find(x => x.label === e.target.value)
                if (g) { setGenreLabel(g.label); setGenreCode(g.code) }
              }}
              className="w-full px-3 py-2 text-sm border border-gray-300 rounded-lg"
            >
              {GENRES.map((g) => <option key={g.label} value={g.label}>{g.label}</option>)}
            </select>
          </div>
          {/* 故事梗概 */}
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">故事梗概</label>
            <textarea
              value={premise}
              onChange={(e) => setPremise(e.target.value)}
              placeholder="简要描述你的小说设定和核心创意..."
              className="w-full px-3 py-2 text-sm border border-gray-300 rounded-lg resize-none h-20"
            />
          </div>
          {/* 写法风格 */}
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">写法风格</label>
            <select
              value={styleId}
              onChange={(e) => setStyleId(e.target.value)}
              className="w-full px-3 py-2 text-sm border border-gray-300 rounded-lg"
              disabled={styles.length === 0}
            >
              <option value="">不使用写法（默认风格）</option>
              {styles.map(s => (
                <option key={s.id} value={s.id}>
                  {s.name}{s.rules_json?.length ? ` (${s.rules_json.length}条规则)` : ''}
                </option>
              ))}
            </select>
            {styles.length === 0 && (
              <p className="text-[11px] text-gray-400 mt-1">暂无写法档案，可在「写法管理」页面创建</p>
            )}
          </div>
          {/* 剧情架构 */}
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">剧情架构</label>
            <select
              value={structureBookId}
              onChange={(e) => setStructureBookId(e.target.value)}
              className="w-full px-3 py-2 text-sm border border-gray-300 rounded-lg"
              disabled={structures.length === 0}
            >
              <option value="">不使用剧情架构</option>
              {structures.map(s => (
                <option key={s.book_id} value={s.book_id}>
                  {s.book_title}{s.arc_pattern ? ` — ${s.arc_pattern}` : ''}
                </option>
              ))}
            </select>
            {structures.length === 0 && (
              <p className="text-[11px] text-gray-400 mt-1">暂无架构数据，可在参考书库中「提取架构」</p>
            )}
          </div>
          {/* 写作指南模块 */}
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-2">写作指南模块</label>
            <div className="grid grid-cols-2 gap-1.5">
              {MODULES.map(m => (
                <label key={m.key} className="flex items-center gap-1.5 px-2 py-1.5 border border-gray-200 rounded-lg text-xs cursor-pointer hover:bg-gray-50">
                  <input
                    type="checkbox"
                    checked={activeModules.includes(m.key)}
                    onChange={() => toggleModule(m.key)}
                    className="h-3.5 w-3.5"
                  />
                  <span>{m.label}</span>
                </label>
              ))}
            </div>
            <p className="text-[11px] text-gray-400 mt-1">已选 {activeModules.length} / {MODULES.length}</p>
          </div>
        </div>
        <div className="flex gap-3 mt-6">
          <button onClick={onClose} className="flex-1 px-4 py-2 text-sm border border-gray-300 rounded-lg hover:bg-gray-50">
            取消
          </button>
          <button
            onClick={submit}
            disabled={!title.trim() || busy}
            className="flex-1 px-4 py-2 text-sm bg-blue-600 text-white rounded-lg disabled:opacity-50"
          >
            {busy ? '创建中...' : '创建项目'}
          </button>
        </div>
      </div>
    </div>
  )
}
