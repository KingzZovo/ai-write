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
import { SettingsExtractionStatus, useSettingsExtraction } from '@/components/workspace/SettingsExtractionStatus'
import { OutlineTree } from '@/components/outline/OutlineTree'
import { VolumeOutlineBlock } from '@/components/outline/VolumeOutlineBlock'
import { OutlineEditor, type OutlineEditTarget } from '@/components/outline/OutlineEditor'
import { GeneratePanel, getSelectedStyleId } from '@/components/panels/GeneratePanel'
import { RewriteMenu } from '@/components/editor/RewriteMenu'
import { applyRewriteCandidate } from '@/components/editor/rewriteApplication'
import { useChapterDraft } from '@/components/editor/useChapterDraft'
import { ChapterSaveStatus } from '@/components/editor/ChapterSaveStatus'
import { ChapterDraftSession } from '@/components/editor/chapterDraftSession'
import { ChapterGeneration, createChapterGeneration } from '@/components/editor/chapterGeneration'
// PR-WS-QUEUE: background generation queue (任务队列)
import { TaskQueuePanel } from '@/components/workspace/TaskQueuePanel'
import { useAsyncTaskQueue } from '@/components/workspace/useAsyncTaskQueue'
import { GenerationSubmissionNotice } from '@/components/workspace/GenerationSubmissionNotice'
import { isTerminal } from '@/components/workspace/asyncTaskQueue'
import { selectVolumeOutlines } from '@/lib/volumeOutlineSelection'

// Lazy load heavy panels — only loaded when their workbench tab/drawer is opened
const AuthorVolumeOutlineEditor = dynamic(() => import('@/components/outline/VolumeOutlineEditor').then(m => m.VolumeOutlineEditor), { ssr: false })
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
const CognitionPanel = dynamic(() => import('@/components/panels/CognitionPanel').then(m => ({ default: m.CognitionPanel })), { ssr: false })
const ArcProgressPanel = dynamic(() => import('@/components/arc/ArcProgressPanel').then(m => m.ArcProgressPanel), { ssr: false })
const CascadeTasksPanel = dynamic(() => import('@/components/panels/CascadeTasksPanel').then(m => ({ default: m.CascadeTasksPanel })), { ssr: false })
import {
  useProjectStore,
  normalizeVolume,
  normalizeChapter,
} from '@/stores/projectStore'
import type { Project, Volume, Chapter } from '@/stores/projectStore'
import { useGenerationStore } from '@/stores/generationStore'
import { apiFetch, apiSSE } from '@/lib/api'
import { runVolumeWizard, volumeOutline } from '@/lib/volumeWizard'
import { generateSavedBook, outlineText, selectBookOutline } from '@/lib/bookOutline'
import { getChapterGenOptions } from '@/lib/chapterGenOptions'

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
  created_at?: string | null
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

