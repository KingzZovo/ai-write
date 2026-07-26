'use client'

import React, { useEffect, useState, useCallback } from 'react'
import Link from 'next/link'
import { useGenerationStore } from '@/stores/generationStore'
import { apiFetch } from '@/lib/api'
import { getChapterGenOptions, saveChapterGenOptions } from '@/lib/chapterGenOptions'
import { styleReferenceBookId } from '@/lib/styleReference'
import { parseDossierStatus } from '@/lib/dossier'
import { useT } from '@/lib/i18n/I18nProvider'

interface StyleInfo {
  id: string
  name: string
  is_active: number
  bind_level: string
  bind_target_id?: string | null
  rules_json: { rule: string }[]
  tone_keywords: string[]
}

interface ProjectSettingsResponse {
  settings_json?: Record<string, unknown> | null
}

interface StructureInfo {
  book_id: string
  book_title: string
  arc_pattern?: string | null
  structure_summary?: string | null
}

interface OutlineReadinessLayer {
  ready: boolean
  detail?: string | null
}

interface OutlineReadinessInfo {
  ready: boolean
  missing_layers: string[]
  block_message?: string | null
  layers: Record<'book' | 'volume' | 'chapter', OutlineReadinessLayer>
}

// Exported so DesktopWorkspace can read the selected values
let _selectedStyleId: string | null = null
let _selectedStructureBookId: string | null = null
export function getSelectedStyleId() { return _selectedStyleId }
export function getSelectedStructureBookId() { return _selectedStructureBookId }

// PR-FIX-PROJSET-SEL (2026-05-05): StyleSelector now persists selected style_profile_id
// to projects.settings_json so refresh / reopen restores the binding.
function StyleSelector({ projectId }: { projectId?: string | null }) {
  const [styles, setStyles] = useState<StyleInfo[]>([])
  const [loading, setLoading] = useState(true)
  const [selectedId, setSelectedId] = useState<string>('')
  const projectSettingsRef = React.useRef<Record<string, unknown> | null>(null)
  const loadedRef = React.useRef(false)

  useEffect(() => {
    let cancelled = false
    Promise.all([
      apiFetch<StyleInfo[]>('/api/styles').catch(() => [] as StyleInfo[]),
      projectId
        ? apiFetch<ProjectSettingsResponse>(`/api/projects/${projectId}`).catch(() => ({} as ProjectSettingsResponse))
        : Promise.resolve({} as ProjectSettingsResponse),
    ]).then(([data, proj]) => {
      if (cancelled) return
      setStyles(data)
      const settings = (proj && proj.settings_json) || {}
      projectSettingsRef.current = settings
      const styleRef = (settings.style_reference as Record<string, unknown> | undefined) || {}
      const persisted = typeof styleRef.profile_id === 'string' ? (styleRef.profile_id as string) : (typeof settings.style_profile_id === 'string' ? (settings.style_profile_id as string) : '')
      if (persisted && data.find(s => s.id === persisted)) {
        setSelectedId(persisted)
        _selectedStyleId = persisted
      } else {
        const active = data.find(s => s.is_active)
        if (active) {
          setSelectedId(active.id)
          _selectedStyleId = active.id
        }
      }
      loadedRef.current = true
      setLoading(false)
    })
    return () => { cancelled = true }
  }, [projectId])

  const handleChange = (id: string) => {
    setSelectedId(id)
    _selectedStyleId = id || null
    if (!projectId || !loadedRef.current) return
    const settings = { ...(projectSettingsRef.current || {}) }
    const prevRef = (settings.style_reference as Record<string, unknown> | undefined) || {}
    // Distillation rework: for book-bound profiles also persist the reference
    // book id so structure/world dossiers resolve even without the profile.
    settings.style_reference = {
      ...prevRef,
      profile_id: id || null,
      reference_book_id: styleReferenceBookId(styles.find(s => s.id === id)),
    }
    // Also write the legacy flat key for back-compat with older readers
    settings.style_profile_id = id || null
    projectSettingsRef.current = settings
    apiFetch(`/api/projects/${projectId}`, {
      method: 'PUT',
      body: JSON.stringify({ settings_json: settings }),
    }).catch(() => {})
  }

  const selected = styles.find(s => s.id === selectedId)

  if (loading) return <p className="text-xs text-gray-400">加载写法...</p>

  if (styles.length === 0) {
    return (
      <div className="text-xs text-gray-500 space-y-1">
        <p>暂无写法档案</p>
        <Link href="/styles" className="text-blue-600 hover:text-blue-700">
          前往创建写法 &rarr;
        </Link>
      </div>
    )
  }

  return (
    <div className="space-y-2">
      <select value={selectedId} onChange={e => handleChange(e.target.value)}
        className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg bg-white transition-shadow duration-150 focus:outline-none focus:ring-2 focus:ring-blue-400/40 focus:border-blue-300">
        <option value="">不使用写法（默认风格）</option>
        {styles.map(s => (
          <option key={s.id} value={s.id}>
            {s.name} ({s.rules_json?.length || 0}条规则)
          </option>
        ))}
      </select>

      {selected && (
        <div className="px-3 py-2 bg-blue-50 border border-blue-100 rounded-lg">
          <div className="flex flex-wrap gap-1">
            {selected.tone_keywords?.slice(0, 6).map((kw, i) => (
              <span key={i} className="text-[10px] px-1.5 py-0.5 bg-blue-100 text-blue-700 rounded">{kw}</span>
            ))}
          </div>
          <p className="text-[10px] text-blue-600 mt-1">{selected.rules_json?.length || 0} 条规则 · 生成时注入</p>
        </div>
      )}

      <Link href="/styles" className="block text-xs text-blue-600 hover:text-blue-700">
        管理写法 ({styles.length}) &rarr;
      </Link>
    </div>
  )
}

