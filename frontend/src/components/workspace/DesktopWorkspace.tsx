'use client'

import { useState, useCallback, useEffect, useRef } from 'react'
import dynamic from 'next/dynamic'
import { useRouter, useSearchParams } from 'next/navigation'
import {
  AlertTriangle,
  BookOpen,
  Brain,
  CheckCircle2,
  ClipboardCheck,
  Database,
  FileCheck2,
  GitBranch,
  History,
  ListChecks,
  Network,
  PenLine,
  Search,
  Settings,
  ShieldCheck,
  Sparkles,
  Users,
} from 'lucide-react'
import { WorkspaceLayout } from '@/components/workspace/WorkspaceLayout'
import { OutlineTree } from '@/components/outline/OutlineTree'
import { VolumeOutlineBlock } from '@/components/outline/VolumeOutlineBlock'
import { OutlineEditor, type OutlineEditTarget } from '@/components/outline/OutlineEditor'
import { GeneratePanel, getSelectedStyleId } from '@/components/panels/GeneratePanel'

// Lazy load heavy panels — only loaded when their workbench tab/drawer is opened
const ForeshadowPanel = dynamic(() => import('@/components/panels/ForeshadowPanel').then(m => ({ default: m.ForeshadowPanel })), { ssr: false })
const SettingsPanel = dynamic(() => import('@/components/panels/SettingsPanel').then(m => ({ default: m.SettingsPanel })), { ssr: false })
const EvaluationPanel = dynamic(() => import('@/components/panels/EvaluationPanel').then(m => ({ default: m.EvaluationPanel })), { ssr: false })
const CheckerDashboard = dynamic(() => import('@/components/panels/CheckerDashboard').then(m => ({ default: m.CheckerDashboard })), { ssr: false })
const StrandPanel = dynamic(() => import('@/components/panels/StrandPanel').then(m => ({ default: m.StrandPanel })), { ssr: false })
const WritingGuidePanel = dynamic(() => import('@/components/panels/WritingGuidePanel').then(m => ({ default: m.WritingGuidePanel })), { ssr: false })
const AntiAIPanel = dynamic(() => import('@/components/panels/AntiAIPanel').then(m => ({ default: m.AntiAIPanel })), { ssr: false })
const VersionPanel = dynamic(() => import('@/components/panels/VersionPanel').then(m => ({ default: m.VersionPanel })), { ssr: false })
const TokenDashboard = dynamic(() => import('@/components/panels/TokenDashboard').then(m => ({ default: m.TokenDashboard })), { ssr: false })
const CharacterCardPanel = dynamic(() => import('@/components/panels/CharacterCardPanel').then(m => ({ default: m.CharacterCardPanel })), { ssr: false })
const CascadeTasksPanel = dynamic(() => import('@/components/panels/CascadeTasksPanel').then(m => ({ default: m.CascadeTasksPanel })), { ssr: false })
import {
  useProjectStore,
  normalizeVolume,
  normalizeChapter,
} from '@/stores/projectStore'
import type { Project, Volume, Chapter } from '@/stores/projectStore'
import { useGenerationStore } from '@/stores/generationStore'
import { apiFetch, apiSSE } from '@/lib/api'

// ----------------------------------------------------------------
// Types for API responses
// ----------------------------------------------------------------

interface VolumeRes {
  id: string
  project_id: string
  title: string
  volume_idx: number
  summary: string | null
}

interface ChapterRes {
  id: string
  volume_id: string
  title: string
  chapter_idx: number
  content_text: string
  word_count: number
  status: string
  summary: string | null
  outline_json: Record<string, unknown>
  target_word_count?: number | null
}

interface OutlineRes {
  id: string
  project_id: string
  level: string
  parent_id: string | null
  content_json: Record<string, unknown>
  version: number
  is_confirmed: number
}

interface OutlineReadinessLayer {
  ready: boolean
  detail?: string | null
  outline_id?: string | null
  title?: string | null
  volume_idx?: number | null
  chapter_idx?: number | null
}

interface OutlineReadinessRes {
  project_id: string
  volume_id?: string | null
  volume_idx?: number | null
  chapter_id?: string | null
  chapter_idx?: number | null
  ready: boolean
  missing_layers: string[]
  block_message?: string | null
  layers: Record<'book' | 'volume' | 'chapter', OutlineReadinessLayer>
}

function StatusPill({
  tone = 'gray',
  children,
}: {
  tone?: 'gray' | 'green' | 'amber' | 'red' | 'blue' | 'purple'
  children: React.ReactNode
}) {
  const tones = {
    gray: 'border-gray-200 bg-gray-50 text-gray-600',
    green: 'border-emerald-200 bg-emerald-50 text-emerald-700',
    amber: 'border-amber-200 bg-amber-50 text-amber-700',
    red: 'border-red-200 bg-red-50 text-red-700',
    blue: 'border-blue-200 bg-blue-50 text-blue-700',
    purple: 'border-violet-200 bg-violet-50 text-violet-700',
  }
  return (
    <span className={`inline-flex items-center rounded border px-2 py-0.5 text-[11px] font-medium ${tones[tone]}`}>
      {children}
    </span>
  )
}

function WorkbenchMetric({
  label,
  value,
  hint,
}: {
  label: string
  value: string | number
  hint?: string
}) {
  return (
    <div className="min-w-0 rounded-md border border-gray-200 bg-white px-2.5 py-2">
      <div className="text-[10px] font-medium text-gray-400">{label}</div>
      <div className="mt-0.5 text-sm font-semibold text-gray-900">{value}</div>
      {hint && <div className="mt-0.5 truncate text-[10px] text-gray-500">{hint}</div>}
    </div>
  )
}

function WorkbenchTabButton({
  active,
  label,
  icon: Icon,
  onClick,
}: {
  active: boolean
  label: string
  icon: React.ComponentType<{ className?: string }>
  onClick: () => void
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`flex items-center justify-center gap-1.5 rounded-md border px-2.5 py-2 text-xs font-medium transition-colors ${
        active
          ? 'border-gray-900 bg-gray-900 text-white'
          : 'border-gray-200 bg-white text-gray-600 hover:border-gray-300 hover:bg-gray-50'
      }`}
    >
      <Icon className="h-3.5 w-3.5" />
      <span>{label}</span>
    </button>
  )
}

function WorkbenchCard({
  title,
  icon: Icon,
  children,
  action,
}: {
  title: string
  icon: React.ComponentType<{ className?: string }>
  children: React.ReactNode
  action?: React.ReactNode
}) {
  return (
    <section className="rounded-lg border border-gray-200 bg-white">
      <div className="flex items-center justify-between gap-2 border-b border-gray-100 px-3 py-2">
        <div className="flex items-center gap-2 min-w-0">
          <Icon className="h-4 w-4 text-gray-500" />
          <h3 className="truncate text-sm font-semibold text-gray-900">{title}</h3>
        </div>
        {action}
      </div>
      <div className="p-3">{children}</div>
    </section>
  )
}

function DrawerLinkButton({
  label,
  icon: Icon,
  onClick,
  hint,
}: {
  label: string
  icon: React.ComponentType<{ className?: string }>
  onClick: () => void
  hint?: string
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="w-full rounded-md border border-gray-200 bg-white px-3 py-2 text-left transition-colors hover:border-gray-300 hover:bg-gray-50"
    >
      <span className="flex items-center gap-2 text-sm font-medium text-gray-800">
        <Icon className="h-4 w-4 text-gray-500" />
        {label}
      </span>
      {hint && <span className="mt-0.5 block pl-6 text-[11px] leading-relaxed text-gray-500">{hint}</span>}
    </button>
  )
}

// ================================================================
// WorkspacePage
// ================================================================