type ChapterStreamOperation = {
  session: ChapterDraftSession
  generation: ChapterGeneration | null
  controller: AbortController | null
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
    <span className={`inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-[11px] font-medium ${tones[tone]}`}>
      <span aria-hidden className="h-1.5 w-1.5 shrink-0 rounded-full bg-current opacity-60" />
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
      <div className="text-[10px] font-medium uppercase tracking-wide text-gray-400">{label}</div>
      <div className="mt-0.5 text-sm font-semibold tabular-nums text-gray-900">{value}</div>
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
      className={`flex items-center justify-center gap-1.5 rounded-md border px-2.5 py-2 text-xs font-medium transition-all duration-150 motion-safe:active:scale-[0.97] ${
        active
          ? 'border-gray-900 bg-gray-900 text-white shadow-sm'
          : 'border-gray-200 bg-white text-gray-600 hover:border-gray-300 hover:bg-gray-50 hover:text-gray-900'
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
    <section className="overflow-hidden rounded-lg border border-gray-200 bg-white shadow-sm">
      <div className="flex items-center justify-between gap-2 border-b border-gray-100 bg-gray-50/60 px-3 py-2">
        <div className="flex items-center gap-2 min-w-0">
          <Icon className="h-4 w-4 shrink-0 text-gray-400" />
          <h3 className="truncate text-sm font-semibold tracking-tight text-gray-900">{title}</h3>
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
      className="w-full rounded-md border border-gray-200 bg-white px-3 py-2 text-left transition-all duration-150 hover:border-gray-300 hover:bg-gray-50 hover:shadow-sm motion-safe:active:scale-[0.99]"
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
    updateChapterSummary,
  } = useProjectStore()

  const { isGenerating, setIsGenerating } =
    useGenerationStore()

  // PR-WS-QUEUE: background async generation tasks for this project
  // (resume-on-load + 5s polling while any task is non-terminal).
  const {
    tasks: queueTasks,
    cancelUnavailable: queueCancelUnavailable,
    submitChapterTask,
    cancelTask,
    submissionPending,
    error: queueError,
    refresh: refreshQueue,
  } = useAsyncTaskQueue(currentProject?.id)

  // ---- Local UI state ----
  const { session: draftSession, state: draftState } = useChapterDraft(currentProject?.id, selectedChapterId)
  const editorContent = draftState?.text ?? ''
  const [creativeInput, setCreativeInput] = useState('')
  const [outlinePreview, setOutlinePreview] = useState('')
  const [generationError, setGenerationError] = useState<string | null>(null)
  // PR-GEN-UX: progress strip (phase + elapsed) and warning banners for the
  // chapter-generation SSE stream. Phase is non-null only while a chapter
  // generation is streaming (outline generation does not touch it).
  const [generationPhase, setGenerationPhase] = useState<string | null>(null)
  const [generationStartedAt, setGenerationStartedAt] = useState<number | null>(null)
  const [generationWarnings, setGenerationWarnings] = useState<string[]>([])
  // PR-GEN-UX Task 5: selection-based rewrite over the chapter textarea.
  const rewriteSequence = useRef(0)
  const [rewriteSel, setRewriteSel] = useState<{
    sessionId: number
    chapterId: string
    sourceText: string
    start: number
    end: number
    text: string
    position: { top: number; left: number }
  } | null>(null)
  // PR-WS-OVERWRITE-GUARD: when the chapter already has substantial prose,
  // 生成本章 / 后台生成本章 first arm this inline confirm instead of firing.
  const [pendingRegenerate, setPendingRegenerate] = useState<'inline' | 'background' | null>(null)
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
  const { state: settingsExtraction, start: startSettingsExtraction } = useSettingsExtraction(
    currentProject?.id, bookOutlineId, bookOutlineData,
  )
  const [volumeOutlineIds, setVolumeOutlineIds] = useState<Record<number, string>>({})
  const [outlineEditorTarget, setOutlineEditorTarget] = useState<OutlineEditTarget | null>(null)
  const [volumeEditorEntry, setVolumeEditorEntry] = useState<{ projectId: string; taskId?: string; volumeId?: string } | null>(null)
  const projectDataRequest = useRef(0)
  // Only a verified saved book can be edited or confirmed.
  const [bookError, setBookError] = useState<string | null>(null)
  const [bookBusy, setBookBusy] = useState(false)
  const bookOperationRef = useRef(0)
  const projectIdRef = useRef(currentProject?.id)
  projectIdRef.current = currentProject?.id
  // Outline inline editing
  const [outlineEditing, setOutlineEditing] = useState(false)
  const [outlineReadiness, setOutlineReadiness] = useState<OutlineReadinessRes | null>(null)
  const [outlineReadinessLoading, setOutlineReadinessLoading] = useState(false)
  const readinessRequest = useRef(0)
  const readinessController = useRef<AbortController | null>(null)

  // Drawer panel
  const [drawerPanel, setDrawerPanel] = useState<string | null>(null)
  const [rightPanelTab, setRightPanelTab] = useState<'write' | 'review' | 'memory' | 'graph'>('write')

  const selectedChapterIdRef = useRef(selectedChapterId)
  selectedChapterIdRef.current = selectedChapterId
  const chapterGenerationRef = useRef<ChapterStreamOperation | null>(null)
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
    const controller = new AbortController()
    let active = true
    apiFetch<Project>(`/api/projects/${urlProjectId}`, { signal: controller.signal })
      .then((project) => { if (active && project.id === urlProjectId) setCurrentProject(project) })
      .catch(() => { if (active) router.replace('/') })
    return () => { active = false; controller.abort() }
  }, [urlProjectId, currentProject?.id, router, setCurrentProject])

  // ----------------------------------------------------------------
  // Load volumes + chapters when a project is selected
  // ----------------------------------------------------------------
  const loadProjectData = useCallback(
    async (projectId: string, chooseView = true) => {
      const request = ++projectDataRequest.current
      const active = () => request === projectDataRequest.current && projectIdRef.current === projectId && !unmountedRef.current
      try {
        const [vols, chs, outlines] = await Promise.all([
          apiFetch<VolumeRes[]>(`/api/projects/${projectId}/volumes`),
          apiFetch<ChapterRes[]>(`/api/projects/${projectId}/chapters?lightweight=true`),
          apiFetch<OutlineRes[]>(`/api/projects/${projectId}/outlines`),
        ])
        if (!active()) return
        const normalized = vols.map((v) => normalizeVolume(v as unknown as Record<string, unknown>))
        setVolumes(normalized)
        const normChs = chs.map((c) => normalizeChapter(c as unknown as Record<string, unknown>))
        setChapters(normChs)
        const bookOutline = selectBookOutline(outlines)
        if (bookOutline) {
          const cj = bookOutline.content_json as Record<string, unknown> | null
          const raw = outlineText(cj || {})
          setOutlinePreview(raw)
          setConfirmedOutlineId(bookOutline.is_confirmed ? bookOutline.id : null)
          setBookOutlineData(cj)  // PR-OL14
          setBookOutlineId(bookOutline.id)  // PR-OUTLINE-CENTER-EDIT
          const plan = Array.isArray(cj?.volume_plan) ? cj.volume_plan as NonNullable<typeof volumePlan> : []
          setVolumePlan(plan)
          setVolumeCountInput(plan.length ? String(plan.length) : '')
        } else {
          setOutlinePreview('')
          setConfirmedOutlineId(null)
          setBookOutlineData(null)  // PR-OL14
          setBookOutlineId(null)  // PR-OUTLINE-CENTER-EDIT
        }

        // Resolve the saved volume identity before indexing its current label.
        const volOutlineMap: Record<number, Record<string, unknown>> = {}
        const volOutlineIdMap: Record<number, string> = {}
        const selected = selectVolumeOutlines(outlines, vols)
        for (const volume of vols) {
          const outline = selected[volume.id]
          if (!outline) continue
          volOutlineMap[volume.volume_idx] = outline.content_json
          volOutlineIdMap[volume.volume_idx] = outline.id
        }
        setVolumeOutlines(volOutlineMap)
        setVolumeOutlineIds(volOutlineIdMap)  // PR-OUTLINE-CENTER-EDIT

        // Route to appropriate view
        if (!chooseView) return
        if (normalized.length > 0) {
          setActiveView('editor')
        } else if (bookOutline) {
          setActiveView('wizard')
          setWizardStep(bookOutline.is_confirmed ? 2 : 1)
        } else {
          setActiveView('wizard')
          setWizardStep(1)
        }
      } catch (err) {
        if (active()) setBookError('作品目录或卷纲读取失败，请重新核对保存稿。')
      }
    },
    [setVolumes, setChapters]
  )

  useEffect(() => {
    ++bookOperationRef.current
    projectDataRequest.current += 1
    setVolumeEditorEntry(null)
    setOutlineEditorTarget(null)
    sseControllerRef.current?.abort()
    setIsGenerating(false)
    setBookBusy(false)
    setBookError(null)
    setOutlineEditing(false)
    setBookOutlineId(null)
    setBookOutlineData(null)
    setConfirmedOutlineId(null)
    setOutlinePreview('')
    if (currentProject) {
      loadProjectData(currentProject.id)
    }
  }, [currentProject?.id]) // eslint-disable-line react-hooks/exhaustive-deps

  const refreshOutlineReadiness = useCallback(async () => {
    const request = ++readinessRequest.current
    readinessController.current?.abort()
    const projectId = currentProject?.id, chapterId = selectedChapterId
    if (!projectId || !chapterId) {
      setOutlineReadiness(null)
      setOutlineReadinessLoading(false)
      return
    }
    const controller = new AbortController()
    readinessController.current = controller
    const active = () => request === readinessRequest.current && !unmountedRef.current &&
      projectIdRef.current === projectId && selectedChapterIdRef.current === chapterId
    setOutlineReadinessLoading(true)
    try {
      const report = await apiFetch<OutlineReadinessRes>(
        `/api/projects/${projectId}/outline-readiness?chapter_id=${chapterId}`, { signal: controller.signal },
      )
      if (active()) setOutlineReadiness(report.project_id === projectId && report.chapter_id === chapterId ? report : null)
    } catch (err) {
      if (active()) {
        console.warn('Failed to load outline readiness:', err)
        setOutlineReadiness(null)
      }
    } finally {
      if (active()) setOutlineReadinessLoading(false)
    }
  }, [currentProject?.id, selectedChapterId])

  useEffect(() => {
    void refreshOutlineReadiness()
    return () => { readinessRequest.current += 1; readinessController.current?.abort() }
  }, [refreshOutlineReadiness, selectedChapterId, currentProject?.id, chapters.length])

  // Loading and explicit user actions select the view. Creating the first
  // volume must not pull the author out of an in-progress wizard.

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
      if (!currentProject || level !== 'book' || bookBusy) return
      const projectId = currentProject.id
      const operation = ++bookOperationRef.current
      const active = () => !unmountedRef.current && projectIdRef.current === projectId && bookOperationRef.current === operation
      sseControllerRef.current?.abort()
      setIsGenerating(true)
      setOutlinePreview('')
      setBookError(null)
      setOutlineEditing(false)
      setConfirmedOutlineId(null)
      setBookOutlineId(null)
      setBookOutlineData(null)
      setActiveView('wizard')
      resetStageStates()
      void generateSavedBook({ project_id: projectId, user_input: creativeInput },
        text => { if (active()) setOutlinePreview(prev => prev + text) },
        evt => {
          if (!active()) return
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
        }, controller => { sseControllerRef.current = controller })
        .then(outline => {
          if (!active()) return
          setBookOutlineId(outline.id)
          setBookOutlineData(outline.content_json)
          setOutlinePreview(outlineText(outline.content_json))
          setConfirmedOutlineId(outline.is_confirmed ? outline.id : null)
          if (Array.isArray(outline.content_json.volume_plan)) {
            setVolumePlan(outline.content_json.volume_plan as typeof volumePlan)
            setVolumeCountInput(String(outline.content_json.volume_plan.length))
          }
        })
        .catch(() => { if (active()) setBookError('大纲生成或保存未完成，当前预览不能确认。请刷新检查或重试。') })
        .finally(() => { if (active()) setIsGenerating(false) })
    },
    [
      isGenerating,
      bookBusy,
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
    if (!currentProject || !bookOutlineId || !bookOutlineData || isGenerating || bookBusy || outlineEditing
      || outlinePreview !== outlineText(bookOutlineData)) return
    const outlineId = bookOutlineId
    const projectId = currentProject.id
    setBookBusy(true)
    setBookError(null)
    try {
      await apiFetch<OutlineRes>(
        `/api/projects/${projectId}/outlines/${outlineId}/confirm`,
        { method: 'POST', body: JSON.stringify({ expected_content_json: bookOutlineData }) }
      )
      if (projectIdRef.current !== projectId || unmountedRef.current) return
      setConfirmedOutlineId(outlineId)
      setWizardStep(2)

      void startSettingsExtraction()

    } catch {
      if (projectIdRef.current === projectId) setBookError('确认失败，大纲可能已被修改。请刷新核对后再确认。')
    } finally {
      if (projectIdRef.current === projectId) setBookBusy(false)
    }
  }, [currentProject, outlinePreview, bookOutlineId, bookOutlineData, isGenerating, bookBusy, outlineEditing, startSettingsExtraction])

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
      const parsed = Number(trimmed)
      if (!Number.isInteger(parsed) || parsed < 1) {
        setWizardProgress('卷数必须是大于 0 的整数。')
        return
      }
      count = parsed
    } else {
      const detected = volumePlan?.length || detectVolumeCount(outlinePreview)
      if (detected < 1) {
        setWizardProgress('未能识别分卷计划，请填写总卷数。')
        return
      }
      count = detected
      setWizardProgress(`已从大纲识别 ${count} 卷，开始生成...`)
    }

    setIsGenerating(true)
    if (trimmed) {
      setWizardProgress(`准备生成 ${count} 卷大纲...`)
    }
    setVolumeOutlines({})

    try {
      const state = await runVolumeWizard({
        projectId: currentProject.id, bookId: confirmedOutlineId, count, userInput: creativeInput,
        stopped: () => unmountedRef.current,
        onController: controller => {
          sseControllerRef.current?.abort()
          sseControllerRef.current = controller
        },
        onProgress: setWizardProgress,
        onState: saved => {
          setVolumes(saved.volumes.map(normalizeVolume))
          setChapters(saved.chapters.map(normalizeChapter))
          const outlines: Record<number, Record<string, unknown>> = {}
          const ids: Record<number, string> = {}
          for (const volume of saved.volumes) {
            const outline = volumeOutline(saved, volume)
            if (outline) { outlines[volume.volume_idx] = outline.content_json; ids[volume.volume_idx] = outline.id }
          }
          setVolumeOutlines(outlines)
          setVolumeOutlineIds(ids)
        },
      })
      if (!state || unmountedRef.current) return
      setWizardProgress(`已核实 ${count} 卷，当前共 ${state.chapters.length} 章。`)
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
    volumePlan,
    setIsGenerating,
    setVolumes,
    setChapters,
  ])

  // Chapter draft ownership is independent of the global outline/store data.
  // Switching chapters retains unsaved drafts and invalidates the old stream.
  useEffect(() => {
    const previous = chapterGenerationRef.current
    if (previous && previous.session !== draftSession) {
      previous.generation?.detach()
      previous.controller?.abort()
    }
    setRewriteSel(null)
    setPendingRegenerate(null)
    setGenerationError(null)
    setGenerationWarnings([])
    if (selectedChapterId) setActiveView('editor')
  }, [draftSession, selectedChapterId])

  useEffect(() => {
    if (!draftState || draftState.projectId !== currentProject?.id || draftState.chapterId !== selectedChapterId) return
    if (draftState.source || draftState.text) updateChapterContent(draftState.chapterId, draftState.text)
    if (draftState.streaming) updateChapterStatus(draftState.chapterId, 'generating')
    else if (draftState.source) updateChapterStatus(draftState.chapterId, draftState.source.status as Chapter['status'])
  }, [draftState, currentProject?.id, selectedChapterId, updateChapterContent, updateChapterStatus])

  const handleEditorChange = useCallback((value: string) => {
    draftSession?.edit(value)
  }, [draftSession])

  const handleGenerateChapter = useCallback(() => {
    if (isGenerating || !selectedChapterId || !currentProject || !draftSession || chapterGenerationRef.current) return
    if (!draftSession.canGenerate()) {
      setGenerationError('请先保存或核对编辑区稿件，再开始生成。')
      return
    }
    if (!outlineReadiness?.ready || outlineReadiness.project_id !== currentProject.id || outlineReadiness.chapter_id !== selectedChapterId) {
      setGenerationError('请先核对当前章节的大纲链路。')
      return
    }
    const projectId = currentProject.id, chapterId = selectedChapterId
    const operation: ChapterStreamOperation = { session: draftSession, generation: null, controller: null }
    const active = () => !unmountedRef.current && projectIdRef.current === projectId &&
      selectedChapterIdRef.current === chapterId && chapterGenerationRef.current === operation
    const generation = createChapterGeneration(draftSession, {
      active,
      phase: setGenerationPhase,
      warning: (warning) => setGenerationWarnings((previous) => [...previous, warning]),
      error: setGenerationError,
      blocked: () => { void refreshOutlineReadiness() },
      settled: () => {
        if (chapterGenerationRef.current !== operation) return
        chapterGenerationRef.current = null
        setIsGenerating(false)
        setGenerationPhase(null)
        setGenerationStartedAt(null)
      },
    })
    if (!generation) return
    operation.generation = generation
    chapterGenerationRef.current = operation
    setGenerationError(null)
    setGenerationWarnings([])
    setGenerationPhase('准备生成…')
    setGenerationStartedAt(Date.now())
    setRewriteSel(null)
    setIsGenerating(true)
    setActiveView('editor')
    const genOpts = getChapterGenOptions(chapterId)
    sseControllerRef.current?.abort()
    operation.controller = apiSSE('/api/generate/chapter', {
      project_id: projectId, chapter_id: chapterId, style_id: getSelectedStyleId(),
      ...(genOpts.userInstruction ? { user_instruction: genOpts.userInstruction } : {}),
      ...(genOpts.targetWords ? { target_words: genOpts.targetWords } : {}),
    }, generation.chunk, generation.done, generation.event, generation.error)
    sseControllerRef.current = operation.controller
  }, [isGenerating, selectedChapterId, currentProject, draftSession, outlineReadiness, setIsGenerating, refreshOutlineReadiness])

  const handleCancelGeneration = useCallback(() => {
    const current = chapterGenerationRef.current
    current?.generation?.cancel()
    current?.generation?.done()
    current?.controller?.abort()
  }, [])

  // PR-WS-QUEUE: submit the current chapter to the background queue. Mirrors
  // the SSE payload of handleGenerateChapter via POST /api/generate/async
  // (per-chapter 指令/目标字数 travel as user_input / params.target_words).
  const chapterHasActiveTask = Boolean(
    selectedChapterId &&
      queueTasks.some(
        (t) => !isTerminal(t.status) && t.chapterId === selectedChapterId,
      ),
  )
  const handleBackgroundGenerate = useCallback(async () => {
    if (!selectedChapterId || !currentProject || isGenerating || submissionPending || !draftSession?.canGenerate() ||
      !outlineReadiness?.ready || outlineReadiness.chapter_id !== selectedChapterId || outlineReadiness.project_id !== currentProject.id) return
    const projectId = currentProject.id, chapterId = selectedChapterId
    const genOpts = getChapterGenOptions(selectedChapterId)
    try {
      await submitChapterTask({
        chapterId: selectedChapterId,
        expectedContentText: draftSession.getSnapshot().source!.content_text,
        styleId: getSelectedStyleId(),
        userInstruction: genOpts.userInstruction || undefined,
        targetWords: genOpts.targetWords ?? undefined,
      })
      if (projectIdRef.current !== projectId || selectedChapterIdRef.current !== chapterId || unmountedRef.current) return
      setGenerationWarnings((prev) => [
        ...prev,
        '已提交后台生成任务；完成后点「刷新章节」核对保存稿，编辑区修改会保留。',
      ])
    } catch (err) {
      if (projectIdRef.current !== projectId || selectedChapterIdRef.current !== chapterId || unmountedRef.current) return
      setGenerationError(
        `后台生成任务尚未确认：${err instanceof Error ? err.message : String(err)}。原请求已保留，请在请求提示中核对。`,
      )
    }
  }, [selectedChapterId, currentProject, isGenerating, submissionPending, draftSession, outlineReadiness, submitChapterTask])

  const handleCancelQueueTask = useCallback(
    async (taskId: string) => {
      const errMsg = await cancelTask(taskId)
      if (errMsg) alert(`取消任务失败：${errMsg}`)
    },
    [cancelTask],
  )

  // A background result goes through the same source comparison. Selecting
  // another chapter never discards the current chapter's pending draft.
  const handleRefreshChapterFromTask = useCallback(async (chapterId: string) => {
    if (!currentProject || !chapters.some((chapter) => chapter.id === chapterId)) return
    setOutlineEditorTarget(null)
    setVolumeEditorEntry(null)
    setActiveView('editor')
    if (chapterId === selectedChapterId) await draftSession?.reload()
    else selectChapter(chapterId)
  }, [currentProject, chapters, selectedChapterId, draftSession, selectChapter])

  // PR-WS-CHAPTER-MGMT: create a new volume at the end (client computes max+1).
  const handleCreateVolume = useCallback(async () => {
    if (!currentProject) return
    const nextIdx =
      volumes.reduce((m, v) => Math.max(m, v.volume_idx ?? v.volumeIdx ?? 0), 0) + 1
    const title = window.prompt('新分卷标题：', `第${nextIdx}卷`)
    if (title === null) return
    try {
      await apiFetch(`/api/projects/${currentProject.id}/volumes`, {
        method: 'POST',
        body: JSON.stringify({
          title: title.trim() || `第${nextIdx}卷`,
          volume_idx: nextIdx,
        }),
      })
      await loadProjectData(currentProject.id)
    } catch (err) {
      console.error('新建分卷失败:', err)
      alert(`新建分卷失败：${err instanceof Error ? err.message : String(err)}`)
    }
  }, [currentProject, volumes, loadProjectData])

  // PR-GEN-UX Task 6: open an outline in the centre editor from GeneratePanel's
  // 查看大纲 buttons. Mirrors the sidebar tree-click path (onSelectOutline).
  const handleViewOutline = useCallback(
    (level: string) => {
      let target: OutlineEditTarget | null = null
      if (level === 'book') {
        if (bookOutlineId) {
          target = {
            type: 'book',
            outlineId: bookOutlineId,
            initialJson: bookOutlineData,
            title: '全书大纲',
          }
        }
      } else if (level === 'volume') {
        // Prefer the selected chapter's volume; fall back to the first volume
        // that has an outline.
        const selected = chapters.find((c) => c.id === selectedChapterId)
        const selVolume = selected
          ? volumes.find((v) => v.id === (selected.volume_id ?? selected.volumeId))
          : null
        const emptyTarget = selVolume ?? (volumes.length === 1 ? volumes[0] : null)
        if (currentProject && emptyTarget && !volumeOutlineIds[emptyTarget.volume_idx ?? emptyTarget.volumeIdx]) {
          setOutlineEditorTarget(null)
          setVolumeEditorEntry({ projectId: currentProject.id, volumeId: emptyTarget.id })
          selectChapter(null)
          setActiveView('editor')
          return
        }
        const preferredIdx = selVolume
          ? selVolume.volume_idx ?? selVolume.volumeIdx
          : null
        const idx =
          typeof preferredIdx === 'number' && volumeOutlineIds[preferredIdx]
            ? preferredIdx
            : Object.keys(volumeOutlineIds)
                .map(Number)
                .sort((a, b) => a - b)[0]
        if (idx !== undefined && volumeOutlineIds[idx]) {
          const idxVolume = volumes.find((v) => (v.volume_idx ?? v.volumeIdx) === idx)
          if (!idxVolume) return
          target = {
            type: 'volume',
            volumeId: idxVolume.id,
            outlineId: volumeOutlineIds[idx],
            initialJson: volumeOutlines[idx] ?? null,
            title: `${idxVolume?.title || `第${idx}卷`} · 分卷大纲`,
            volumeIdx: idx,
          }
        }
      } else if (level === 'chapter') {
        const ch = chapters.find((c) => c.id === selectedChapterId)
        if (ch) {
          const oj = ch.outline_json as Record<string, unknown> | string | null | undefined
          target = {
            type: 'chapter',
            chapterId: ch.id,
            initialJson:
              typeof oj === 'object' && oj !== null
                ? oj
                : typeof oj === 'string'
                  ? { raw_text: oj }
                  : null,
            title: `${ch.title || ''} · 章节大纲`,
          }
        }
      }
      if (!target) return
      setVolumeEditorEntry(null)
      setOutlineEditorTarget(target)
      selectChapter(null)
      setActiveView('editor')
    },
    [
      currentProject?.id,
      bookOutlineId,
      bookOutlineData,
      chapters,
      volumes,
      volumeOutlineIds,
      volumeOutlines,
      selectedChapterId,
      selectChapter,
    ],
  )

  // ----------------------------------------------------------------
  // Helpers: extract volume/chapter titles from outline text
  // ----------------------------------------------------------------

  // Cleanup on unmount
  useEffect(() => {
    // Reset on (re)mount so a StrictMode dev remount doesn't stay flagged.
    unmountedRef.current = false
    return () => {
      unmountedRef.current = true
      chapterGenerationRef.current?.generation?.detach()
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
  const currentReadiness = outlineReadiness?.chapter_id === selectedChapterId && outlineReadiness?.project_id === currentProject?.id ? outlineReadiness : null
  const outlineMissingLayers = currentReadiness?.missing_layers || []
  const canGenerateChapterProse = Boolean(currentReadiness?.ready && draftSession?.canGenerate())
  // PR-WS-OVERWRITE-GUARD: >50 non-whitespace-trimmed chars counts as real
  // prose (ignores whitespace stubs); below that, one-click generation stays.
  const chapterContentChars = editorContent.trim().length
  const chapterHasContent = Boolean(selectedChapterId && chapterContentChars > 50)
  const outlineReadinessText = !selectedChapterId
    ? '先选中一章，再看这章的链路状态。'
    : outlineReadinessLoading
      ? '正在检查大纲链路...'
      : currentReadiness?.ready
        ? !draftSession?.canGenerate() ? '链路完整，请先保存或核对当前稿件。' : chapterHasContent
          ? `本章已有正文（${chapterContentChars.toLocaleString()} 字）。大纲链路完整，可在下方重新生成（将覆盖当前内容）。`
          : '链路完整，可生成正文。'
        : currentReadiness?.block_message || '大纲链路未完成。'
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
        <div
          className="absolute inset-0 bg-black/30 motion-safe:transition-opacity motion-safe:duration-200 motion-safe:starting:opacity-0"
          onClick={() => setDrawerPanel(null)}
        />
        <div className="relative ml-auto w-full max-w-2xl bg-white shadow-xl overflow-y-auto motion-safe:transition-[transform,opacity] motion-safe:duration-200 motion-safe:starting:translate-x-6 motion-safe:starting:opacity-0">
          <div className="sticky top-0 bg-white border-b border-gray-200 px-6 py-4 flex items-center justify-between z-10">
            <h2 className="text-lg font-semibold tracking-tight text-gray-900">
              {drawerPanel === 'strand' ? '三线平衡' :
               drawerPanel === 'foreshadow' ? '伏笔追踪' :
               drawerPanel === 'cognition' ? '人物认知' :
               drawerPanel === 'arc' ? '弧进度与作者确认' :
               drawerPanel === 'settings' ? '设定集' : '角色关系'}
            </h2>
            <button onClick={() => setDrawerPanel(null)}
              className="rounded-md p-1.5 text-lg leading-none text-gray-400 transition-colors duration-150 hover:bg-gray-100 hover:text-gray-600">&#x2715;</button>
          </div>
          <div className="p-6">
            {drawerPanel === 'strand' && <StrandPanel projectId={currentProject.id} />}
            {drawerPanel === 'foreshadow' && <ForeshadowPanel projectId={currentProject.id} chapterId={currentChapter?.id} editorContent={editorContent} />}
            {drawerPanel === 'settings' && <SettingsPanel projectId={currentProject.id} />}
            {drawerPanel === 'relationship' && <CharacterCardPanel projectId={currentProject.id} />}
            {drawerPanel === 'cognition' && <CognitionPanel projectId={currentProject.id} />}
            {drawerPanel === 'arc' && <ArcProgressPanel projectId={currentProject.id} volumes={volumes}
              onCreated={() => loadProjectData(currentProject.id, false)}
              currentVolumeId={currentChapterVolume?.id} onOpenChapter={chapterId => {
                if (chapters.some(chapter => chapter.id === chapterId)) { selectChapter(chapterId); setDrawerPanel(null); setActiveView('editor') }
              }} />}
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
              className="flex items-center gap-1 text-xs text-gray-500 transition-colors duration-150 hover:text-gray-900 mb-2"
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
            {currentProject && <div className="mt-3"><GenerationSubmissionNotice projectId={currentProject.id} /></div>}
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
                onEditVolume={(volumeId) => { if (currentProject) {
                  setOutlineEditorTarget(null)
                  setVolumeEditorEntry({ projectId: currentProject.id, volumeId })
                  selectChapter(null)
                  setActiveView('editor')
                } }}
                onSelectOutline={(target) => {
                  // PR-OUTLINE-CENTER-EDIT: switch the centre editor to outline mode.
                  setVolumeEditorEntry(null)
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
                  setVolumeEditorEntry(null)
                  selectChapter(chapterId)
                  setActiveView('editor')
                }}
              />
              {/* PR-WS-CHAPTER-MGMT: append a volume at the end */}
              {currentProject && (
                <button
                  type="button"
                  onClick={handleCreateVolume}
                  className="mt-2 w-full rounded-md border border-dashed border-gray-300 px-3 py-1.5 text-xs text-gray-500 transition-colors duration-150 hover:border-gray-400 hover:bg-gray-50 hover:text-gray-700"
                >
                  + 新建分卷
                </button>
              )}
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
          {confirmedOutlineId && confirmedOutlineId === bookOutlineId && (
            <SettingsExtractionStatus state={settingsExtraction} onStart={() => { void startSettingsExtraction() }} />
          )}
          {/* PR-OUTLINE-CENTER-EDIT (2026-05-04): outline-in-centre takes priority. */}
          {volumeEditorEntry && volumeEditorEntry.projectId === currentProject?.id && currentProject ? (
            <div className="flex-1 overflow-y-auto p-4"><AuthorVolumeOutlineEditor projectId={currentProject.id}
              candidateTaskId={volumeEditorEntry.taskId} volumeId={volumeEditorEntry.volumeId} onClose={() => setVolumeEditorEntry(null)}
              onSaved={() => { void loadProjectData(currentProject.id, false); void refreshOutlineReadiness() }} /></div>
          ) : outlineEditorTarget?.type === 'volume' && currentProject ? (
            <div className="flex-1 overflow-y-auto p-4"><AuthorVolumeOutlineEditor projectId={currentProject.id}
              volumeId={outlineEditorTarget.volumeId} onClose={() => setOutlineEditorTarget(null)}
              onSaved={() => { void loadProjectData(currentProject.id, false); void refreshOutlineReadiness() }} /></div>
          ) : outlineEditorTarget && currentProject ? (
            <OutlineEditor
              projectId={currentProject.id}
              target={outlineEditorTarget}
              onClose={() => setOutlineEditorTarget(null)}
              onSaved={(t, updatedJson) => {
                if (t.type === 'book') {
                  setBookOutlineData(updatedJson)
                  const rt = (updatedJson as Record<string, unknown>)['raw_text']
                  if (typeof rt === 'string') setOutlinePreview(rt)
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
                        disabled={isGenerating || bookBusy || outlineEditing || !creativeInput.trim()}
                        className="px-6 py-2.5 bg-blue-600 text-white rounded-xl hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed text-sm font-medium"
                      >
                        {isGenerating ? '正在生成...' : '生成全书大纲'}
                      </button>
                    </div>

                    {bookError && <p role="alert" className="mt-3 text-sm text-red-700">{bookError}</p>}
                    {/* Outline preview */}
                    {outlinePreview && (
                      <div className="mt-6">
                        <div className="flex items-center justify-between mb-2">
                          <h3 className="text-sm font-semibold text-gray-700">大纲预览</h3>
                          {!isGenerating && bookOutlineId && (
                            <button
                              disabled={bookBusy}
                              onClick={() => {
                                if (outlineEditing && bookOutlineData) setOutlinePreview(outlineText(bookOutlineData))
                                setOutlineEditing(v => !v)
                              }}
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
                              disabled={bookBusy}
                              onChange={(e) => setOutlinePreview(e.target.value)}
                              className="w-full h-96 px-4 py-3 text-sm border border-gray-300 rounded-xl resize-none font-mono"
                            />
                            <div className="mt-2 flex gap-2">
                              <button
                                onClick={async () => {
                                  if (!currentProject || !bookOutlineId || !bookOutlineData || bookBusy) return
                                  const projectId = currentProject.id
                                  setBookBusy(true)
                                  setBookError(null)
                                  try {
                                    const saved = await apiFetch<OutlineRes>(`/api/projects/${projectId}/outlines/${bookOutlineId}`, {
                                      method: 'PUT',
                                      body: JSON.stringify({ content_json: { ...bookOutlineData, raw_text: outlinePreview },
                                        expected_content_json: bookOutlineData, is_confirmed: false }),
                                    })
                                    if (projectIdRef.current !== projectId || unmountedRef.current) return
                                    setBookOutlineData(saved.content_json)
                                    setOutlinePreview(outlineText(saved.content_json))
                                    setConfirmedOutlineId(null)
                                    setOutlineEditing(false)
                                  } catch {
                                    if (projectIdRef.current === projectId) setBookError('保存失败，编辑内容已保留；请核对是否存在其他修改。')
                                  } finally {
                                    if (projectIdRef.current === projectId) setBookBusy(false)
                                  }
                                }}
                                disabled={bookBusy}
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
                              disabled={!bookOutlineId || !bookOutlineData || outlineEditing || bookBusy || outlinePreview !== outlineText(bookOutlineData)}
                              className="px-4 py-2 bg-green-600 text-white rounded-lg hover:bg-green-700 text-sm font-medium"
                            >
                              确认大纲
                            </button>
                            <button
                              disabled={bookBusy || outlineEditing}
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
                                  if (!currentProject || !bookOutlineId) {
                                    setEditingPlan(false)
                                    return
                                  }
                                  setSavingPlan(true)
                                  try {
                                    await apiFetch(
                                      `/api/projects/${currentProject.id}/outlines/${bookOutlineId}/volume-plan`,
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
                              <VolumeOutlineCard
                                key={v.id}
                                volume={v}
                                data={vo}
                                projectId={currentProject.id}
                                onSaved={() => { void loadProjectData(currentProject.id, false); void refreshOutlineReadiness() }}
                                onVolumeChanged={() => { void loadProjectData(currentProject.id, false); void refreshOutlineReadiness() }}
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
                    <h3 className="text-xl font-semibold tracking-tight text-gray-900">
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
                      <span className="rounded-full bg-gray-100 px-2 py-0.5 text-[11px] tabular-nums text-gray-500">
                        {(
                          currentChapter.word_count ??
                          currentChapter.wordCount ??
                          editorContent.length
                        ).toLocaleString()}{' '}
                        字
                      </span>
                      <span
                        className={`inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-[11px] font-medium ${
                          currentChapter.status === 'completed'
                            ? 'bg-green-100 text-green-700'
                            : currentChapter.status === 'generating'
                              ? 'bg-yellow-100 text-yellow-700'
                              : currentChapter.status === 'needs_review'
                                ? 'bg-red-100 text-red-700'
                              : 'bg-gray-100 text-gray-600'
                        }`}
                      >
                        <span
                          aria-hidden
                          className={`h-1.5 w-1.5 shrink-0 rounded-full bg-current opacity-60 ${
                            currentChapter.status === 'generating' ? 'motion-safe:animate-pulse' : ''
                          }`}
                        />
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
                  {/* PR-WS-SUMMARY: collapsible 本章摘要 strip under the title */}
                  <ChapterSummaryEditor
                    projectId={currentProject!.id}
                    chapter={currentChapter}
                    onSaved={(summary) => updateChapterSummary(currentChapter.id, summary)}
                  />
                </div>
              )}

              <div className="max-w-3xl mx-auto py-4 px-6">
                {selectedChapterId && (
                  <div className="mb-3">
                    <div
                      className={`mb-2 rounded-lg border px-3 py-2 text-xs ${
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
                              className="rounded-full bg-white/70 px-2 py-0.5 text-[11px]"
                            >
                              缺 {outlineLayerLabels[layer] || layer}
                            </span>
                          ))}
                        </div>
                      )}
                    </div>
                    <div className="flex items-center gap-2">
                      <button
                        onClick={
                          // PR-WS-OVERWRITE-GUARD: existing prose → arm confirm first.
                          chapterHasContent
                            ? () => setPendingRegenerate('inline')
                            : handleGenerateChapter
                        }
                        disabled={isGenerating || !canGenerateChapterProse}
                        className="px-4 py-2 text-sm font-medium bg-green-600 text-white rounded-lg shadow-sm transition-all duration-150 hover:bg-green-700 motion-safe:active:scale-[0.98] disabled:opacity-50 disabled:cursor-not-allowed"
                      >
                        {isGenerating ? '生成中...' : chapterHasContent ? '重新生成本章' : '生成本章'}
                      </button>
                      {/* PR-WS-QUEUE: submit to the background queue instead of streaming */}
                      <button
                        onClick={
                          chapterHasContent
                            ? () => setPendingRegenerate('background')
                            : handleBackgroundGenerate
                        }
                        disabled={isGenerating || submissionPending || !canGenerateChapterProse || chapterHasActiveTask}
                        title="提交到后台队列生成，可离开页面稍后回来查看"
                        className="px-4 py-2 text-sm border border-emerald-600 text-emerald-700 rounded-lg transition-colors duration-150 hover:bg-emerald-50 disabled:opacity-50 disabled:cursor-not-allowed"
                      >
                        {chapterHasActiveTask
                          ? '后台生成中...'
                          : chapterHasContent
                            ? '后台重新生成'
                            : '后台生成本章'}
                      </button>
                    </div>
                    {/* PR-WS-OVERWRITE-GUARD: inline confirm before overwriting existing prose */}
                    {pendingRegenerate && chapterHasContent && !isGenerating && (
                      <div className="mt-2 rounded-lg border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-800 motion-safe:transition-[opacity,transform] motion-safe:duration-300 motion-safe:starting:-translate-y-1 motion-safe:starting:opacity-0">
                        <div className="flex items-start gap-2">
                          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-500" />
                          <span className="min-w-0">
                            本章已有 {chapterContentChars.toLocaleString()} 字正文，
                            {pendingRegenerate === 'background' ? '后台重新生成' : '重新生成'}
                            将覆盖当前内容。旧内容会保留在版本历史中，可随时恢复。
                          </span>
                        </div>
                        <div className="mt-2 flex items-center gap-2 pl-[22px]">
                          <button
                            type="button"
                            onClick={() => {
                              const mode = pendingRegenerate
                              setPendingRegenerate(null)
                              if (mode === 'background') {
                                void handleBackgroundGenerate()
                              } else {
                                handleGenerateChapter()
                              }
                            }}
                            className="rounded-md bg-amber-600 px-3 py-1 font-medium text-white transition-all duration-150 hover:bg-amber-700 motion-safe:active:scale-[0.97]"
                          >
                            确认重新生成
                          </button>
                          <button
                            type="button"
                            onClick={() => setPendingRegenerate(null)}
                            className="rounded-md border border-amber-300 px-3 py-1 text-amber-700 transition-colors duration-150 hover:bg-amber-100"
                          >
                            取消
                          </button>
                        </div>
                      </div>
                    )}
                    {/* PR-GEN-UX Task 1/2: phase + elapsed + cancel */}
                    {isGenerating && generationPhase && (
                      <GenerationProgressStrip
                        phase={generationPhase}
                        startedAt={generationStartedAt}
                        onCancel={handleCancelGeneration}
                      />
                    )}
                    {generationError && (
                      <div className="mt-2 flex items-start gap-2 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700 motion-safe:transition-[opacity,transform] motion-safe:duration-300 motion-safe:starting:-translate-y-1 motion-safe:starting:opacity-0">
                        <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-red-500" />
                        <span className="min-w-0">{generationError}</span>
                      </div>
                    )}
                    {/* PR-GEN-UX Task 3: truncated/refusal/cancel notices */}
                    {generationWarnings.map((warning, i) => (
                      <div
                        key={`${i}-${warning.slice(0, 12)}`}
                        className="mt-2 flex items-start justify-between gap-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800 motion-safe:transition-[opacity,transform] motion-safe:duration-300 motion-safe:starting:-translate-y-1 motion-safe:starting:opacity-0"
                      >
                        <span className="flex min-w-0 items-start gap-2">
                          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-500" />
                          <span>{warning}</span>
                        </span>
                        <button
                          type="button"
                          onClick={() =>
                            setGenerationWarnings((prev) => prev.filter((_, wi) => wi !== i))
                          }
                          className="shrink-0 rounded px-1.5 py-0.5 text-amber-700 transition-colors duration-150 hover:bg-amber-100"
                        >
                          关闭
                        </button>
                      </div>
                    ))}
                  </div>
                )}
                {draftSession && draftState && <ChapterSaveStatus key={`${draftState.projectId}:${draftState.chapterId}`} session={draftSession} state={draftState} />}
                <textarea
                  aria-label="章节正文"
                  value={editorContent}
                  onChange={(e) => handleEditorChange(e.target.value)}
                  onMouseUp={(e) => {
                    // PR-GEN-UX Task 5: selection-based rewrite. Plain-textarea
                    // offsets are enough; the menu anchors at the mouseup point.
                    if (isGenerating || !selectedChapterId || !draftSession?.canEdit()) return
                    const ta = e.currentTarget
                    const start = ta.selectionStart ?? 0
                    const end = ta.selectionEnd ?? 0
                    const text = editorContent.slice(start, end)
                    if (end > start && text.trim()) {
                      setRewriteSel({
                        sessionId: ++rewriteSequence.current,
                        chapterId: selectedChapterId,
                        sourceText: editorContent,
                        start,
                        end,
                        text,
                        position: {
                          top: Math.min(e.clientY + 10, window.innerHeight - 160),
                          left: Math.max(8, Math.min(e.clientX, window.innerWidth - 400)),
                        },
                      })
                    } else {
                      setRewriteSel(null)
                    }
                  }}
                  placeholder={
                    selectedChapterId
                      ? '章节内容将在此显示。点击 "生成本章" 按钮开始生成...'
                      : '请从左侧选择一个章节进行编辑...'
                  }
                  className="w-full min-h-[500px] rounded-xl p-5 text-[17px] leading-loose text-gray-800 border-none outline-none resize-none ring-1 ring-transparent transition-[box-shadow,background-color] duration-200 focus:bg-white focus:ring-gray-200"
                  style={{ fontFamily: "'Noto Serif SC', serif" }}
                  readOnly={isGenerating || !draftSession?.canEdit()}
                />
                {rewriteSel && !isGenerating && selectedChapterId && (
                  <RewriteMenu
                    key={rewriteSel.sessionId}
                    selectedText={rewriteSel.text}
                    contextBefore={rewriteSel.sourceText.slice(0, rewriteSel.start)}
                    contextAfter={rewriteSel.sourceText.slice(rewriteSel.end)}
                    position={rewriteSel.position}
                    onAccept={(newText, operation) => {
                      const applied = applyRewriteCandidate(rewriteSel, selectedChapterId, editorContent, newText, operation)
                      if (applied === null) {
                        setGenerationError('正文或选段已变化，未应用旧改写。请重新选择后生成。')
                        return
                      }
                      handleEditorChange(applied)
                      setRewriteSel(null)
                    }}
                    onReject={() => setRewriteSel(null)}
                    onClose={() => setRewriteSel(null)}
                  />
                )}
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
                    outlineReadiness={currentReadiness}
                    outlineReadinessLoading={outlineReadinessLoading}
                    onGenerate={handleGenerateChapter}
                    onGenerateOutline={handleGenerateOutline}
                    onViewOutline={handleViewOutline}
                  />
                </WorkbenchCard>

                <WorkbenchCard title="弧进度" icon={ClipboardCheck}>
                  <DrawerLinkButton label="审读与确认本弧" icon={ClipboardCheck}
                    hint="核对具体保存稿，记录完成章节与后续方向。" onClick={() => setDrawerPanel('arc')} />
                </WorkbenchCard>

                {/* PR-WS-QUEUE: background generation tasks */}
                <WorkbenchCard title="任务队列" icon={ListChecks}>
                  {queueError && <div className="mb-2 text-xs text-red-700"><p role="alert">{queueError}</p>
                    <button type="button" onClick={() => void refreshQueue()} className="mt-1 text-blue-800">重新读取后台任务</button></div>}
                  <TaskQueuePanel
                    tasks={queueTasks}
                    cancelUnavailable={queueCancelUnavailable}
                    onCancel={handleCancelQueueTask}
                    onRefreshChapter={handleRefreshChapterFromTask}
                    onReviewVolume={(taskId) => { if (currentProject) {
                      setOutlineEditorTarget(null)
                      setVolumeEditorEntry({ projectId: currentProject.id, taskId })
                      setActiveView('editor')
                    } }}
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
                      hint="查看角色档案、状态与关系变化。"
                      onClick={() => setDrawerPanel('relationship')}
                    />
                    <DrawerLinkButton
                      label="人物认知"
                      icon={Brain}
                      hint="核对谁在何时知情，查看原文依据并处理提取失败。"
                      onClick={() => setDrawerPanel('cognition')}
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
                    {/* PR-WS-GRAPH: link out to the standalone relationship graph page */}
                    {currentProject && (
                      <a
                        href={`/relationship-graph?id=${currentProject.id}`}
                        target="_blank"
                        rel="noreferrer"
                        className="flex items-center justify-center gap-1.5 rounded-md border border-gray-900 bg-gray-900 px-2.5 py-2 text-xs font-medium text-white shadow-sm transition-all duration-150 hover:bg-gray-800 motion-safe:active:scale-[0.98]"
                      >
                        <Network className="h-3.5 w-3.5" />
                        打开独立关系图页面 ↗
                      </a>
                    )}
                    <p className="rounded-md bg-gray-50 px-2.5 py-2 leading-relaxed">
                      关系图基于已沉淀的角色与关系数据；下方入口继续提供角色、伏笔和剧情线视图。
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