// GET /api/decompile/authors row; the route is being added in parallel on the
// backend, so the selector hides itself entirely when it is unavailable.
interface AuthorDossierInfo {
  author: string
  dossier_status?: unknown
}

/** Optional author-level dossier selector (distillation rework). Lists authors
 *  with a COMPLETED dossier and persists `style_reference.author_name` into
 *  projects.settings_json — same save path as StyleSelector. */
function AuthorDossierSelector({ projectId }: { projectId?: string | null }) {
  const t = useT()
  const [authors, setAuthors] = useState<string[] | null>(null)
  const [selected, setSelected] = useState('')
  const loadedRef = React.useRef(false)

  useEffect(() => {
    let cancelled = false
    Promise.all([
      apiFetch<AuthorDossierInfo[]>('/api/decompile/authors').catch(() => null),
      projectId
        ? apiFetch<ProjectSettingsResponse>(`/api/projects/${projectId}`).catch(() => ({} as ProjectSettingsResponse))
        : Promise.resolve({} as ProjectSettingsResponse),
    ]).then(([list, proj]) => {
      if (cancelled) return
      setAuthors(
        Array.isArray(list)
          ? list
              .filter(a => typeof a.author === 'string' && a.author
                && parseDossierStatus(a.dossier_status)?.state === 'done')
              .map(a => a.author)
          : null,
      )
      const settings = (proj && proj.settings_json) || {}
      const styleRef = (settings.style_reference as Record<string, unknown> | undefined) || {}
      if (typeof styleRef.author_name === 'string' && styleRef.author_name) {
        setSelected(styleRef.author_name)
      }
      loadedRef.current = true
    })
    return () => { cancelled = true }
  }, [projectId])

  const handleChange = async (name: string) => {
    setSelected(name)
    if (!projectId || !loadedRef.current) return
    try {
      // Read-modify-write against fresh settings so we never clobber the
      // style_reference fields StyleSelector may have just written.
      const proj = await apiFetch<ProjectSettingsResponse>(`/api/projects/${projectId}`)
      const settings = { ...((proj && proj.settings_json) || {}) }
      const styleRef = { ...((settings.style_reference as Record<string, unknown> | undefined) || {}) }
      if (name) styleRef.author_name = name
      else delete styleRef.author_name // empty option clears the key
      settings.style_reference = styleRef
      await apiFetch(`/api/projects/${projectId}`, {
        method: 'PUT',
        body: JSON.stringify({ settings_json: settings }),
      })
    } catch { /* */ }
  }

  // Route unavailable, or nothing to pick and nothing persisted → hide.
  if (authors === null || (authors.length === 0 && !selected)) return null
  // Keep a stale persisted selection visible in the dropdown.
  const options = selected && !authors.includes(selected) ? [selected, ...authors] : authors

  return (
    <div className="mt-3 space-y-1">
      <label className="block text-xs font-medium text-gray-600">{t('generate.authorDossier.label')}</label>
      <select value={selected} onChange={e => handleChange(e.target.value)}
        className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg bg-white transition-shadow duration-150 focus:outline-none focus:ring-2 focus:ring-blue-400/40 focus:border-blue-300">
        <option value="">{t('generate.authorDossier.none')}</option>
        {options.map(a => <option key={a} value={a}>{a}</option>)}
      </select>
      <p className="text-[10px] text-gray-400">{t('generate.authorDossier.hint')}</p>
    </div>
  )
}