export default function DesktopWorkspace() {
  const router = useRouter()
  const searchParams = useSearchParams()
  const urlProjectId = searchParams.get('id')

  const {
    currentProject,
    selectedChapterId,
    setCurrentProject,
    setVolumes,
    setChapters,
    selectChapter,
    volumes,
    chapters,
    updateChapterContent,
    updateChapterStatus,
  } = useProjectStore()

  const { isGenerating, setIsGenerating, appendStreamContent, resetStreamContent } =
    useGenerationStore()

  // ---- Local UI state ----
  const [editorContent, setEditorContent] = useState('')
  const [creativeInput, setCreativeInput] = useState('')
  const [outlinePreview, setOutlinePreview] = useState('')
  const [generationError, setGenerationError] = useState<string | null>(null)
  // PR-OL1: AI-suggested volume plan parsed from staged SSE done event.
  const [volumePlan, setVolumePlan] = useState<Array<{idx:number; title:string; theme:string; core_conflict:string; est_chapters:number}> | null>(null)
  // PR-OL3: edit mode for the volume plan card.
  const [editingPlan, setEditingPlan] = useState(false)
  const [savingPlan, setSavingPlan] = useState(false)
  // PR-OL5: post-save notice prompting to regenerate volumes if any exist.
  const [planSaveNotice, setPlanSaveNotice] = useState<string | null>(null)
  const [activeView, setActiveView] = useState<'editor' | 'outline' | 'wizard'>(
    'wizard'
  )

  // v1.4.2 Task B: per-stage progress for the staged book outline SSE.
  // status: idle | running | done | error. Stages A/B/C correspond to
  // skeleton / characters / world.
  type StageKey = 'A' | 'B' | 'C'
  type StageStatus = 'idle' | 'running' | 'done' | 'error'
  const [stageStates, setStageStates] = useState<Record<StageKey, StageStatus>>({
    A: 'idle',
    B: 'idle',
    C: 'idle',
  })
  const stageLabels: Record<StageKey, string> = { A: '骨架', B: '角色', C: '世界观' }
  const resetStageStates = () =>
    setStageStates({ A: 'idle', B: 'idle', C: 'idle' })

  // Wizard state
  const [wizardStep, setWizardStep] = useState(1)
  const [wizardProgress, setWizardProgress] = useState('')
  const [confirmedOutlineId, setConfirmedOutlineId] = useState<string | null>(null)
  // PR-OL14: book outline content_json for top-level OutlineTree section.
  const [bookOutlineData, setBookOutlineData] = useState<Record<string, unknown> | null>(null)
  // Volume generation config & results
  const [volumeCountInput, setVolumeCountInput] = useState('')
  const [volumeOutlines, setVolumeOutlines] = useState<Record<number, Record<string, unknown>>>({})
  // PR-OUTLINE-CENTER-EDIT (2026-05-04): outline IDs (alongside content_json) so
  // the centre editor knows which row to PUT.
  const [bookOutlineId, setBookOutlineId] = useState<string | null>(null)
  const [volumeOutlineIds, setVolumeOutlineIds] = useState<Record<number, string>>({})
  const [outlineEditorTarget, setOutlineEditorTarget] = useState<OutlineEditTarget | null>(null)
  // Tracks the backend auto-saved outline id for the current in-progress book outline
  const pendingBookOutlineIdRef = useRef<string | null>(null)
  // Outline inline editing
  const [outlineEditing, setOutlineEditing] = useState(false)
  const [outlineReadiness, setOutlineReadiness] = useState<OutlineReadinessRes | null>(null)
  const [outlineReadinessLoading, setOutlineReadinessLoading] = useState(false)

  // Drawer panel
  const [drawerPanel, setDrawerPanel] = useState<string | null>(null)
  const [rightPanelTab, setRightPanelTab] = useState<'write' | 'review' | 'memory' | 'graph'>('write')

  // Auto-save ref
  const saveTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const lastSavedRef = useRef<string>('')
  const generationBufferRef = useRef<string>('')
  const generationBaselineRef = useRef<{ content: string; status: Chapter['status'] } | null>(null)
  const generationSavedRef = useRef(false)
  const generationFailedRef = useRef(false)
  // Task F2: tracks the live SSE stream. Aborted before starting a new
  // stream (regenerate / re-click) and on unmount, so a stale stream cannot
  // keep reading and setState on a dead component.
  const sseControllerRef = useRef<AbortController | null>(null)
  // Task F3 fold-in: lets long-running async loops (volume outline wizard)
  // notice unmount and stop opening new streams.
  const unmountedRef = useRef(false)

  // ----------------------------------------------------------------
  // Sync URL ?id=... → currentProject; redirect to / if missing/invalid
  // ----------------------------------------------------------------
  useEffect(() => {
    if (!urlProjectId) {
      router.replace('/')
      return
    }
    if (currentProject?.id === urlProjectId) return
    apiFetch<Project>(`/api/projects/${urlProjectId}`)
      .then((p) => setCurrentProject(p))
      .catch(() => router.replace('/'))
  }, [urlProjectId, currentProject?.id, router, setCurrentProject])

  // ----------------------------------------------------------------
  // Load volumes + chapters when a project is selected
  // ----------------------------------------------------------------
  const loadProjectData = useCallback(
    async (projectId: string) => {
      try {
        const vols = await apiFetch<VolumeRes[]>(
          `/api/projects/${projectId}/volumes`
        )
        const normalized = vols.map((v) => normalizeVolume(v as unknown as Record<string, unknown>))
        setVolumes(normalized)

        const chs = await apiFetch<ChapterRes[]>(
          `/api/projects/${projectId}/chapters?lightweight=true`
        )
        const normChs = chs.map((c) => normalizeChapter(c as unknown as Record<string, unknown>))
        setChapters(normChs)

        const outlines = await apiFetch<OutlineRes[]>(
          `/api/projects/${projectId}/outlines`
        )

        // Load book outline into preview (prefer confirmed).
        // Filter out legacy garbage: book-level records whose content is actually
        // a volume outline (has volume_idx key). Sort by id ascending to prefer
        // the earliest valid record.
        const bookOutlines = outlines
          .filter((o) => o.level === 'book')
          .filter((o) => {
            const cj = (o.content_json as Record<string, unknown>) || {}
            return !('volume_idx' in cj)
          })
          .sort((a, b) => (a.id < b.id ? -1 : 1))
        const bookOutline =
          bookOutlines.find((o) => o.is_confirmed) || bookOutlines[0] || null
        if (bookOutline) {
          const cj = bookOutline.content_json as Record<string, unknown> | null
          const raw = String(cj?.raw_text || JSON.stringify(cj, null, 2) || '')
          setOutlinePreview(raw)
          setConfirmedOutlineId(bookOutline.id)
          setBookOutlineData(cj)  // PR-OL14
          setBookOutlineId(bookOutline.id)  // PR-OUTLINE-CENTER-EDIT
        } else {
          setOutlinePreview('')
          setConfirmedOutlineId(null)
          setBookOutlineData(null)  // PR-OL14
          setBookOutlineId(null)  // PR-OUTLINE-CENTER-EDIT
        }

        // Index volume outlines by volume_idx (pick the most recent per idx)
        const volOutlineMap: Record<number, Record<string, unknown>> = {}
        const volOutlineIdMap: Record<number, string> = {}
        for (const o of outlines) {
          if (o.level !== 'volume') continue
          const cj = (o.content_json as Record<string, unknown>) || {}
          const idx = typeof cj.volume_idx === 'number' ? cj.volume_idx : null
          if (idx !== null) {
            volOutlineMap[idx] = cj
            // Prefer confirmed; otherwise overwrite with later id (keeps last-loaded).
            volOutlineIdMap[idx] = o.id
          }
        }
        setVolumeOutlines(volOutlineMap)
        setVolumeOutlineIds(volOutlineIdMap)  // PR-OUTLINE-CENTER-EDIT

        // Route to appropriate view
        if (normalized.length > 0) {
          setActiveView('editor')
        } else if (bookOutline) {
          setActiveView('wizard')
          setWizardStep(2)
        } else {
          setActiveView('wizard')
          setWizardStep(1)
        }
      } catch (err) {
        console.error('Failed to load project data:', err)
      }
    },
    [setVolumes, setChapters]
  )

  useEffect(() => {
    if (currentProject) {
      loadProjectData(currentProject.id)
    }
  }, [currentProject?.id]) // eslint-disable-line react-hooks/exhaustive-deps

  const refreshOutlineReadiness = useCallback(async () => {
    if (!currentProject?.id) {
      setOutlineReadiness(null)
      return
    }
    if (!selectedChapterId) {
      setOutlineReadiness(null)
      return
    }
    setOutlineReadinessLoading(true)
    try {
      const report = await apiFetch<OutlineReadinessRes>(
        `/api/projects/${currentProject.id}/outline-readiness?chapter_id=${selectedChapterId}`,
      )
      setOutlineReadiness(report)
    } catch (err) {
      console.warn('Failed to load outline readiness:', err)
      setOutlineReadiness(null)
    } finally {
      setOutlineReadinessLoading(false)
    }
  }, [currentProject?.id, selectedChapterId])

  useEffect(() => {
    void refreshOutlineReadiness()
  }, [refreshOutlineReadiness, selectedChapterId, currentProject?.id, chapters.length])

  // PR-FIX-WIZARD-LOCK-V2: 只在 volumes 从 0 变为 >0 那一刻保护一次，避免 init race 退回引导。
  // 不能持续拦截，否则用户主动点“查看分卷/编辑大纲”会被即刻拉回 editor。
  const prevVolCountRef = useRef(volumes.length)
  useEffect(() => {
    const prev = prevVolCountRef.current
    prevVolCountRef.current = volumes.length
    if (prev === 0 && volumes.length > 0 && activeView === 'wizard') {
      setActiveView('editor')
    }
  }, [volumes.length, activeView])

  // ----------------------------------------------------------------
  // Outline generation (SSE)
  // ----------------------------------------------------------------
  const handleGenerateOutline = useCallback(
    (level: string) => {
      if (isGenerating) return
      if (level === 'chapter') {
        if (!currentProject || !selectedChapterId) {
          setGenerationError('请先在左侧选择要生成章节大纲的章节。')
          return
        }
        setIsGenerating(true)
        setGenerationError(null)
        setActiveView('editor')
        apiFetch<{ outline_json?: Record<string, unknown> }>(
          `/api/projects/${currentProject.id}/chapters/${selectedChapterId}/outline/expand`,
          { method: 'POST' },
        )
          .then((res) => {
            const outlineJson = res.outline_json || {}
            const targetChapter = chapters.find((c) => c.id === selectedChapterId)
            setChapters(
              chapters.map((c) =>
                c.id === selectedChapterId ? { ...c, outline_json: outlineJson } : c
              ),
            )
            setOutlineEditorTarget({
              type: 'chapter',
              chapterId: selectedChapterId,
              initialJson: outlineJson,
              title: `${targetChapter?.title || ''} · 章节大纲`,
            })
            void refreshOutlineReadiness()
          })
          .catch((err) => {
            const msg = err instanceof Error ? err.message : String(err)
            setGenerationError(
              msg === '[object Object]'
                ? '章节大纲生成失败，请先补齐全书大纲和当前卷大纲。'
                : `章节大纲生成失败：${msg}`,
            )
          })
          .finally(() => setIsGenerating(false))
        return
      }
      setIsGenerating(true)
      setOutlinePreview('')
      if (level === 'book') {
        pendingBookOutlineIdRef.current = null
        setConfirmedOutlineId(null)
        // PR-STEP1-EDIT: also clear bookOutlineId so the stale id from the
        // previous outline does not let the edit button leak across regen.
        setBookOutlineId(null)
        setActiveView('wizard')
        resetStageStates()
      } else {
        setActiveView('outline')
      }

      sseControllerRef.current?.abort()
      sseControllerRef.current = apiSSE(
        '/api/generate/outline',
        {
          project_id: currentProject?.id || '',
          level,
          user_input: creativeInput,
          // v1.4.2 Task B: request structured staged SSE events for the
          // book outline so we can drive per-stage progress indicators.
          ...(level === 'book' ? { staged_stream: true } : {}),
        },
        (text) => {
          setOutlinePreview((prev) => prev + text)
        },
        () => {
          setIsGenerating(false)
        },
        (evt) => {
          if (level === 'book' && evt.status === 'saved' && typeof evt.outline_id === 'string') {
            pendingBookOutlineIdRef.current = evt.outline_id
            // PR-STEP1-EDIT: surface the freshly-saved (unconfirmed) outline
            // id as React state so the「编辑」button appears immediately,
            // not only after 「确认大纲」is pressed.
            setBookOutlineId(evt.outline_id)
          }
          // v1.4.2 Task B: structured staged events for book outline.
          if (level === 'book' && typeof evt.event === 'string') {
            const kind = evt.event as string
            const stage = evt.stage as StageKey | undefined
            if (kind === 'stage_start' && stage) {
              setStageStates((s) => ({ ...s, [stage]: 'running' }))
            } else if (kind === 'stage_chunk' && stage && typeof evt.delta === 'string') {
              setOutlinePreview((prev) => prev + (evt.delta as string))
            } else if (kind === 'stage_end' && stage) {
              setStageStates((s) => ({ ...s, [stage]: 'done' }))
            } else if (kind === 'error' && stage) {
              setStageStates((s) => ({ ...s, [stage]: 'error' }))
            } else if (kind === 'done') {
              const full = evt.full_outline
              if (typeof full === 'string' && full.length > 0) {
                // Replace the per-chunk interleaved preview with the
                // canonical reassembled 9-section outline.
                setOutlinePreview(full)
              }
              // PR-OL1: capture AI-suggested volume plan for step 2 prefill.
              if (Array.isArray(evt.volume_plan)) {
                setVolumePlan(evt.volume_plan as typeof volumePlan)
                if (evt.volume_plan.length > 0) {
                  setVolumeCountInput(String(evt.volume_plan.length))
                }
              }
            }
          }
        },
        (err) => {
          // outlinePreview is the visible surface in both wizard and outline
          // views, so surface transport errors there.
          setOutlinePreview((prev) => prev + `\n\n[错误] 大纲生成失败：${err.message}`)
        },
      )
    },
    [
      isGenerating,
      currentProject,
      creativeInput,
      setIsGenerating,
      selectedChapterId,
      chapters,
      setChapters,
      refreshOutlineReadiness,
    ]
  )

  // ----------------------------------------------------------------
  // Confirm outline => mark auto-saved outline as confirmed, advance wizard
  // ----------------------------------------------------------------
  const handleConfirmOutline = useCallback(async () => {
    if (!currentProject || !outlinePreview) return

    try {
      // Backend auto-saved the outline during SSE; we captured its id.
      // Fall back to fetching the latest unconfirmed book outline if we missed the event.
      let outlineId = pendingBookOutlineIdRef.current
      if (!outlineId) {
        const existing = await apiFetch<OutlineRes[]>(
          `/api/projects/${currentProject.id}/outlines?level=book`
        )
        const latest = [...existing]
          .filter((o) => !o.is_confirmed)
          .sort((a, b) => (a.id < b.id ? 1 : -1))[0]
        outlineId = latest?.id ?? null
      }

      if (!outlineId) {
        console.error('No book outline found to confirm')
        return
      }

      await apiFetch<OutlineRes>(
        `/api/projects/${currentProject.id}/outlines/${outlineId}/confirm`,
        { method: 'POST' }
      )

      setConfirmedOutlineId(outlineId)
      pendingBookOutlineIdRef.current = null
      setWizardStep(2)

      // Fire-and-forget: extract structured characters + world rules for later use.
      // Ignore failures; user can still proceed.
      apiFetch<{ characters_created: number; world_rules_created: number }>(
        `/api/projects/${currentProject.id}/outlines/${outlineId}/extract-settings`,
        { method: 'POST' }
      )
        .then((r) =>
          console.info(
            `Extracted settings: ${r.characters_created} characters, ${r.world_rules_created} world rules`
          )
        )
        .catch((err) => console.warn('Settings extraction failed:', err))
    } catch (err) {
      console.error('Failed to confirm outline:', err)
    }
  }, [currentProject, outlinePreview])

  // ----------------------------------------------------------------
  // Wizard Step 2: Generate volume outlines (loop) + create chapters
  // ----------------------------------------------------------------
  const handleGenerateVolumeOutlines = useCallback(async () => {
    if (!currentProject || isGenerating) return
    if (!confirmedOutlineId) {
      setWizardProgress('找不到已确认的全书大纲，请返回第一步重新生成。')
      return
    }
    const trimmed = volumeCountInput.trim()
    let count: number
    if (trimmed) {
      const parsed = parseInt(trimmed, 10)
      if (Number.isNaN(parsed) || parsed < 1) {
        setWizardProgress('卷数必须是大于 0 的整数。')
        return
      }
      count = Math.min(20, parsed)
    } else {
      const detected = detectVolumeCount(outlinePreview)
      count = detected > 0 ? Math.min(20, detected) : 3
      setWizardProgress(
        detected > 0
          ? `已从大纲识别 ${detected} 卷，开始生成...`
          : `未能从大纲识别卷数，按默认 3 卷生成...`
      )
    }

    setIsGenerating(true)
    if (trimmed) {
      setWizardProgress(`准备生成 ${count} 卷大纲...`)
    }
    setVolumeOutlines({})

    const createdVolumes: Volume[] = []
    const createdChapters: Chapter[] = []
    const outlinesByIdx: Record<number, Record<string, unknown>> = {}

    try {
      const isEmptyOrInvalid = (p: Record<string, unknown>) => {
        const hasStructure =
          typeof p.title === 'string' ||
          Array.isArray(p.chapter_summaries) ||
          typeof p.core_conflict === 'string'
        return !hasStructure
      }

      for (let i = 1; i <= count; i++) {
        // Unmount aborts the current stream but not this loop; without this
        // guard the loop would keep opening new streams nobody can abort.
        if (unmountedRef.current) return
        const existing = volumes.find(
          (v) => (v.volume_idx ?? v.volumeIdx) === i
        )
        if (existing) {
          setWizardProgress((prev) => prev + `\n第 ${i} 卷已存在，跳过`)
          continue
        }

        const runOnce = async (): Promise<{
          text: string
          outlineId: string | null
          parsed: Record<string, unknown>
        }> => {
          let text = ''
          let outlineId: string | null = null
          await new Promise<void>((resolve) => {
            // Per-volume stream in a sequential loop: the previous volume's
            // stream has already finished by the time we get here, so the
            // abort is a no-op then — it only matters for stale streams from
            // other entry points.
            sseControllerRef.current?.abort()
            sseControllerRef.current = apiSSE(
              '/api/generate/outline',
              {
                project_id: currentProject.id,
                level: 'volume',
                volume_idx: i,
                parent_outline_id: confirmedOutlineId,
                user_input: creativeInput,
              },
              (t) => {
                text += t
                setWizardProgress(
                  `正在生成第 ${i}/${count} 卷大纲...\n${text.slice(-200)}`
                )
              },
              () => resolve(),
              (evt) => {
                if (evt.status === 'saved' && typeof evt.outline_id === 'string') {
                  outlineId = evt.outline_id
                }
              },
              (err) => {
                setWizardProgress((prev) => prev + `\n⚠ 第 ${i} 卷大纲生成出错：${err.message}`)
              },
            )
          })
          return { text, outlineId, parsed: parseVolumeOutline(text) }
        }

        setWizardProgress(`正在生成第 ${i}/${count} 卷大纲...`)
        let { outlineId: volumeOutlineId, parsed } = await runOnce()
        if (isEmptyOrInvalid(parsed)) {
          // An unmount abort truncates the stream, which looks like an
          // invalid outline — don't start a retry stream after unmount.
          if (unmountedRef.current) return
          setWizardProgress((prev) => prev + `\n第 ${i} 卷首次生成无效，重试中...`)
          const retry = await runOnce()
          volumeOutlineId = retry.outlineId
          parsed = retry.parsed
        }
        if (isEmptyOrInvalid(parsed)) {
          setWizardProgress((prev) => prev + `\n⚠ 第 ${i} 卷生成失败，已跳过`)
          continue
        }

        outlinesByIdx[i] = parsed
        setVolumeOutlines((prev) => ({ ...prev, [i]: parsed }))

        // Persist the parsed structure back on the outline record (best effort)
        if (volumeOutlineId) {
          apiFetch(
            `/api/projects/${currentProject.id}/outlines/${volumeOutlineId}`,
            {
              method: 'PUT',
              body: JSON.stringify({ content_json: parsed }),
            }
          ).catch((err) => console.warn('Failed to store structured volume outline:', err))
        }

        const volumeTitle =
          typeof parsed.title === 'string' && parsed.title.trim()
            ? parsed.title.trim()
            : `第${i}卷`
        const volumeSummary =
          typeof parsed.core_conflict === 'string'
            ? parsed.core_conflict
            : typeof parsed.emotional_arc === 'string'
              ? parsed.emotional_arc
              : null

        const vol = await apiFetch<VolumeRes>(
          `/api/projects/${currentProject.id}/volumes`,
          {
            method: 'POST',
            body: JSON.stringify({
              title: volumeTitle,
              volume_idx: i,
              summary: volumeSummary,
            }),
          }
        )
        const normVol = normalizeVolume(vol as unknown as Record<string, unknown>)
        createdVolumes.push(normVol)
        setVolumes([...createdVolumes])

        const chapterSummaries = Array.isArray(parsed.chapter_summaries)
          ? (parsed.chapter_summaries as Array<Record<string, unknown>>)
          : []

        for (let ci = 0; ci < chapterSummaries.length; ci++) {
          const cs = chapterSummaries[ci] || {}
          const chapterIdx =
            typeof cs.chapter_idx === 'number' ? cs.chapter_idx : ci + 1
          const chapterTitle =
            typeof cs.title === 'string' && cs.title.trim()
              ? cs.title.trim()
              : `第${chapterIdx}章`
          const ch = await apiFetch<ChapterRes>(
            `/api/projects/${currentProject.id}/chapters`,
            {
              method: 'POST',
              body: JSON.stringify({
                volume_id: normVol.id,
                title: chapterTitle,
                chapter_idx: chapterIdx,
                outline_json: cs,
              }),
            }
          )
          createdChapters.push(
            normalizeChapter(ch as unknown as Record<string, unknown>)
          )
        }
        setChapters([...createdChapters])
      }

      setWizardProgress(
        `已生成 ${createdVolumes.length} 卷，共 ${createdChapters.length} 章。`
      )
      setWizardStep(3)
    } catch (err) {
      console.error('Failed to generate volume outlines:', err)
      setWizardProgress('生成失败，请重试。错误：' + (err instanceof Error ? err.message : String(err)))
    } finally {
      setIsGenerating(false)
    }
  }, [
    currentProject,
    isGenerating,
    creativeInput,
    confirmedOutlineId,
    volumeCountInput,
    outlinePreview,
    volumes,
    setIsGenerating,
    setVolumes,
    setChapters,
  ])

  // ----------------------------------------------------------------
  // Chapter editor: load chapter content when selected
  // ----------------------------------------------------------------
  useEffect(() => {
    if (!selectedChapterId || !currentProject) return
    setActiveView('editor')

    apiFetch<ChapterRes>(
      `/api/projects/${currentProject.id}/chapters/${selectedChapterId}`
    )
      .then((ch) => {
        setEditorContent(ch.content_text || '')
        lastSavedRef.current = ch.content_text || ''
        updateChapterContent(ch.id, ch.content_text || '')
        updateChapterStatus(
          ch.id,
          ch.status as 'draft' | 'generating' | 'completed'
        )
      })
      .catch((err) => console.error('Failed to load chapter:', err))
  }, [selectedChapterId, currentProject, updateChapterContent, updateChapterStatus])

  // ----------------------------------------------------------------
  // Auto-save editor content (debounced 3s)
  // ----------------------------------------------------------------
  const handleEditorChange = useCallback(
    (value: string) => {
      setEditorContent(value)
      if (selectedChapterId) {
        updateChapterContent(selectedChapterId, value)
      }

      if (saveTimerRef.current) clearTimeout(saveTimerRef.current)

      if (selectedChapterId && currentProject && value !== lastSavedRef.current) {
        saveTimerRef.current = setTimeout(() => {
          apiFetch<ChapterRes>(
            `/api/projects/${currentProject.id}/chapters/${selectedChapterId}`,
            {
              method: 'PUT',
              body: JSON.stringify({ content_text: value }),
            }
          )
            .then(() => {
              lastSavedRef.current = value
            })
            .catch((err) => console.error('Auto-save failed:', err))
        }, 3000)
      }
    },
    [selectedChapterId, currentProject, updateChapterContent]
  )

  // ----------------------------------------------------------------
  // Generate chapter content (SSE)
  // ----------------------------------------------------------------
  const handleGenerateChapter = useCallback(() => {
    if (isGenerating || !selectedChapterId || !currentProject) return
    const currentChapterBeforeGenerate = chapters.find((c) => c.id === selectedChapterId)
    const baselineContent =
      currentChapterBeforeGenerate?.content_text ??
      currentChapterBeforeGenerate?.contentText ??
      editorContent
    generationBaselineRef.current = {
      content: baselineContent,
      status: currentChapterBeforeGenerate?.status ?? 'draft',
    }
    generationSavedRef.current = false
    generationFailedRef.current = false
    setGenerationError(null)
    setIsGenerating(true)
    resetStreamContent()
    generationBufferRef.current = ''
    setEditorContent('')
    setActiveView('editor')
    updateChapterStatus(selectedChapterId, 'generating')

    sseControllerRef.current?.abort()
    sseControllerRef.current = apiSSE(
      '/api/generate/chapter',
      {
        project_id: currentProject.id,
        chapter_id: selectedChapterId,
        style_id: getSelectedStyleId(),
      },
      (text) => {
        generationBufferRef.current += text
        appendStreamContent(text)
        setEditorContent(generationBufferRef.current)
        updateChapterContent(selectedChapterId, generationBufferRef.current)
      },
      () => {
        setIsGenerating(false)
        if (generationFailedRef.current) {
          updateChapterStatus(selectedChapterId, 'needs_review')
          lastSavedRef.current = generationBufferRef.current
          return
        }
        if (generationSavedRef.current) {
          updateChapterStatus(selectedChapterId, 'completed')
          lastSavedRef.current = generationBufferRef.current
          return
        }
        const baseline = generationBaselineRef.current
        generationBufferRef.current = baseline?.content ?? ''
        resetStreamContent()
        if (generationBufferRef.current) appendStreamContent(generationBufferRef.current)
        setEditorContent(generationBufferRef.current)
        updateChapterContent(selectedChapterId, generationBufferRef.current)
        updateChapterStatus(selectedChapterId, baseline?.status ?? 'draft')
      },
      (evt) => {
        const eventName = String(evt.event ?? evt.status ?? '')
        if (eventName === 'fallback_restart' || eventName === 'revise_restart') {
          // Backend Task 3 (b5273da): the scene stream failed mid-way and the
          // single-shot fallback will re-send the FULL chapter text next.
          // Discard the partial scene chunks streamed so far — i.e. return
          // the streaming buffer to its empty generation-start state — so the
          // resent full text is not appended onto stale partial scenes.
          // revise_restart (emitted by apiSSE on a new revise_round) is the
          // same situation: the auto-revise loop re-sends the full chapter.
          generationBufferRef.current = ''
          resetStreamContent()
          setEditorContent('')
          updateChapterContent(selectedChapterId, '')
          return
        }
        if (eventName === 'generation_blocked' || evt.reason === 'outline_chain_incomplete') {
          const baseline = generationBaselineRef.current
          generationBufferRef.current = baseline?.content ?? ''
          resetStreamContent()
          if (generationBufferRef.current) appendStreamContent(generationBufferRef.current)
          setEditorContent(generationBufferRef.current)
          updateChapterContent(selectedChapterId, generationBufferRef.current)
          updateChapterStatus(selectedChapterId, baseline?.status ?? 'draft')
          setGenerationError(String(evt.message ?? '大纲链路未完成，不能生成正文。'))
          void refreshOutlineReadiness()
          return
        }
        if (eventName === 'saved') {
          generationSavedRef.current = true
          updateChapterStatus(selectedChapterId, 'completed')
          return
        }
        if (eventName === 'quality_failed') {
          generationFailedRef.current = true
          const blockedDraft =
            typeof evt.content_text === 'string'
              ? evt.content_text
              : typeof evt.final_text === 'string'
                ? evt.final_text
                : generationBufferRef.current
          generationBufferRef.current = blockedDraft
          resetStreamContent()
          if (generationBufferRef.current) appendStreamContent(generationBufferRef.current)
          setEditorContent(generationBufferRef.current)
          updateChapterContent(selectedChapterId, generationBufferRef.current)
          updateChapterStatus(selectedChapterId, 'needs_review')
          lastSavedRef.current = generationBufferRef.current
          if (generationBufferRef.current.trim()) {
            void apiFetch<ChapterRes>(
              `/api/projects/${currentProject.id}/chapters/${selectedChapterId}`,
              {
                method: 'PUT',
                body: JSON.stringify({
                  content_text: generationBufferRef.current,
                  status: 'needs_review',
                }),
              },
            ).catch((err) => console.error('Failed to stage review draft:', err))
          }
          const report = evt.report as Record<string, unknown> | undefined
          const reason = String(evt.reason ?? 'quality_gate_blocked')
          const summary = report
            ? [
                `dialogue=${report.dialogue_symmetry_risk_count ?? 0}`,
                `duplicate=${report.duplicate_short_dialogue_ladder_count ?? 0}`,
                `spatial=${report.spatial_mapping_count ?? 0}`,
                `bio=${report.biographical_infodump_count ?? 0}`,
                `plain=${report.plain_contemporary_violation_count ?? 0}`,
                `pseudo=${report.pseudo_literary_register_count ?? 0}`,
                `repeat=${report.duplicate_explanation_span_count ?? 0}`,
              ].join(', ')
            : ''
          setGenerationError(`质量门禁阻断保存：${reason}${summary ? ` (${summary})` : ''}`)
          return
        }
        const replacement =
          typeof evt.content_text === 'string'
            ? evt.content_text
            : typeof evt.final_text === 'string'
              ? evt.final_text
              : null
        if (!replacement) return
        if (eventName === 'quality_rewrite_done' || eventName === 'quality_warning') {
          generationBufferRef.current = replacement
          resetStreamContent()
          appendStreamContent(replacement)
          setEditorContent(replacement)
          updateChapterContent(selectedChapterId, replacement)
        }
      },
      (err) => {
        // onDone (guaranteed by apiSSE) restores the baseline content and
        // status; here we only surface the error to the user.
        setGenerationError(`正文生成失败：${err.message}`)
      }
    )
  }, [
    isGenerating,
    selectedChapterId,
    currentProject,
    chapters,
    editorContent,
    setIsGenerating,
    resetStreamContent,
    appendStreamContent,
    updateChapterContent,
    updateChapterStatus,
    refreshOutlineReadiness,
  ])

  // ----------------------------------------------------------------
  // Helpers: extract volume/chapter titles from outline text
  // ----------------------------------------------------------------

  // Cleanup on unmount
  useEffect(() => {
    // Reset on (re)mount so a StrictMode dev remount doesn't stay flagged.
    unmountedRef.current = false
    return () => {
      unmountedRef.current = true
      if (saveTimerRef.current) clearTimeout(saveTimerRef.current)
      // Task F2: stop any in-flight SSE stream so it cannot setState on an
      // unmounted component.
      sseControllerRef.current?.abort()
    }
  }, [])

  // ----------------------------------------------------------------
  // Get current chapter info
  // ----------------------------------------------------------------
  const currentChapter = selectedChapterId
    ? chapters.find((c) => c.id === selectedChapterId)
    : null
  const outlineLayerLabels: Record<string, string> = {
    book: '全书大纲',
    volume: '当前卷大纲',
    chapter: '本章大纲',
  }
  const outlineMissingLayers = outlineReadiness?.missing_layers || []
  const canGenerateChapterProse = Boolean(selectedChapterId && outlineReadiness?.ready)
  const outlineReadinessText = !selectedChapterId
    ? '先选中一章，再看这章的链路状态。'
    : outlineReadinessLoading
      ? '正在检查大纲链路...'
      : outlineReadiness?.ready
        ? '链路完整，可生成正文。'
        : outlineReadiness?.block_message || '大纲链路未完成。'
  const completedChapters = chapters.filter((c) => c.status === 'completed').length
  const reviewChapters = chapters.filter((c) => c.status === 'needs_review').length
  const draftChapters = chapters.filter((c) => c.status === 'draft').length
  const totalWords = chapters.reduce(
    (sum, chapter) => sum + Number(chapter.word_count ?? chapter.wordCount ?? 0),
    0,
  )
  const chaptersWithOutlines = chapters.filter((c) => Boolean(c.outline_json)).length
  const volumeOutlineCount = Object.keys(volumeOutlines).length
  const totalMemoryLayers =
    (bookOutlineData ? 1 : 0) + volumeOutlineCount + chaptersWithOutlines
  const currentChapterVolume = currentChapter
    ? volumes.find((volume) => volume.id === (currentChapter.volume_id ?? currentChapter.volumeId))
    : null
  const currentVolumeIdx = currentChapterVolume
    ? currentChapterVolume.volume_idx ?? currentChapterVolume.volumeIdx
    : null
  const currentVolumeOutline =
    typeof currentVolumeIdx === 'number' ? volumeOutlines[currentVolumeIdx] : null
  const currentChapterOutline = currentChapter?.outline_json ?? null
  const canonReady = Boolean(bookOutlineData && currentVolumeOutline && currentChapterOutline)
  const draftGateTone =
    !selectedChapterId ? 'gray' : reviewChapters > 0 ? 'red' : canonReady ? 'green' : 'amber'
  const draftGateText = !selectedChapterId
    ? '未选章节'
    : reviewChapters > 0
      ? `${reviewChapters} 章需复核`
      : canonReady
        ? '可进入正式生成'
        : '大纲链路未齐'

  // ================================================================
  // RENDER
  // ================================================================

  return (
    <>
    {/* Drawer overlay for large panels */}
    {drawerPanel && currentProject && (
      <div className="fixed inset-0 z-50 flex">
        <div className="absolute inset-0 bg-black/30" onClick={() => setDrawerPanel(null)} />
        <div className="relative ml-auto w-full max-w-2xl bg-white shadow-xl overflow-y-auto">
          <div className="sticky top-0 bg-white border-b border-gray-200 px-6 py-4 flex items-center justify-between z-10">
            <h2 className="text-lg font-semibold text-gray-900">
              {drawerPanel === 'strand' ? '三线平衡' :
               drawerPanel === 'foreshadow' ? '伏笔追踪' :
               drawerPanel === 'settings' ? '设定集' : '角色关系'}
            </h2>
            <button onClick={() => setDrawerPanel(null)}
              className="text-gray-400 hover:text-gray-600 text-lg">&#x2715;</button>
          </div>
          <div className="p-6">
            {drawerPanel === 'strand' && <StrandPanel projectId={currentProject.id} />}
            {drawerPanel === 'foreshadow' && <ForeshadowPanel projectId={currentProject.id} />}
            {drawerPanel === 'settings' && <SettingsPanel projectId={currentProject.id} />}
            {drawerPanel === 'relationship' && <CharacterCardPanel projectId={currentProject.id} />}
          </div>
        </div>
      </div>
    )}

    <WorkspaceLayout
      projectId={currentProject?.id}
      sidebar={
        <div className="flex flex-col h-full">
          {/* ---- Header: back to project list + current title ---- */}
          <div className="p-4 border-b border-gray-200">
            <button
              onClick={() => router.push('/')}
              className="flex items-center gap-1 text-xs text-gray-500 hover:text-gray-900 mb-2"
            >
              <span>←</span>
              <span>返回项目列表</span>
            </button>
            <h2 className="text-lg font-semibold text-gray-900 truncate">
              {currentProject?.title || 'AI Write'}
            </h2>
            <div className="mt-3 grid grid-cols-2 gap-2">
              <WorkbenchMetric label="卷册" value={volumes.length} hint="分卷大纲" />
              <WorkbenchMetric label="章节" value={chapters.length} hint={`${completedChapters} 完成`} />
            </div>
            <div className="mt-2 flex flex-wrap gap-1.5">
              <StatusPill tone={bookOutlineData ? 'green' : 'amber'}>全书大纲</StatusPill>
              <StatusPill tone={volumeOutlineCount > 0 ? 'green' : 'amber'}>{volumeOutlineCount} 个分卷</StatusPill>
              <StatusPill tone={chaptersWithOutlines > 0 ? 'green' : 'amber'}>{chaptersWithOutlines} 个细纲</StatusPill>
            </div>
          </div>

          {/* ---- Volume/Chapter tree ---- */}
          <div className="flex-1 overflow-y-auto">
            <div className="p-3">
              <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-2">
                卷 / 章节
              </h3>
              <OutlineTree
                projectId={currentProject?.id || ''}
                volumeOutlines={volumeOutlines}
                bookOutline={bookOutlineData}
                bookOutlineId={bookOutlineId}
                volumeOutlineIds={volumeOutlineIds}
                onSelectOutline={(target) => {
                  // PR-OUTLINE-CENTER-EDIT: switch the centre editor to outline mode.
                  setOutlineEditorTarget(target)
                  selectChapter(null)
                  setActiveView('editor')
                }}
                selectedOutlineKey={
                  outlineEditorTarget
                    ? (outlineEditorTarget.type === 'chapter'
                        ? `chapter:${outlineEditorTarget.chapterId}`
                        : `${outlineEditorTarget.type}:${outlineEditorTarget.outlineId}`)
                    : null
                }
                onChanged={() => {
                  if (currentProject) loadProjectData(currentProject.id)
                }}
                onSelectChapter={(chapterId) => {
                  // PR-OUTLINE-CENTER-EDIT: chapter wins over outline editor.
                  setOutlineEditorTarget(null)
                  selectChapter(chapterId)
                  setActiveView('editor')
                }}
              />
            </div>
          </div>
        </div>
      }
      editor={
        <div className="h-full flex flex-col">
          {currentProject && (
            <div className="border-b border-gray-200 bg-white px-6 py-3">
              <div className="flex items-start justify-between gap-4">
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <PenLine className="h-4 w-4 text-gray-500" />
                    <h1 className="truncate text-base font-semibold text-gray-950">写作中枢</h1>
                    <StatusPill tone={draftGateTone}>草稿闸门：{draftGateText}</StatusPill>
                  </div>
                  <p className="mt-1 truncate text-xs text-gray-500">
                    {currentChapter
                      ? `${currentChapterVolume?.title || '未分卷'} / ${currentChapter.title}`
                      : bookOutlineData
                        ? '全书大纲、分卷大纲和章节细纲已接入目录，可从左侧打开。'
                        : '先建立全书大纲，再拆分分卷和章节细纲。'}
                  </p>
                </div>
                <div className="grid w-[420px] grid-cols-4 gap-2">
                  <WorkbenchMetric label="总字数" value={`${(totalWords / 1000).toFixed(1)}k`} hint="正式正文" />
                  <WorkbenchMetric label="完成" value={completedChapters} hint={`${chapters.length} 章`} />
                  <WorkbenchMetric label="记忆层" value={totalMemoryLayers} hint="大纲/细纲" />
                  <WorkbenchMetric label="待处理" value={reviewChapters + draftChapters} hint="草稿/复核" />
                </div>
              </div>
            </div>
          )}
          {/* PR-OUTLINE-CENTER-EDIT (2026-05-04): outline-in-centre takes priority. */}
          {outlineEditorTarget && currentProject ? (
            <OutlineEditor
              projectId={currentProject.id}
              target={outlineEditorTarget}
              onClose={() => setOutlineEditorTarget(null)}
              onSaved={(t, updatedJson) => {
                if (t.type === 'book') {
                  setBookOutlineData(updatedJson)
                  const rt = (updatedJson as Record<string, unknown>)['raw_text']
                  if (typeof rt === 'string') setOutlinePreview(rt)
                } else if (t.type === 'volume' && typeof t.volumeIdx === 'number') {
                  const idx = t.volumeIdx
                  setVolumeOutlines((prev) => ({ ...prev, [idx]: updatedJson }))
                }
                // chapter outline_json lives on the chapter row in projectStore;
                // avoid coupling here — reload on next loadProjectData if needed.
              }}
            />
          ) : (
          <>
          {/* ---- Outline Wizard ---- */}
          {activeView === 'wizard' && currentProject && (
            <div className="flex-1 p-8 overflow-y-auto">
              <div className="max-w-2xl mx-auto">
                {/* Wizard steps indicator */}
                <div className="flex items-center gap-2 mb-6">
                  {[1, 2, 3].map((step) => (
                    <div key={step} className="flex items-center gap-2">
                      <button
                        onClick={() => setWizardStep(step)}
                        className={`w-8 h-8 rounded-full flex items-center justify-center text-sm font-medium transition-colors ${
                          wizardStep === step
                            ? 'bg-blue-600 text-white'
                            : wizardStep > step
                              ? 'bg-green-500 text-white hover:bg-green-600'
                              : 'bg-gray-200 text-gray-500 hover:bg-gray-300'
                        }`}
                      >
                        {wizardStep > step ? '✓' : step}
                      </button>
                      {step < 3 && (
                        <div
                          className={`w-12 h-0.5 ${
                            wizardStep > step ? 'bg-green-500' : 'bg-gray-200'
                          }`}
                        />
                      )}
                    </div>
                  ))}
                </div>

                {/* Step 1: Book outline */}
                {wizardStep === 1 && (
                  <div>
                    <h2 className="text-xl font-bold text-gray-900 mb-2">
                      第一步：输入你的小说创意
                    </h2>
                    <p className="text-gray-500 mb-4 text-sm">
                      描述你的小说设定、主要角色和核心剧情，AI 将为你生成完整的全书大纲。
                    </p>
                    <textarea
                      value={creativeInput}
                      onChange={(e) => setCreativeInput(e.target.value)}
                      placeholder={`例如：\n都市修仙，主角是一个外卖员，意外获得一本修炼功法...\n\n描述类型、背景、主角设定和核心故事。`}
                      className="w-full h-48 px-4 py-3 text-sm border border-gray-300 rounded-xl resize-none focus:ring-2 focus:ring-blue-500 focus:border-transparent"
                      disabled={isGenerating}
                    />

                    <div className="flex gap-3 mt-4">
                      <button
                        onClick={() => handleGenerateOutline('book')}
                        disabled={isGenerating || !creativeInput.trim()}
                        className="px-6 py-2.5 bg-blue-600 text-white rounded-xl hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed text-sm font-medium"
                      >
                        {isGenerating ? '正在生成...' : '生成全书大纲'}
                      </button>
                    </div>

                    {/* Outline preview */}
                    {outlinePreview && (
                      <div className="mt-6">
                        <div className="flex items-center justify-between mb-2">
                          <h3 className="text-sm font-semibold text-gray-700">大纲预览</h3>
                          {!isGenerating && bookOutlineId && (
                            <button
                              onClick={() => setOutlineEditing((v) => !v)}
                              className="text-xs text-blue-600 hover:underline"
                            >
                              {outlineEditing ? '取消编辑' : '编辑'}
                            </button>
                          )}
                        </div>
                        {/* v1.4.2 Task B: per-stage progress indicators. */}
                        {(stageStates.A !== 'idle' ||
                          stageStates.B !== 'idle' ||
                          stageStates.C !== 'idle') && (
                          <div className="flex items-center gap-4 mb-3 text-xs">
                            {(['A', 'B', 'C'] as const).map((k) => {
                              const st = stageStates[k]
                              const cls =
                                st === 'running'
                                  ? 'bg-blue-500 animate-pulse'
                                  : st === 'done'
                                    ? 'bg-green-500'
                                    : st === 'error'
                                      ? 'bg-red-500'
                                      : 'bg-gray-300'
                              return (
                                <div key={k} className="flex items-center gap-1.5">
                                  <span
                                    className={`inline-block w-2.5 h-2.5 rounded-full ${cls}`}
                                  />
                                  <span className="text-gray-600">{stageLabels[k]}</span>
                                </div>
                              )
                            })}
                          </div>
                        )}
                        {outlineEditing ? (
                          <div>
                            <textarea
                              value={outlinePreview}
                              onChange={(e) => setOutlinePreview(e.target.value)}
                              className="w-full h-96 px-4 py-3 text-sm border border-gray-300 rounded-xl resize-none font-mono"
                            />
                            <div className="mt-2 flex gap-2">
                              <button
                                onClick={async () => {
                                  // PR-STEP1-EDIT: bookOutlineId covers both unconfirmed (just
                                  // streamed) and confirmed states; confirmedOutlineId would be
                                  // null while user is still in the review-then-confirm window.
                                  const targetId = bookOutlineId || confirmedOutlineId
                                  if (!currentProject || !targetId) return
                                  await apiFetch(`/api/projects/${currentProject.id}/outlines/${targetId}`, {
                                    method: 'PUT',
                                    body: JSON.stringify({ content_json: { raw_text: outlinePreview } }),
                                  })
                                  setOutlineEditing(false)
                                }}
                                className="px-4 py-2 text-sm bg-green-600 text-white rounded-lg"
                              >
                                保存
                              </button>
                            </div>
                          </div>
                        ) : (
                          <pre className="whitespace-pre-wrap text-sm text-gray-800 bg-gray-50 p-4 rounded-xl border max-h-96 overflow-y-auto">
                            {outlinePreview}
                          </pre>
                        )}

                        {!isGenerating && !confirmedOutlineId && (
                          <div className="mt-4 flex gap-3">
                            <button
                              onClick={handleConfirmOutline}
                              className="px-4 py-2 bg-green-600 text-white rounded-lg hover:bg-green-700 text-sm font-medium"
                            >
                              确认大纲
                            </button>
                            <button
                              onClick={() => {
                                setOutlinePreview('')
                                handleGenerateOutline('book')
                              }}
                              className="px-4 py-2 bg-gray-200 text-gray-700 rounded-lg hover:bg-gray-300 text-sm"
                            >
                              重新生成
                            </button>
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                )}

                {/* Step 2: Volume outlines */}
                {wizardStep === 2 && (
                  <div>
                    <h2 className="text-xl font-bold text-gray-900 mb-2">
                      第二步：生成分卷大纲
                    </h2>
                    <p className="text-gray-500 mb-4 text-sm">
                      基于全书大纲，AI 将按卷逐个生成分卷大纲，并自动从章节摘要创建章节。
                    </p>

                    {/* Show confirmed book outline for reference */}
                    {outlinePreview && (
                      <details className="mb-4 border rounded-xl overflow-hidden" open>
                        <summary className="cursor-pointer px-4 py-2 bg-gray-50 text-sm font-medium text-gray-700 hover:bg-gray-100">
                          全书大纲（已确认）
                        </summary>
                        <pre
                          className="whitespace-pre-wrap text-sm text-gray-800 bg-white p-4 border-t max-h-72 overflow-y-auto leading-relaxed"
                          style={{ fontFamily: "'Noto Serif SC', serif" }}
                        >
                          {outlinePreview}
                        </pre>
                      </details>
                    )}

                    {volumePlan && volumePlan.length > 0 && (
                      <div className="mb-4 border border-blue-200 bg-blue-50 rounded-xl p-3">
                        <div className="flex items-center justify-between gap-2 mb-2">
                          <div className="flex items-center gap-2">
                            <span className="text-sm font-medium text-blue-900">📜 AI 推荐卷规划</span>
                            <span className="text-xs text-blue-700">共 {volumePlan.length} 卷，共 {volumePlan.reduce((s, v) => s + (v.est_chapters || 0), 0)} 章</span>
                          </div>
                          {!editingPlan ? (
                            <button
                              type="button"
                              onClick={() => setEditingPlan(true)}
                              className="text-xs px-2 py-1 rounded bg-white text-blue-700 border border-blue-300 hover:bg-blue-100"
                            >✎ 编辑</button>
                          ) : (
                            <div className="flex items-center gap-1.5">
                              <button
                                type="button"
                                disabled={savingPlan}
                                onClick={async () => {
                                  if (!currentProject || !pendingBookOutlineIdRef.current) {
                                    setEditingPlan(false)
                                    return
                                  }
                                  setSavingPlan(true)
                                  try {
                                    await apiFetch(
                                      `/api/projects/${currentProject.id}/outlines/${pendingBookOutlineIdRef.current}/volume-plan`,
                                      { method: "PATCH", body: JSON.stringify({ volume_plan: volumePlan }) },
                                    )
                                    setVolumeCountInput(String(volumePlan.length))
                                    // PR-OL5: post-save notice when stale volume outlines exist.
                                    if (volumes.length > 0) {
                                      setPlanSaveNotice(
                                        `卷规划已保存。检测到已生成 ${volumes.length} 个分卷大纲，如果卷名/章数有变动，请手动在底部列表中删除并点 “生成分卷大纲” 重生。`,
                                      )
                                    } else {
                                      setPlanSaveNotice('卷规划已保存。点 “生成分卷大纲” 开始创建。')
                                    }
                                  } catch (err) {
                                    console.error("保存卷规划失败:", err)
                                    setPlanSaveNotice('保存失败，请重试。')
                                  } finally {
                                    setSavingPlan(false)
                                    setEditingPlan(false)
                                  }
                                }}
                                className="text-xs px-2 py-1 rounded bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50"
                              >{savingPlan ? "保存中..." : "保存"}</button>
                              <button
                                type="button"
                                onClick={() => setEditingPlan(false)}
                                className="text-xs px-2 py-1 rounded bg-white text-gray-700 border border-gray-300 hover:bg-gray-100"
                              >取消</button>
                            </div>
                          )}
                        </div>
                        <div className="space-y-1.5">
                          {volumePlan.map((v, vi) => (
                            <div key={v.idx} className="text-xs text-gray-700 bg-white rounded px-2 py-1.5">
                              {!editingPlan ? (
                                <>
                                  <span className="font-semibold text-gray-900">第{v.idx}卷 {v.title}</span>
                                  <span className="text-gray-500 ml-1">({v.est_chapters}章)</span>
                                  {v.theme && <span className="text-gray-600"> · {v.theme}</span>}
                                  {v.core_conflict && <div className="text-gray-500 mt-0.5">冲突: {v.core_conflict}</div>}
                                </>
                              ) : (
                                <div className="flex items-center gap-1.5 flex-wrap">
                                  <span className="text-gray-500">第{v.idx}卷</span>
                                  <input
                                    type="text"
                                    value={v.title}
                                    onChange={(e) => {
                                      const next = [...volumePlan]
                                      next[vi] = { ...v, title: e.target.value }
                                      setVolumePlan(next)
                                    }}
                                    placeholder="卷名"
                                    className="flex-1 min-w-[100px] px-2 py-0.5 border border-gray-300 rounded text-xs"
                                  />
                                  <input
                                    type="number"
                                    min={1}
                                    value={v.est_chapters}
                                    onChange={(e) => {
                                      const next = [...volumePlan]
                                      next[vi] = { ...v, est_chapters: parseInt(e.target.value || "0", 10) || 0 }
                                      setVolumePlan(next)
                                    }}
                                    className="w-16 px-2 py-0.5 border border-gray-300 rounded text-xs"
                                  />
                                  <span className="text-gray-500">章</span>
                                  {v.theme && <div className="w-full text-gray-600 mt-0.5">主题: {v.theme}</div>}
                                </div>
                              )}
                            </div>
                          ))}
                        </div>
                      </div>
                    )}
                    {planSaveNotice && (
                      <div className="mb-3 border border-emerald-200 bg-emerald-50 rounded-lg p-2.5 flex items-start justify-between gap-2">
                        <span className="text-xs text-emerald-900">{planSaveNotice}</span>
                        <button
                          type="button"
                          onClick={() => setPlanSaveNotice(null)}
                          className="text-xs text-emerald-700 hover:underline"
                        >关闭</button>
                      </div>
                    )}
                    {(!volumePlan || volumePlan.length === 0) && outlinePreview && (
                      <div className="mb-4 border border-amber-200 bg-amber-50 rounded-xl p-3">
                        <div className="flex items-center gap-2 mb-1">
                          <span className="text-sm font-medium text-amber-900">⚠ AI 未输出结构化卷规划</span>
                          <span className="text-xs text-amber-700">(探测到 {detectVolumeCount(outlinePreview) || 3} 卷，已作为 fallback)</span>
                        </div>
                        <div className="text-xs text-amber-800">
                          请手动确认 / 调整下方“共 N 卷”，或返回修改大纲 让其明确输出 {"<volume-plan>"} JSON 块。
                        </div>
                      </div>
                    )}
                    <div className="mb-4 flex items-center gap-3 flex-wrap">
                      <label className="text-sm text-gray-700">共</label>
                      <input
                        type="number"
                        min={1}
                        max={20}
                        value={volumeCountInput}
                        onChange={(e) => setVolumeCountInput(e.target.value)}
                        placeholder="自动"
                        disabled={isGenerating}
                        className="w-24 px-3 py-1.5 text-sm border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent disabled:bg-gray-100 placeholder-gray-400"
                      />
                      <label className="text-sm text-gray-700">卷</label>
                      <span className="text-xs text-gray-400">
                        （留空则根据大纲自动判断）
                      </span>
                    </div>

                    {wizardProgress && (
                      <pre className="whitespace-pre-wrap text-sm text-gray-700 bg-gray-50 p-4 rounded-xl border mb-4 max-h-64 overflow-y-auto">
                        {wizardProgress}
                      </pre>
                    )}

                    <div className="flex gap-3">
                      <button
                        onClick={handleGenerateVolumeOutlines}
                        disabled={isGenerating || !confirmedOutlineId}
                        className="px-6 py-2.5 bg-indigo-600 text-white rounded-xl hover:bg-indigo-700 disabled:opacity-50 disabled:cursor-not-allowed text-sm font-medium"
                      >
                        {isGenerating ? '正在生成...' : (volumes.length > 0 ? '补齐缺失卷' : '生成分卷大纲')}
                      </button>
                      <button
                        onClick={() => setWizardStep(1)}
                        disabled={isGenerating}
                        className="px-6 py-2.5 border border-gray-300 text-gray-700 rounded-xl hover:bg-gray-50 disabled:opacity-50 text-sm"
                      >
                        返回修改大纲
                      </button>
                    </div>

                    {Object.keys(volumeOutlines).length > 0 && currentProject && (
                      <div className="mt-6 space-y-2">
                        <h3 className="text-sm font-semibold text-gray-700">已生成分卷</h3>
                        {volumes
                          .slice()
                          .sort((a, b) => (a.volume_idx ?? a.volumeIdx) - (b.volume_idx ?? b.volumeIdx))
                          .map((v) => {
                            const vi = v.volume_idx ?? v.volumeIdx
                            const vo = volumeOutlines[vi]
                            if (!vo) return null
                            return (
                              <VolumeOutlineEditor
                                key={v.id}
                                volume={v}
                                data={vo}
                                projectId={currentProject.id}
                                onSaved={(updated) => setVolumeOutlines((prev) => ({ ...prev, [vi]: updated }))}
                                onVolumeChanged={async () => {
                                  if (!currentProject) return
                                  try {
                                    const refreshed = await apiFetch<Volume[]>(`/api/projects/${currentProject.id}/volumes`)
                                    setVolumes(refreshed)
                                  } catch {}
                                }}
                              />
                            )
                          })}
                      </div>
                    )}
                  </div>
                )}

                {/* Step 3: Completion summary */}
                {wizardStep === 3 && (
                  <div>
                    <h2 className="text-xl font-bold text-gray-900 mb-2">
                      第三步：完成
                    </h2>
                    <p className="text-gray-500 mb-4 text-sm">
                      分卷与章节已创建完毕。点击下方按钮进入编辑器开始写作，也可展开各卷查看分卷大纲。
                    </p>

                    {wizardProgress && (
                      <div className="mb-4 px-4 py-2 bg-green-50 text-sm text-green-800 rounded-lg border border-green-200">
                        {wizardProgress}
                      </div>
                    )}

                    <div className="space-y-3 mb-4">
                      {volumes.map((v) => {
                        const volChapters = chapters.filter(
                          (c) => (c.volume_id ?? c.volumeId) === v.id
                        )
                        const vo = volumeOutlines[v.volume_idx ?? v.volumeIdx]
                        return (
                          <details
                            key={v.id}
                            className="border rounded-xl overflow-hidden"
                          >
                            <summary className="cursor-pointer px-4 py-2.5 bg-gray-50 hover:bg-gray-100 flex items-center justify-between">
                              <span className="font-medium text-gray-800">
                                {v.title}
                              </span>
                              <span className="text-xs text-gray-500">
                                {volChapters.length} 章
                              </span>
                            </summary>
                            <div className="px-4 py-3 bg-white border-t text-sm">
                              {vo ? (
                                <VolumeOutlineBlock data={vo} />
                              ) : (
                                <div className="text-gray-400">暂无大纲</div>
                              )}
                            </div>
                          </details>
                        )
                      })}
                    </div>

                    <div className="flex gap-3">
                      <button
                        onClick={() => {
                          // PR-FIX-START-CREATE: 进入 editor 同时选中第一章，避免 editor 退化为“全书大纲”返回页。
                          if (chapters.length > 0) {
                            const sorted = [...chapters].sort((a, b) => {
                              const av = (a as { volume_index?: number }).volume_index ?? 0
                              const bv = (b as { volume_index?: number }).volume_index ?? 0
                              if (av !== bv) return av - bv
                              const ai = (a as { chapter_index?: number; index?: number }).chapter_index ?? (a as { index?: number }).index ?? 0
                              const bi = (b as { chapter_index?: number; index?: number }).chapter_index ?? (b as { index?: number }).index ?? 0
                              return ai - bi
                            })
                            selectChapter(sorted[0].id)
                          }
                          setActiveView('editor')
                        }}
                        className="px-6 py-2.5 bg-purple-600 text-white rounded-xl hover:bg-purple-700 text-sm font-medium"
                      >
                        开始创作
                      </button>
                    </div>
                  </div>
                )}
              </div>
            </div>
          )}

          {/* ---- Outline preview (non-wizard mode) ---- */}
          {activeView === 'outline' && (
            <div className="flex-1 p-8 overflow-y-auto">
              <h2 className="text-xl font-bold text-gray-900 mb-4">
                大纲预览
              </h2>
              <pre className="whitespace-pre-wrap text-sm text-gray-800 bg-gray-50 p-6 rounded-xl border">
                {outlinePreview || '生成中...'}
              </pre>
              {!isGenerating && outlinePreview && (
                <div className="mt-4 flex gap-3">
                  <button
                    onClick={handleConfirmOutline}
                    className="px-4 py-2 bg-green-600 text-white rounded-lg hover:bg-green-700 text-sm"
                  >
                    确认大纲
                  </button>
                  <button
                    onClick={() => handleGenerateOutline('book')}
                    className="px-4 py-2 bg-gray-200 text-gray-700 rounded-lg hover:bg-gray-300 text-sm"
                  >
                    重新生成
                  </button>
                </div>
              )}
            </div>
          )}

          {/* ---- Chapter Editor ---- */}
          {activeView === 'editor' && (
            <div className="flex-1 overflow-y-auto">
              {/* Show outline if no chapter selected */}
              {!currentChapter && outlinePreview && (
                <div className="max-w-3xl mx-auto pt-4 px-6">
                  <div className="flex items-center justify-between mb-3">
                    <h2 className="text-lg font-bold text-gray-900">全书大纲</h2>
                    <div className="flex items-center gap-2">
                      <button onClick={() => { setActiveView('wizard'); setWizardStep(1) }}
                        className="px-3 py-1.5 text-xs border border-gray-300 text-gray-700 rounded-lg hover:bg-gray-50">
                        编辑大纲
                      </button>
                      <button onClick={() => { setActiveView('wizard'); setWizardStep(2) }}
                        className="px-3 py-1.5 text-xs border border-gray-300 text-gray-700 rounded-lg hover:bg-gray-50">
                        查看分卷
                      </button>
                    </div>
                  </div>
                  <pre className="whitespace-pre-wrap text-sm text-gray-700 bg-gray-50 p-4 rounded-xl border leading-relaxed"
                    style={{ fontFamily: "'Noto Serif SC', serif" }}>
                    {outlinePreview}
                  </pre>
                </div>
              )}
              {currentChapter && (
                <div className="max-w-3xl mx-auto pt-4 px-6">
                  <div className="flex items-center justify-between mb-2">
                    <h3 className="text-lg font-semibold text-gray-800">
                      {currentChapter.title}
                    </h3>
                    <div className="flex items-center gap-2">
                      <ChapterTargetWordsEditor
                        projectId={currentProject!.id}
                        chapter={currentChapter}
                        projectDefault={
                          currentProject?.settings_json?.target_chapter_words ?? null
                        }
                        onSaved={() => {
                          if (currentProject) loadProjectData(currentProject.id)
                        }}
                      />
                      <span className="text-xs text-gray-400">
                        {(
                          currentChapter.word_count ??
                          currentChapter.wordCount ??
                          editorContent.length
                        ).toLocaleString()}{' '}
                        字
                      </span>
                      <span
                        className={`text-xs px-2 py-0.5 rounded ${
                          currentChapter.status === 'completed'
                            ? 'bg-green-100 text-green-700'
                            : currentChapter.status === 'generating'
                              ? 'bg-yellow-100 text-yellow-700'
                              : currentChapter.status === 'needs_review'
                                ? 'bg-red-100 text-red-700'
                              : 'bg-gray-100 text-gray-600'
                        }`}
                      >
                        {currentChapter.status === 'completed'
                          ? '完成'
                          : currentChapter.status === 'generating'
                            ? '生成中'
                            : currentChapter.status === 'needs_review'
                              ? '需复核'
                            : '草稿'}
                      </span>
                    </div>
                  </div>
                </div>
              )}

              <div className="max-w-3xl mx-auto py-4 px-6">
                {selectedChapterId && (
                  <div className="mb-3">
                    <div
                      className={`mb-2 rounded border px-3 py-2 text-xs ${
                        canGenerateChapterProse
                          ? 'border-emerald-200 bg-emerald-50 text-emerald-800'
                          : 'border-amber-200 bg-amber-50 text-amber-800'
                      }`}
                    >
                      <div className="font-medium">大纲链路：{outlineReadinessText}</div>
                      {outlineMissingLayers.length > 0 && (
                        <div className="mt-1 flex flex-wrap gap-1">
                          {outlineMissingLayers.map((layer) => (
                            <span
                              key={layer}
                              className="rounded bg-white/70 px-1.5 py-0.5 text-[11px]"
                            >
                              缺 {outlineLayerLabels[layer] || layer}
                            </span>
                          ))}
                        </div>
                      )}
                    </div>
                    <button
                      onClick={handleGenerateChapter}
                      disabled={isGenerating || !canGenerateChapterProse}
                      className="px-4 py-2 text-sm bg-green-600 text-white rounded-lg hover:bg-green-700 disabled:opacity-50 disabled:cursor-not-allowed"
                    >
                      {isGenerating ? '生成中...' : '生成本章'}
                    </button>
                    {generationError && (
                      <div className="mt-2 rounded border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
                        {generationError}
                      </div>
                    )}
                  </div>
                )}
                <textarea
                  value={editorContent}
                  onChange={(e) => handleEditorChange(e.target.value)}
                  placeholder={
                    selectedChapterId
                      ? '章节内容将在此显示。点击 "生成本章" 按钮开始生成...'
                      : '请从左侧选择一个章节进行编辑...'
                  }
                  className="w-full min-h-[500px] p-4 text-base leading-relaxed border-none outline-none resize-none"
                  style={{ fontFamily: "'Noto Serif SC', serif" }}
                  readOnly={isGenerating}
                />
              </div>
            </div>
          )}
          </>
          )}
        </div>
      }
      panel={
        <div className="flex h-full flex-col bg-gray-50">
          <div className="border-b border-gray-200 bg-white px-4 py-3">
            <div className="flex items-center justify-between gap-2">
              <div>
                <h2 className="text-sm font-semibold text-gray-950">长篇工作台</h2>
                <p className="mt-0.5 text-[11px] text-gray-500">生成、审查、记忆和图谱在同一处闭环。</p>
              </div>
              <StatusPill tone={canonReady ? 'green' : 'amber'}>
                {canonReady ? 'Canon 就绪' : '链路待补'}
              </StatusPill>
            </div>
            <div className="mt-3 grid grid-cols-2 gap-2">
              <WorkbenchTabButton
                active={rightPanelTab === 'write'}
                label="写作中枢"
                icon={PenLine}
                onClick={() => setRightPanelTab('write')}
              />
              <WorkbenchTabButton
                active={rightPanelTab === 'review'}
                label="审查中心"
                icon={ShieldCheck}
                onClick={() => setRightPanelTab('review')}
              />
              <WorkbenchTabButton
                active={rightPanelTab === 'memory'}
                label="记忆中心"
                icon={Brain}
                onClick={() => setRightPanelTab('memory')}
              />
              <WorkbenchTabButton
                active={rightPanelTab === 'graph'}
                label="图谱洞察"
                icon={Network}
                onClick={() => setRightPanelTab('graph')}
              />
            </div>
          </div>

          <div className="flex-1 space-y-3 overflow-y-auto p-3">
            {rightPanelTab === 'write' && (
              <>
                <WorkbenchCard title="草稿闸门" icon={FileCheck2}>
                  <div className="space-y-2 text-xs text-gray-600">
                    <div className="flex items-center justify-between">
                      <span>全书大纲</span>
                      <StatusPill tone={bookOutlineData ? 'green' : 'amber'}>
                        {bookOutlineData ? '已确认' : '缺失'}
                      </StatusPill>
                    </div>
                    <div className="flex items-center justify-between">
                      <span>当前卷大纲</span>
                      <StatusPill tone={currentVolumeOutline ? 'green' : 'amber'}>
                        {currentVolumeOutline ? '已接入' : '缺失'}
                      </StatusPill>
                    </div>
                    <div className="flex items-center justify-between">
                      <span>本章细纲</span>
                      <StatusPill tone={currentChapterOutline ? 'green' : 'amber'}>
                        {currentChapterOutline ? '已接入' : '缺失'}
                      </StatusPill>
                    </div>
                    <p className="rounded-md bg-gray-50 px-2.5 py-2 leading-relaxed">
                      正文生成只作为草稿流入编辑区；章节链路完整后再进入正式保存和后续审查。
                    </p>
                  </div>
                </WorkbenchCard>

                <WorkbenchCard title="生成设置" icon={Sparkles}>
                  <GeneratePanel
                    projectId={currentProject?.id}
                    selectedChapterId={selectedChapterId}
                    outlineReadiness={outlineReadiness}
                    outlineReadinessLoading={outlineReadinessLoading}
                    onGenerate={handleGenerateChapter}
                    onGenerateOutline={handleGenerateOutline}
                  />
                </WorkbenchCard>

                <WorkbenchCard title="写作指南" icon={BookOpen}>
                  <WritingGuidePanel projectId={urlProjectId} />
                </WorkbenchCard>
              </>
            )}

            {rightPanelTab === 'review' && (
              <>
                <WorkbenchCard title="审查中心" icon={ShieldCheck}>
                  <div className="grid grid-cols-3 gap-2">
                    <WorkbenchMetric label="完成" value={completedChapters} />
                    <WorkbenchMetric label="草稿" value={draftChapters} />
                    <WorkbenchMetric label="复核" value={reviewChapters} />
                  </div>
                  <p className="mt-3 text-xs leading-relaxed text-gray-500">
                    当前审查优先覆盖章节质量、机械写法、去 AI 味、版本回滚和级联影响。
                  </p>
                </WorkbenchCard>

                {selectedChapterId ? (
                  <>
                    <WorkbenchCard title="质量评估" icon={ClipboardCheck}>
                      <EvaluationPanel chapterId={selectedChapterId} />
                    </WorkbenchCard>
                    <WorkbenchCard title="质量检查详情" icon={ListChecks}>
                      <CheckerDashboard chapterId={selectedChapterId} />
                    </WorkbenchCard>
                    <WorkbenchCard title="去 AI 味检查" icon={AlertTriangle}>
                      <AntiAIPanel chapterId={selectedChapterId} />
                    </WorkbenchCard>
                    <WorkbenchCard title="版本历史" icon={History}>
                      <VersionPanel chapterId={selectedChapterId} />
                    </WorkbenchCard>
                  </>
                ) : (
                  <WorkbenchCard title="未选章节" icon={AlertTriangle}>
                    <p className="text-xs leading-relaxed text-gray-500">
                      先从左侧目录选中章节，再运行章节审查、去 AI 味检查和版本对比。
                    </p>
                  </WorkbenchCard>
                )}

                {currentProject && (
                  <WorkbenchCard title="级联任务" icon={GitBranch}>
                    <CascadeTasksPanel
                      projectId={currentProject.id}
                      chapterId={selectedChapterId || undefined}
                    />
                  </WorkbenchCard>
                )}
              </>
            )}

            {rightPanelTab === 'memory' && (
              <>
                <WorkbenchCard title="记忆中心" icon={Brain}>
                  <div className="grid grid-cols-2 gap-2">
                    <WorkbenchMetric label="全书 Canon" value={bookOutlineData ? '已接入' : '缺失'} />
                    <WorkbenchMetric label="分卷记忆" value={volumeOutlineCount} />
                    <WorkbenchMetric label="章节快照" value={chaptersWithOutlines} />
                    <WorkbenchMetric label="总字数" value={totalWords.toLocaleString()} />
                  </div>
                  <p className="mt-3 text-xs leading-relaxed text-gray-500">
                    生成前优先读取全书大纲、当前卷大纲、本章细纲、上一章状态和已沉淀章节信息。
                  </p>
                </WorkbenchCard>
                <WorkbenchCard title="设定与角色" icon={Users}>
                  <div className="space-y-2">
                    <DrawerLinkButton
                      label="设定集"
                      icon={Settings}
                      hint="维护世界规则、角色资料和不可违背设定。"
                      onClick={() => setDrawerPanel('settings')}
                    />
                    <DrawerLinkButton
                      label="角色关系"
                      icon={Users}
                      hint="查看角色状态、关系变化和当前认知。"
                      onClick={() => setDrawerPanel('relationship')}
                    />
                  </div>
                </WorkbenchCard>
                <WorkbenchCard title="伏笔与剧情线" icon={Database}>
                  <div className="space-y-2">
                    <DrawerLinkButton
                      label="伏笔追踪"
                      icon={Search}
                      hint="检查新增、推进、回收和长期未处理的伏笔债务。"
                      onClick={() => setDrawerPanel('foreshadow')}
                    />
                    <DrawerLinkButton
                      label="三线平衡"
                      icon={GitBranch}
                      hint="观察主线、任务线和情绪线是否断档。"
                      onClick={() => setDrawerPanel('strand')}
                    />
                  </div>
                </WorkbenchCard>
              </>
            )}

            {rightPanelTab === 'graph' && (
              <>
                <WorkbenchCard title="图谱洞察" icon={Network}>
                  <div className="space-y-2 text-xs text-gray-600">
                    <div className="grid grid-cols-2 gap-2">
                      <WorkbenchMetric label="角色" value="关系面板" hint="当前入口" />
                      <WorkbenchMetric label="线索" value="伏笔追踪" hint="债务面板" />
                    </div>
                    <p className="rounded-md bg-gray-50 px-2.5 py-2 leading-relaxed">
                      现阶段复用角色关系、伏笔追踪和三线平衡作为图谱入口；后续可接入独立关系图页面。
                    </p>
                  </div>
                </WorkbenchCard>
                <WorkbenchCard title="探索入口" icon={Search}>
                  <div className="space-y-2">
                    <DrawerLinkButton
                      label="角色关系网络"
                      icon={Users}
                      hint="追踪角色、任务、认知和关系变化。"
                      onClick={() => setDrawerPanel('relationship')}
                    />
                    <DrawerLinkButton
                      label="伏笔关联"
                      icon={Database}
                      hint="从伏笔债务回看涉及章节与角色。"
                      onClick={() => setDrawerPanel('foreshadow')}
                    />
                    <DrawerLinkButton
                      label="剧情线平衡"
                      icon={GitBranch}
                      hint="查看长期剧情推进是否偏科。"
                      onClick={() => setDrawerPanel('strand')}
                    />
                  </div>
                </WorkbenchCard>
                <WorkbenchCard title="正式记忆状态" icon={CheckCircle2}>
                  <p className="text-xs leading-relaxed text-gray-500">
                    当前项目已有 {completedChapters} 章完成、{chaptersWithOutlines} 章细纲。
                    图谱和检索入口只展示已确认或已沉淀的信息，避免草稿污染后续生成。
                  </p>
                </WorkbenchCard>
              </>
            )}
          </div>

          <div className="border-t border-gray-200 bg-white p-3">
            <TokenDashboard />
          </div>
        </div>
      }
    />
    </>
  )
}

// ================================================================
// Utility: parse volume outline JSON (handles markdown fences, partial output)
// ================================================================

function parseVolumeOutline(text: string): Record<string, unknown> {
  let cleaned = text.trim()
  if (cleaned.startsWith('```')) {
    const lines = cleaned.split('\n')
    lines.shift()
    if (lines.length > 0 && lines[lines.length - 1].trim() === '```') {
      lines.pop()
    }
    cleaned = lines.join('\n').trim()
  }
  try {
    const obj = JSON.parse(cleaned)
    if (obj && typeof obj === 'object') return obj as Record<string, unknown>
  } catch {
    // fall through
  }
  return { raw_text: text }
}

// ================================================================
// Utility: detect volume count from a free-text book outline
// ================================================================

const CN_NUM_MAP: Record<string, number> = {
  一: 1, 二: 2, 三: 3, 四: 4, 五: 5,
  六: 6, 七: 7, 八: 8, 九: 9, 十: 10,
  十一: 11, 十二: 12, 十三: 13, 十四: 14, 十五: 15,
  十六: 16, 十七: 17, 十八: 18, 十九: 19, 二十: 20,
}

function detectVolumeCount(text: string): number {
  if (!text) return 0
  const indices = new Set<number>()

  // Arabic numerals: 第1卷, 第 2 卷, 卷3, Volume 4
  const arabicPatterns = [
    /第\s*(\d{1,2})\s*卷/g,
    /卷\s*(\d{1,2})/g,
    /Volume\s+(\d{1,2})/gi,
    /Vol\.?\s*(\d{1,2})/gi,
  ]
  for (const re of arabicPatterns) {
    let m: RegExpExecArray | null
    while ((m = re.exec(text)) !== null) {
      const n = parseInt(m[1], 10)
      if (n >= 1 && n <= 50) indices.add(n)
    }
  }

  // Chinese numerals: 第一卷 ... 第二十卷
  const cnRe = /第([一二三四五六七八九十]{1,3})卷/g
  let m: RegExpExecArray | null
  while ((m = cnRe.exec(text)) !== null) {
    const n = CN_NUM_MAP[m[1]]
    if (n) indices.add(n)
  }

  // Non-numbered volumes: 前传/外传/番外/序卷/终章/终卷. Each unique keyword = +1
  const keywords = ['前传', '外传', '番外', '序卷', '终章', '终卷']
  let extras = 0
  for (const kw of keywords) {
    if (text.includes(kw)) extras += 1
  }

  if (indices.size === 0 && extras === 0) return 0
  return indices.size + extras
}

// ================================================================
// End of module
// ================================================================

function ChapterTargetWordsEditor({
  projectId,
  chapter,
  projectDefault,
  onSaved,
}: {
  projectId: string
  chapter: Chapter
  projectDefault: number | null | undefined
  onSaved: () => void
}) {
  const initial = chapter.target_word_count ?? null
  const [text, setText] = useState(initial != null ? String(initial) : '')
  const [editing, setEditing] = useState(false)
  const effective = initial != null ? initial : (projectDefault ?? null)
  const save = async () => {
    const trimmed = text.trim()
    const n: number | null = trimmed ? parseInt(trimmed, 10) : null
    if (trimmed && (Number.isNaN(n!) || (n as number) <= 0)) return
    await apiFetch(`/api/projects/${projectId}/chapters/${chapter.id}`, {
      method: 'PUT',
      body: JSON.stringify({ target_word_count: n }),
    })
    setEditing(false)
    onSaved()
  }
  return (
    <span className="text-xs text-gray-500">
      {editing ? (
        <>
          目标：
          <input
            type="number"
            min={0}
            value={text}
            onChange={(e) => setText(e.target.value)}
            onBlur={save}
            onKeyDown={(e) => { if (e.key === 'Enter') save() }}
            autoFocus
            className="w-20 px-1 py-0.5 text-xs border border-blue-300 rounded ml-1"
            placeholder={projectDefault ? String(projectDefault) : '默认'}
          />
        </>
      ) : (
        <button onClick={() => setEditing(true)} className="hover:text-gray-800">
          目标 {effective ? `${effective.toLocaleString()} 字` : '未设'}
          {initial == null && projectDefault ? '（默认）' : ''}
        </button>
      )}
    </span>
  )
}

function VolumeOutlineEditor({
  volume,
  data,
  projectId,
  onSaved,
  onVolumeChanged,
}: {
  volume: Volume
  data: Record<string, unknown>
  projectId: string
  onSaved: (data: Record<string, unknown>) => void
  onVolumeChanged?: () => void
}) {
  const [editing, setEditing] = useState(false)
  const [text, setText] = useState(() => {
    if (typeof data.raw_text === 'string') return data.raw_text
    return JSON.stringify(data, null, 2)
  })
  const [busy, setBusy] = useState(false)
  // PR-OL7: inline edit volume title (separate from outline content edit).
  const [editingTitle, setEditingTitle] = useState(false)
  const [titleDraft, setTitleDraft] = useState(volume.title || "")
  const [savingTitle, setSavingTitle] = useState(false)

  const saveTitle = async () => {
    if (savingTitle || !titleDraft.trim()) return
    setSavingTitle(true)
    try {
      const updated = await apiFetch<{ id: string; title: string }>(
        `/api/projects/${projectId}/volumes/${volume.id}`,
        { method: "PUT", body: JSON.stringify({ title: titleDraft.trim() }) },
      )
      // Update local volume reference (safe — volume is a prop snapshot, parent re-fetch will eventually update).
      ;(volume as { title: string }).title = updated.title
      setEditingTitle(false)
      onVolumeChanged?.()
    } catch (err) {
      console.error("保存卷名失败:", err)
    } finally {
      setSavingTitle(false)
    }
  }

  // PR-OL8: move (swap volume_idx) and delete.
  const moveVolume = async (direction: "up" | "down") => {
    if (!confirm(`确定${direction === "up" ? "上" : "下"}移 《${volume.title}》吗？`)) return
    try {
      const allVolumes = await apiFetch<Volume[]>(`/api/projects/${projectId}/volumes`)
      const sorted = [...allVolumes].sort(
        (a, b) => (a.volume_idx ?? a.volumeIdx) - (b.volume_idx ?? b.volumeIdx),
      )
      const myIdx = volume.volume_idx ?? volume.volumeIdx
      const targetIdx = direction === "up" ? myIdx - 1 : myIdx + 1
      const target = sorted.find((v) => (v.volume_idx ?? v.volumeIdx) === targetIdx)
      if (!target) {
        alert(direction === "up" ? "已是第一卷" : "已是最后一卷")
        return
      }
      // Swap: 先把当前 改为一个临时卷号，再交换（避免其他 uq冲突，虽然 model 没 uq 但防万一）
      const TMP = 9999
      await apiFetch(`/api/projects/${projectId}/volumes/${volume.id}`, {
        method: "PUT", body: JSON.stringify({ volume_idx: TMP }),
      })
      await apiFetch(`/api/projects/${projectId}/volumes/${target.id}`, {
        method: "PUT", body: JSON.stringify({ volume_idx: myIdx }),
      })
      await apiFetch(`/api/projects/${projectId}/volumes/${volume.id}`, {
        method: "PUT", body: JSON.stringify({ volume_idx: targetIdx }),
      })
      onVolumeChanged?.()
    } catch (err) {
      console.error("移动卷失败:", err)
      alert("移动失败，请重试。")
    }
  }

  const deleteVolume = async () => {
    if (!confirm(`确定删除 《${volume.title}》 及其所有章节吗？此操作不可逆。`)) return
    try {
      await apiFetch(`/api/projects/${projectId}/volumes/${volume.id}`, { method: "DELETE" })
      onVolumeChanged?.()
    } catch (err) {
      console.error("删除卷失败:", err)
      alert("删除失败，请重试。")
    }
  }

  const save = async () => {
    if (busy) return
    setBusy(true)
    try {
      const outlines = await apiFetch<OutlineRes[]>(
        `/api/projects/${projectId}/outlines?level=volume`
      )
      const target = outlines.find((o) => {
        const cj = (o.content_json as Record<string, unknown>) || {}
        return cj.volume_idx === (volume.volume_idx ?? volume.volumeIdx)
      })
      if (!target) return
      let contentJson: Record<string, unknown>
      try {
        contentJson = JSON.parse(text)
      } catch {
        contentJson = { ...data, raw_text: text }
      }
      await apiFetch(`/api/projects/${projectId}/outlines/${target.id}`, {
        method: 'PUT',
        body: JSON.stringify({ content_json: contentJson }),
      })
      onSaved(contentJson)
      setEditing(false)
    } finally {
      setBusy(false)
    }
  }

  return (
    <details className="border rounded-xl overflow-hidden">
      <summary className="cursor-pointer px-4 py-2 bg-gray-50 text-sm font-medium text-gray-700 hover:bg-gray-100 flex items-center justify-between">
        <div className="flex-1 flex items-center gap-2" onClick={(e) => editingTitle && e.preventDefault()}>
          {editingTitle ? (
            <>
              <input
                type="text"
                value={titleDraft}
                onChange={(e) => setTitleDraft(e.target.value)}
                onClick={(e) => e.preventDefault()}
                className="flex-1 min-w-[120px] px-2 py-0.5 border border-blue-300 rounded text-sm"
              />
              <button
                onClick={(e) => { e.preventDefault(); saveTitle() }}
                disabled={savingTitle}
                className="text-xs px-2 py-0.5 rounded bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50"
              >{savingTitle ? "保存中..." : "保存名"}</button>
              <button
                onClick={(e) => { e.preventDefault(); setTitleDraft(volume.title || ""); setEditingTitle(false) }}
                className="text-xs px-2 py-0.5 rounded bg-gray-100 text-gray-700 hover:bg-gray-200"
              >取消</button>
            </>
          ) : (
            <>
              <span>{volume.title}</span>
              <button
                onClick={(e) => { e.preventDefault(); setTitleDraft(volume.title || ""); setEditingTitle(true) }}
                className="text-xs text-gray-500 hover:text-blue-600 hover:underline"
                title="重命名本卷"
              >✎</button>
            </>
          )}
        </div>
        <div className="flex items-center gap-1">
          <button
            onClick={(e) => { e.preventDefault(); moveVolume("up") }}
            className="text-xs px-1.5 py-0.5 rounded bg-gray-100 hover:bg-gray-200"
            title="上移"
          >↑</button>
          <button
            onClick={(e) => { e.preventDefault(); moveVolume("down") }}
            className="text-xs px-1.5 py-0.5 rounded bg-gray-100 hover:bg-gray-200"
            title="下移"
          >↓</button>
          <button
            onClick={(e) => { e.preventDefault(); deleteVolume() }}
            className="text-xs px-1.5 py-0.5 rounded text-red-600 hover:bg-red-50"
            title="删除本卷"
          >✖</button>
          <button
            onClick={(e) => { e.preventDefault(); setEditing((v) => !v) }}
            className="text-xs text-blue-600 hover:underline ml-2"
          >
            {editing ? '取消编辑' : '编辑大纲'}
          </button>
        </div>
      </summary>
      <div className="px-4 py-3 bg-white border-t text-sm">
        {editing ? (
          <div>
            <textarea
              value={text}
              onChange={(e) => setText(e.target.value)}
              className="w-full h-64 px-3 py-2 text-xs border border-gray-300 rounded-lg font-mono resize-none"
            />
            <div className="mt-2">
              <button
                onClick={save}
                disabled={busy}
                className="px-4 py-1.5 text-sm bg-green-600 text-white rounded-lg disabled:opacity-50"
              >
                {busy ? '保存中...' : '保存'}
              </button>
            </div>
          </div>
        ) : (
          <VolumeOutlineBlock data={data} />
        )}
      </div>
    </details>
  )
}