// PR-GEN-UX Task 1/2: compact status line shown above the chapter textarea
// while generation streams — current phase, elapsed time, cancel button.
function GenerationProgressStrip({
  phase,
  startedAt,
  onCancel,
}: {
  phase: string
  startedAt: number | null
  onCancel: () => void
}) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [])
  const elapsed = startedAt ? Math.max(0, Math.floor((now - startedAt) / 1000)) : 0
  const mm = Math.floor(elapsed / 60)
  const ss = String(elapsed % 60).padStart(2, '0')
  return (
    <div className="mt-2 flex items-center justify-between gap-2 rounded-lg border border-blue-200 bg-blue-50 px-3 py-2 text-xs text-blue-800 motion-safe:transition-[opacity,transform] motion-safe:duration-300 motion-safe:starting:-translate-y-1 motion-safe:starting:opacity-0">
      <div className="flex min-w-0 items-center gap-2">
        <span className="relative flex h-2 w-2 shrink-0">
          <span className="absolute inline-flex h-full w-full rounded-full bg-blue-400 opacity-75 motion-safe:animate-ping" />
          <span className="relative inline-flex h-2 w-2 rounded-full bg-blue-500" />
        </span>
        <span className="truncate">{phase}</span>
      </div>
      <div className="flex shrink-0 items-center gap-2">
        <span className="tabular-nums text-blue-600">
          {mm}:{ss}
        </span>
        <button
          type="button"
          onClick={onCancel}
          className="rounded-md border border-transparent px-2 py-0.5 text-red-600 transition-colors duration-150 hover:border-red-200 hover:bg-red-50"
        >
          取消生成
        </button>
      </div>
    </div>
  )
}