interface GeneratePanelProps {
  projectId?: string
  selectedChapterId?: string | null
  outlineReadiness?: OutlineReadinessInfo | null
  outlineReadinessLoading?: boolean
  onGenerate?: () => void
  onGenerateOutline?: (level: string) => void
  onViewOutline?: (level: string) => void
}

interface EndpointInfo {
  id: string
  name: string
  provider_type: string
  default_model: string
  enabled: number
}

interface TaskConfig {
  task_type: string
  endpoint: EndpointInfo | null
  model_name: string
  temperature: number
  max_tokens: number
}

const TASK_LABELS: Record<string, string> = {
  generation: '正文生成',
  polishing: '润色',
  outline: '大纲生成',
  extraction: '信息提取',
  evaluation: '质量评估',
  summary: '摘要',
  embedding: '向量嵌入',
}

export function GeneratePanel({
  projectId,
  selectedChapterId,
  outlineReadiness,
  outlineReadinessLoading,
  onGenerate,
  onGenerateOutline,
  onViewOutline,
}: GeneratePanelProps) {
  const t = useT()
  const { isGenerating } = useGenerationStore()
  const [endpoints, setEndpoints] = useState<EndpointInfo[]>([])
  const [tasks, setTasks] = useState<TaskConfig[]>([])
  const [loading, setLoading] = useState(true)

  // outline 存在性状态。key: book/volume/chapter
  const [outlineCounts, setOutlineCounts] = useState<Record<string, number>>({ book: 0, volume: 0, chapter: 0 })
  const [confirmLevel, setConfirmLevel] = useState<string | null>(null)
  // UX hazard fix: when the chapter already has prose, gate 生成章节正文
  // behind a confirm (the previous version stays in 版本历史).
  const [confirmProse, setConfirmProse] = useState(false)
  // Keyed by chapter id so switching chapters resets to 0 without a
  // synchronous setState in the effect body.
  const [proseInfo, setProseInfo] = useState<{ chapterId: string; words: number } | null>(null)
  const chapterWordCount = proseInfo && proseInfo.chapterId === selectedChapterId ? proseInfo.words : 0

  useEffect(() => {
    if (!selectedChapterId || isGenerating) return // refresh once generation finishes
    let cancelled = false
    apiFetch<{ word_count?: number | null; content_text?: string | null }>(`/api/chapters/${selectedChapterId}`)
      .then(c => {
        if (cancelled) return
        const words = c.word_count || (c.content_text ? c.content_text.trim().length : 0)
        setProseInfo({ chapterId: selectedChapterId, words })
      })
      .catch(() => { if (!cancelled) setProseInfo({ chapterId: selectedChapterId, words: 0 }) })
    return () => { cancelled = true }
  }, [selectedChapterId, isGenerating])
  const refreshOutlineCounts = useCallback(async () => {
    if (!projectId) return
    try {
      const all = await apiFetch<Array<{ level: string }>>(`/api/projects/${projectId}/outlines`)
      const counts: Record<string, number> = { book: 0, volume: 0, chapter: 0 }
      ;(all || []).forEach(o => { counts[o.level] = (counts[o.level] || 0) + 1 })
      setOutlineCounts(counts)
    } catch { /* ignore */ }
  }, [projectId])

  useEffect(() => {
    Promise.all([
      apiFetch<{ endpoints: EndpointInfo[] }>('/api/model-config/endpoints').catch(() => ({ endpoints: [] })),
      apiFetch<{ tasks: TaskConfig[] }>('/api/model-config/tasks').catch(() => ({ tasks: [] })),
    ]).then(([epData, taskData]) => {
      setEndpoints(epData.endpoints)
      setTasks(taskData.tasks)
      setLoading(false)
    })
  }, [])

  useEffect(() => {
    queueMicrotask(() => { void refreshOutlineCounts() })
  }, [refreshOutlineCounts, isGenerating])

  const enabledEndpoints = endpoints.filter(e => e.enabled)
  const hasEndpoints = enabledEndpoints.length > 0
  const outlineLayerLabels: Record<'book' | 'volume' | 'chapter', string> = {
    book: '全书大纲',
    volume: '当前卷大纲',
    chapter: '本章大纲',
  }
  const canGenerateVolumeOutline =
    selectedChapterId
      ? Boolean(outlineReadiness?.layers.book?.ready)
      : outlineCounts.book > 0
  const canGenerateChapterOutline = Boolean(
    selectedChapterId &&
      outlineReadiness?.layers.book?.ready &&
      outlineReadiness?.layers.volume?.ready,
  )
  const canGenerateChapterProse = Boolean(selectedChapterId && outlineReadiness?.ready)

  // Key tasks for writing
  const writingTasks = tasks.filter(t => ['generation', 'outline', 'polishing'].includes(t.task_type))
  const otherTasks = tasks.filter(t => !['generation', 'outline', 'polishing'].includes(t.task_type))

  return (
    <div className="p-1 space-y-5">
      {/* Current model status */}
      <div>
        <h3 className="text-sm font-semibold text-gray-900 mb-3">当前模型配置</h3>
        {loading ? (
          <p className="text-xs text-gray-400">加载中...</p>
        ) : !hasEndpoints ? (
          <div className="p-3 bg-amber-50 border border-amber-200 rounded-lg">
            <p className="text-xs text-amber-700 mb-2">尚未配置模型端点</p>
            <Link href="/settings"
              className="text-xs text-blue-600 hover:text-blue-700 font-medium">
              前往设置页面配置 &rarr;
            </Link>
          </div>
        ) : (
          <div className="space-y-2">
            {/* Writing-related tasks */}
            {writingTasks.map(t => (
              <div key={t.task_type}
                className="flex items-center justify-between px-3 py-2 bg-gray-50 rounded-lg">
                <span className="text-xs font-medium text-gray-600">
                  {TASK_LABELS[t.task_type] || t.task_type}
                </span>
                {t.endpoint ? (
                  <span className="text-xs text-gray-800 font-mono truncate max-w-[140px]"
                    title={`${t.endpoint.name} / ${t.model_name || t.endpoint.default_model}`}>
                    {t.model_name || t.endpoint.default_model}
                  </span>
                ) : (
                  <span className="text-xs text-gray-400">未分配</span>
                )}
              </div>
            ))}

            {/* Other tasks collapsed */}
            {otherTasks.length > 0 && (
              <details className="text-xs">
                <summary className="text-gray-400 cursor-pointer hover:text-gray-600 py-1">
                  其他任务 ({otherTasks.length})
                </summary>
                <div className="space-y-1 mt-1">
                  {otherTasks.map(t => (
                    <div key={t.task_type}
                      className="flex items-center justify-between px-3 py-1.5 bg-gray-50 rounded">
                      <span className="text-gray-500">
                        {TASK_LABELS[t.task_type] || t.task_type}
                      </span>
                      {t.endpoint ? (
                        <span className="text-gray-700 font-mono truncate max-w-[120px]">
                          {t.model_name || t.endpoint.default_model}
                        </span>
                      ) : (
                        <span className="text-gray-400">未分配</span>
                      )}
                    </div>
                  ))}
                </div>
              </details>
            )}

            <Link href="/settings"
              className="block text-xs text-blue-600 hover:text-blue-700 mt-1">
              管理端点和任务分配 &rarr;
            </Link>
          </div>
        )}
      </div>

      {/* Outline chain status */}
      <div className="border-t pt-4">
        <h3 className="text-sm font-semibold text-gray-900 mb-3">大纲链路</h3>
        {!selectedChapterId ? (
          <div className="p-3 bg-gray-50 border border-gray-200 rounded-lg text-xs text-gray-500">
            先选中一章，再检查全书 / 分卷 / 章节大纲是否齐备。
          </div>
        ) : outlineReadinessLoading ? (
          <div className="p-3 bg-gray-50 border border-gray-200 rounded-lg text-xs text-gray-500">
            检查中...
          </div>
        ) : (
          <div className="space-y-2">
            <div
              className={`p-3 rounded-lg border text-xs ${
                outlineReadiness?.ready
                  ? 'bg-emerald-50 border-emerald-200 text-emerald-800'
                  : 'bg-amber-50 border-amber-200 text-amber-800'
              }`}
            >
              {outlineReadiness?.ready
                ? '链路完整，可生成本章正文。'
                : outlineReadiness?.block_message || '链路齐备后才能生成本章正文。'}
            </div>
            <div className="grid grid-cols-3 gap-2">
              {(['book', 'volume', 'chapter'] as const).map((layerKey) => {
                const layer = outlineReadiness?.layers[layerKey]
                const ready = Boolean(layer?.ready)
                return (
                  <div
                    key={layerKey}
                    className={`rounded-lg border px-3 py-2 text-xs ${
                      ready
                        ? 'bg-emerald-50 border-emerald-200 text-emerald-800'
                        : 'bg-amber-50 border-amber-200 text-amber-800'
                    }`}
                  >
                    <div className="font-medium">{outlineLayerLabels[layerKey]}</div>
                    <div className="mt-1 text-[11px] leading-snug">
                      {layer?.detail || (ready ? '已就绪' : '未就绪')}
                    </div>
                  </div>
                )
              })}
            </div>
          </div>
        )}
      </div>

      {/* PR-GEN-UX Task 4: per-chapter generation options (指令 / 目标字数) */}
      {selectedChapterId && (
        <div className="border-t pt-4">
          <h3 className="text-sm font-semibold text-gray-900 mb-3">本章生成选项</h3>
          <ChapterGenOptionsEditor key={selectedChapterId} chapterId={selectedChapterId} />
        </div>
      )}

      {/* Generation buttons */}
      <div className="border-t pt-4">
        <h3 className="text-sm font-semibold text-gray-900 mb-3">内容生成</h3>
        <div className="space-y-2">
          <OutlineButtonRow
            label="全书大纲" colorClass="bg-purple-600 hover:bg-purple-700"
            count={outlineCounts.book} disabled={isGenerating || !hasEndpoints}
            onView={() => onViewOutline?.("book")}
            onGenerate={() => outlineCounts.book > 0 ? setConfirmLevel("book") : onGenerateOutline?.("book")}
          />
          <OutlineButtonRow
            label="分卷大纲" colorClass="bg-indigo-600 hover:bg-indigo-700"
            count={outlineCounts.volume} disabled={isGenerating || !hasEndpoints || !canGenerateVolumeOutline}
            onView={() => onViewOutline?.("volume")}
            onGenerate={() => outlineCounts.volume > 0 ? setConfirmLevel("volume") : onGenerateOutline?.("volume")}
          />
          <OutlineButtonRow
            label="章节大纲" colorClass="bg-blue-600 hover:bg-blue-700"
            count={outlineCounts.chapter} disabled={isGenerating || !hasEndpoints || !canGenerateChapterOutline}
            onView={() => onViewOutline?.("chapter")}
            onGenerate={() => outlineCounts.chapter > 0 ? setConfirmLevel("chapter") : onGenerateOutline?.("chapter")}
          />
          <button
            onClick={() => chapterWordCount > 0 ? setConfirmProse(true) : onGenerate?.()}
            disabled={isGenerating || !hasEndpoints || !canGenerateChapterProse}
            className="w-full px-4 py-2 text-sm font-medium bg-green-600 text-white rounded-lg shadow-sm transition-all duration-150 hover:bg-green-700 motion-safe:active:scale-[0.98] disabled:opacity-50 disabled:cursor-not-allowed">
            {isGenerating ? "生成中..." : chapterWordCount > 0 ? t('generate.prose.regen') : "生成章节正文"}
          </button>
          {confirmLevel && (
            <ConfirmModal
              level={confirmLevel}
              onCancel={() => setConfirmLevel(null)}
              onConfirm={() => { onGenerateOutline?.(confirmLevel); setConfirmLevel(null) }}
            />
          )}
          {confirmProse && (
            <ProseRegenConfirmModal
              wordCount={chapterWordCount}
              onCancel={() => setConfirmProse(false)}
              onConfirm={() => { setConfirmProse(false); onGenerate?.() }}
            />
          )}
        </div>
      </div>

      {/* Writing style selector */}
      <div className="border-t pt-4">
        <h3 className="text-sm font-semibold text-gray-900 mb-3">写作风格</h3>
        <StyleSelector projectId={projectId} />
        <AuthorDossierSelector projectId={projectId} />
      </div>

      {/* Plot structure selector (optional) */}
      <div className="border-t pt-4">
        <h3 className="text-sm font-semibold text-gray-900 mb-3">剧情架构（可选）</h3>
        <StructureSelector projectId={projectId} />
      </div>
    </div>
  )
}

// PR-FIX-PROJSET-SEL (2026-05-05): StructureSelector now persists selected structure_book_id
// to projects.settings_json.plot_structure so refresh restores the binding.
function StructureSelector({ projectId }: { projectId?: string | null }) {
  const [structures, setStructures] = useState<StructureInfo[]>([])
  const [selectedId, setSelectedId] = useState<string>('')
  const projectSettingsRef = React.useRef<Record<string, unknown> | null>(null)
  const loadedRef = React.useRef(false)

  useEffect(() => {
    let cancelled = false
    Promise.all([
      apiFetch<StructureInfo[]>('/api/styles/structures').catch(() => [] as StructureInfo[]),
      projectId
        ? apiFetch<ProjectSettingsResponse>(`/api/projects/${projectId}`).catch(() => ({} as ProjectSettingsResponse))
        : Promise.resolve({} as ProjectSettingsResponse),
    ]).then(([data, proj]) => {
      if (cancelled) return
      setStructures(data)
      const settings = (proj && proj.settings_json) || {}
      projectSettingsRef.current = settings
      const ps = (settings.plot_structure as Record<string, unknown> | undefined) || {}
      const persisted = typeof ps.structure_book_id === 'string' ? (ps.structure_book_id as string) : ''
      if (persisted && data.find((s) => s.book_id === persisted)) {
        setSelectedId(persisted)
        _selectedStructureBookId = persisted
      }
      loadedRef.current = true
    })
    return () => { cancelled = true }
  }, [projectId])

  const handleChange = (id: string) => {
    setSelectedId(id)
    _selectedStructureBookId = id || null
    if (!projectId || !loadedRef.current) return
    const settings = { ...(projectSettingsRef.current || {}) }
    settings.plot_structure = { structure_book_id: id || null }
    projectSettingsRef.current = settings
    apiFetch(`/api/projects/${projectId}`, {
      method: 'PUT',
      body: JSON.stringify({ settings_json: settings }),
    }).catch(() => {})
  }

  if (structures.length === 0) {
    return <p className="text-xs text-gray-400">暂无架构数据，请先在参考书库中“提取架构”</p>
  }

  return (
    <div className="space-y-2">
      <select value={selectedId} onChange={e => handleChange(e.target.value)}
        className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg bg-white transition-shadow duration-150 focus:outline-none focus:ring-2 focus:ring-blue-400/40 focus:border-blue-300">
        <option value="">不使用剧情架构</option>
        {structures.map((s) => (
          <option key={s.book_id} value={s.book_id}>
            {s.book_title} — {s.arc_pattern || ''}
          </option>
        ))}
      </select>
      {selectedId && (
        <p className="text-[10px] text-orange-500">
          {structures.find((s) => s.book_id === selectedId)?.structure_summary || ''}
        </p>
      )}
    </div>
  )
}