// PR-WS-SUMMARY: collapsible 本章摘要 strip under the chapter title. Summaries
// feed the AI's chapter memory, so edits go straight to PUT {summary}.
function ChapterSummaryEditor({
  projectId,
  chapter,
  onSaved,
}: {
  projectId: string
  chapter: Chapter
  onSaved: (summary: string | null) => void
}) {
  const [open, setOpen] = useState(false)
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(chapter.summary ?? '')
  const [saving, setSaving] = useState(false)

  // Reset local edit state when switching chapters.
  useEffect(() => {
    setDraft(chapter.summary ?? '')
    setEditing(false)
  }, [chapter.id]) // eslint-disable-line react-hooks/exhaustive-deps

  const save = async () => {
    if (saving) return
    setSaving(true)
    try {
      const trimmed = draft.trim()
      await apiFetch(`/api/projects/${projectId}/chapters/${chapter.id}`, {
        method: 'PUT',
        body: JSON.stringify({ summary: trimmed || null }),
      })
      onSaved(trimmed || null)
      setEditing(false)
    } catch (err) {
      console.error('保存章节摘要失败:', err)
      alert('保存摘要失败，请重试。')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="mb-2 overflow-hidden rounded-lg border border-gray-200 bg-gray-50">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center justify-between px-3 py-1.5 text-left transition-colors duration-150 hover:bg-gray-100"
      >
        <span className="text-xs font-medium text-gray-700">
          本章摘要
          {!open && chapter.summary && (
            <span className="ml-2 font-normal text-gray-400">
              {chapter.summary.slice(0, 30)}
              {chapter.summary.length > 30 ? '…' : ''}
            </span>
          )}
        </span>
        <span className="text-xs text-gray-400">{open ? '收起' : '展开'}</span>
      </button>
      {open && (
        <div className="border-t border-gray-200 px-3 py-2 motion-safe:transition-opacity motion-safe:duration-200 motion-safe:starting:opacity-0">
          {editing ? (
            <div>
              <textarea
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                placeholder="概括本章剧情要点、人物变化和承接线索..."
                className="w-full h-24 px-2 py-1.5 text-xs border border-gray-300 rounded-md resize-none transition-shadow duration-150 focus:outline-none focus:ring-2 focus:ring-blue-400/40 focus:border-blue-300"
              />
              <div className="mt-1.5 flex items-center gap-2">
                <button
                  type="button"
                  onClick={save}
                  disabled={saving}
                  className="px-3 py-1 text-xs font-medium bg-blue-600 text-white rounded-md transition-all duration-150 hover:bg-blue-700 motion-safe:active:scale-[0.97] disabled:opacity-50"
                >
                  {saving ? '保存中...' : '保存'}
                </button>
                <button
                  type="button"
                  onClick={() => {
                    setDraft(chapter.summary ?? '')
                    setEditing(false)
                  }}
                  className="px-3 py-1 text-xs text-gray-600 rounded-md border border-gray-200 bg-white transition-colors duration-150 hover:bg-gray-100"
                >
                  取消
                </button>
              </div>
            </div>
          ) : (
            <div>
              {chapter.summary ? (
                <p className="whitespace-pre-wrap text-xs leading-relaxed text-gray-700">
                  {chapter.summary}
                </p>
              ) : (
                <p className="text-xs text-gray-400">暂无摘要。</p>
              )}
              <div className="mt-1.5 flex items-center justify-between gap-2">
                <span className="text-[11px] text-gray-400">
                  摘要会写入 AI 的章节记忆，作为后续章节生成的上下文。
                </span>
                <button
                  type="button"
                  onClick={() => {
                    setDraft(chapter.summary ?? '')
                    setEditing(true)
                  }}
                  className="shrink-0 text-xs text-blue-600 hover:underline"
                >
                  编辑
                </button>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

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
        <button onClick={() => setEditing(true)} className="rounded-full bg-gray-100 px-2 py-0.5 text-[11px] tabular-nums transition-colors duration-150 hover:bg-gray-200 hover:text-gray-800">
          目标 {effective ? `${effective.toLocaleString()} 字` : '未设'}
          {initial == null && projectDefault ? '（默认）' : ''}
        </button>
      )}
    </span>
  )
}

function VolumeOutlineCard({
  volume, data, projectId, onSaved, onVolumeChanged,
}: {
  volume: Volume
  data: Record<string, unknown>
  projectId: string
  onSaved: () => void
  onVolumeChanged?: () => void
}) {
  const [editing, setEditing] = useState(false)
  const [operationError, setOperationError] = useState<string | null>(null)
  const [structureBusy, setStructureBusy] = useState(false)
  // PR-OL7: inline edit volume title (separate from outline content edit).
  const [editingTitle, setEditingTitle] = useState(false)
  const [titleDraft, setTitleDraft] = useState(volume.title || "")
  const [savingTitle, setSavingTitle] = useState(false)

  const saveTitle = async () => {
    if (savingTitle || structureBusy || !titleDraft.trim()) return
    setSavingTitle(true)
    try {
      const updated = await apiFetch<{ id: string; title: string }>(
        `/api/projects/${projectId}/volumes/${volume.id}`,
        { method: "PUT", body: JSON.stringify({ title: titleDraft.trim() }) },
      )
      if (updated.id !== volume.id) throw new Error('返回了另一分卷的保存结果。')
      setEditingTitle(false)
      onVolumeChanged?.()
    } catch (err) {
      setOperationError('卷名保存未确认，请重新读取目录核对。')
    } finally {
      setSavingTitle(false)
    }
  }

  // PR-OL8: move (swap volume_idx) and delete.
  const moveVolume = async (direction: "up" | "down") => {
    if (structureBusy || savingTitle) return
    setStructureBusy(true)
    setOperationError(null)
    try {
      const allVolumes = await apiFetch<Volume[]>(`/api/projects/${projectId}/volumes`)
      const sorted = [...allVolumes].sort(
        (a, b) => (a.volume_idx ?? a.volumeIdx) - (b.volume_idx ?? b.volumeIdx),
      )
      const position = sorted.findIndex((item) => item.id === volume.id)
      const actual = sorted[position]
      const target = sorted[position + (direction === 'up' ? -1 : 1)]
      if (!actual || !target) {
        setOperationError(!actual ? '当前分卷已变化，请重新读取目录。' : direction === 'up' ? '已是第一卷。' : '已是最后一卷。')
        return
      }
      await apiFetch(`/api/projects/${projectId}/volumes/${volume.id}/move`, {
        method: 'POST', body: JSON.stringify({ neighbor_id: target.id,
          expected_idx: actual.volume_idx ?? actual.volumeIdx,
          expected_neighbor_idx: target.volume_idx ?? target.volumeIdx }),
      })
      onVolumeChanged?.()
    } catch (err) {
      setOperationError('分卷移动未确认，请先重新读取目录，核对实际顺序后再操作。')
      onVolumeChanged?.()
    } finally { setStructureBusy(false) }
  }

  const deleteVolume = async () => {
    if (structureBusy || savingTitle || !confirm(`确定删除 《${volume.title}》 及其所有章节吗？此操作不可逆。`)) return
    setStructureBusy(true)
    setOperationError(null)
    try {
      await apiFetch(`/api/projects/${projectId}/volumes/${volume.id}`, { method: "DELETE" })
      onVolumeChanged?.()
    } catch (err) {
      setOperationError('分卷删除未确认，请重新读取目录核对。')
      onVolumeChanged?.()
    } finally { setStructureBusy(false) }
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
            disabled={structureBusy || savingTitle}
            onClick={(e) => { e.preventDefault(); void moveVolume("up") }}
            className="text-xs px-1.5 py-0.5 rounded bg-gray-100 hover:bg-gray-200"
            title="上移"
          >↑</button>
          <button
            disabled={structureBusy || savingTitle}
            onClick={(e) => { e.preventDefault(); void moveVolume("down") }}
            className="text-xs px-1.5 py-0.5 rounded bg-gray-100 hover:bg-gray-200"
            title="下移"
          >↓</button>
          <button
            disabled={structureBusy || savingTitle}
            onClick={(e) => { e.preventDefault(); void deleteVolume() }}
            className="text-xs px-1.5 py-0.5 rounded text-red-600 hover:bg-red-50"
            title="删除本卷"
          >✖</button>
          <button
            onClick={(e) => { e.preventDefault(); setEditing((v) => !v) }}
            className="text-xs text-blue-600 hover:underline ml-2"
          >
            {editing ? '收起编辑稿' : '编辑大纲'}
          </button>
        </div>
      </summary>
      <div className="px-4 py-3 bg-white border-t text-sm">
        {operationError && <p role="alert" className="mb-3 text-red-700">{operationError}</p>}
        {editing ? (
          <AuthorVolumeOutlineEditor projectId={projectId} volumeId={volume.id}
            onClose={() => setEditing(false)} onSaved={onSaved} />
        ) : (
          <VolumeOutlineBlock data={data} />
        )}
      </div>
    </details>
  )
}