// PR-GEN-UX Task 4: 指令 + 目标字数 inputs, persisted per-chapter in
// localStorage (see lib/chapterGenOptions.ts). DesktopWorkspace reads them
// when it builds the /api/generate/chapter payload.
function ChapterGenOptionsEditor({ chapterId }: { chapterId: string }) {
  // Mounted with key={chapterId}, so lazy init re-reads storage per chapter.
  const [instruction, setInstruction] = useState(
    () => getChapterGenOptions(chapterId).userInstruction,
  )
  const [targetWordsText, setTargetWordsText] = useState(() => {
    const words = getChapterGenOptions(chapterId).targetWords
    return words != null ? String(words) : ''
  })

  const persist = (nextInstruction: string, nextWordsText: string) => {
    const n = parseInt(nextWordsText.trim(), 10)
    saveChapterGenOptions(chapterId, {
      userInstruction: nextInstruction.trim(),
      targetWords: Number.isNaN(n) || n <= 0 ? null : n,
    })
  }

  return (
    <div className="space-y-2">
      <div>
        <label className="block text-xs text-gray-600 mb-1">指令（可选，随本章生成一起发送）</label>
        <textarea
          value={instruction}
          onChange={(e) => { setInstruction(e.target.value); persist(e.target.value, targetWordsText) }}
          placeholder="例如：多写对话，结尾留悬念..."
          className="w-full px-3 py-2 text-xs border border-gray-200 rounded-lg resize-none h-20 transition-shadow duration-150 focus:outline-none focus:ring-2 focus:ring-blue-400/40 focus:border-blue-300"
        />
      </div>
      <div className="flex items-center gap-2">
        <label className="text-xs text-gray-600">目标字数</label>
        <input
          type="number"
          min={1}
          value={targetWordsText}
          onChange={(e) => { setTargetWordsText(e.target.value); persist(instruction, e.target.value) }}
          placeholder="默认"
          className="w-24 px-2 py-1 text-xs border border-gray-200 rounded-lg placeholder-gray-400 transition-shadow duration-150 focus:outline-none focus:ring-2 focus:ring-blue-400/40 focus:border-blue-300"
        />
        <span className="text-[10px] text-gray-400">留空使用章节 / 项目默认</span>
      </div>
    </div>
  )
}

function OutlineButtonRow({ label, colorClass, count, disabled, onView, onGenerate }: {
  label: string; colorClass: string; count: number; disabled: boolean;
  onView: () => void; onGenerate: () => void
}) {
  const exists = count > 0
  if (exists) {
    return (
      <div className="flex gap-1.5">
        <button onClick={onView} disabled={disabled}
          className={`flex-1 px-3 py-2 text-sm font-medium text-white rounded-lg shadow-sm transition-all duration-150 motion-safe:active:scale-[0.98] disabled:opacity-50 disabled:cursor-not-allowed ${colorClass}`}>
          📖 查看{label} ({count})
        </button>
        <button onClick={onGenerate} disabled={disabled} title="重新生成（需确认）"
          className="px-2 py-2 text-xs bg-stone-200 text-stone-700 rounded-lg transition-colors duration-150 hover:bg-stone-300 disabled:opacity-50 disabled:cursor-not-allowed">
          ↺
        </button>
      </div>
    )
  }
  return (
    <button onClick={onGenerate} disabled={disabled}
      className={`w-full px-4 py-2 text-sm font-medium text-white rounded-lg shadow-sm transition-all duration-150 motion-safe:active:scale-[0.98] disabled:opacity-50 disabled:cursor-not-allowed ${colorClass}`}>
      ⚡ 生成{label}
    </button>
  )
}

// Confirm gate for regenerating a chapter that already has prose (same look
// as ConfirmModal below). Purely presentational; the generate handler is
// unchanged and only runs after explicit confirmation.
function ProseRegenConfirmModal({ wordCount, onCancel, onConfirm }: {
  wordCount: number; onCancel: () => void; onConfirm: () => void
}) {
  const t = useT()
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4 motion-safe:transition-opacity motion-safe:duration-200 motion-safe:starting:opacity-0">
      <div className="bg-white rounded-lg shadow-xl max-w-sm w-full p-5 space-y-3 motion-safe:transition-[opacity,transform] motion-safe:duration-200 motion-safe:starting:scale-95 motion-safe:starting:opacity-0">
        <h3 className="text-base font-semibold tracking-tight text-stone-900">{t('generate.prose.regenTitle')}</h3>
        <p className="text-sm text-stone-600">
          {t('generate.prose.regenBodyPrefix')}{wordCount.toLocaleString()}{t('generate.prose.regenBodySuffix')}
        </p>
        <div className="flex gap-2 justify-end">
          <button onClick={onCancel} className="px-3 py-1.5 text-sm text-stone-700 rounded-md transition-colors duration-150 hover:bg-stone-100">取消</button>
          <button onClick={onConfirm} className="px-3 py-1.5 text-sm font-medium bg-red-600 text-white rounded-md shadow-sm transition-all duration-150 hover:bg-red-700 motion-safe:active:scale-[0.98]">{t('generate.prose.regenConfirm')}</button>
        </div>
      </div>
    </div>
  )
}

function ConfirmModal({ level, onCancel, onConfirm }: { level: string; onCancel: () => void; onConfirm: () => void }) {
  const labels: Record<string, string> = { book: "全书大纲", volume: "分卷大纲", chapter: "章节大纲" }
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4 motion-safe:transition-opacity motion-safe:duration-200 motion-safe:starting:opacity-0">
      <div className="bg-white rounded-lg shadow-xl max-w-sm w-full p-5 space-y-3 motion-safe:transition-[opacity,transform] motion-safe:duration-200 motion-safe:starting:scale-95 motion-safe:starting:opacity-0">
        <h3 className="text-base font-semibold tracking-tight text-stone-900">重新生成{labels[level] || level}？</h3>
        <p className="text-sm text-stone-600">已存在{labels[level] || level}。重新生成会产生新的版本，原有版本会保留但不再被默认使用。</p>
        <div className="flex gap-2 justify-end">
          <button onClick={onCancel} className="px-3 py-1.5 text-sm text-stone-700 rounded-md transition-colors duration-150 hover:bg-stone-100">取消</button>
          <button onClick={onConfirm} className="px-3 py-1.5 text-sm font-medium bg-red-600 text-white rounded-md shadow-sm transition-all duration-150 hover:bg-red-700 motion-safe:active:scale-[0.98]">确认重新生成</button>
        </div>
      </div>
    </div>
  )
}
