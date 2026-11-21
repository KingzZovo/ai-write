'use client'

import { useState, useEffect, useCallback, useRef } from 'react'
import type { ReactNode } from 'react'
import dynamic from 'next/dynamic'
import { useRouter, useSearchParams } from 'next/navigation'
import { Brain, FileCheck2, Network, PenLine, ShieldCheck } from 'lucide-react'
import { apiFetch } from '@/lib/api'
import { useChapterDraft } from '@/components/editor/useChapterDraft'
import { ChapterSaveStatus } from '@/components/editor/ChapterSaveStatus'
import { getChapterDraftSession, preserveChapterDrafts } from '@/components/editor/chapterDraftSession'
import { MobileTask, isMobileTaskTerminal, mergeMobileTaskLists, parseMobileTask, parseMobileTasks } from './mobileTaskProgress'
import { useMobileTaskProgress } from './useMobileTaskProgress'
import { MobileTaskPanel } from './MobileTaskPanel'
import { GenerationSubmissionNotice } from './GenerationSubmissionNotice'
import { useGenerationSubmission } from './useGenerationSubmission'
import type { GenerationInput } from '@/lib/generationSubmission'
import { statusLabel as taskStatusLabel, taskTypeLabel } from './asyncTaskQueue'
import { getSelectedStructureBookId } from '@/components/panels/GeneratePanel'
import { selectVolumeOutlines } from '@/lib/volumeOutlineSelection'
import { volumeOutlineContentText } from '@/lib/volumeOutlines'

// Lazy load panels — only when user opens the tools tab
const GeneratePanel = dynamic(() => import('@/components/panels/GeneratePanel').then(m => ({ default: m.GeneratePanel })), { ssr: false })
const ForeshadowPanel = dynamic(() => import('@/components/panels/ForeshadowPanel').then(m => ({ default: m.ForeshadowPanel })), { ssr: false })
const EvaluationPanel = dynamic(() => import('@/components/panels/EvaluationPanel').then(m => ({ default: m.EvaluationPanel })), { ssr: false })
const WritingGuidePanel = dynamic(() => import('@/components/panels/WritingGuidePanel').then(m => ({ default: m.WritingGuidePanel })), { ssr: false })
const SettingsPanel = dynamic(() => import('@/components/panels/SettingsPanel').then(m => ({ default: m.SettingsPanel })), { ssr: false })
const CognitionPanel = dynamic(() => import('@/components/panels/CognitionPanel').then(m => ({ default: m.CognitionPanel })), { ssr: false })
const ArcProgressPanel = dynamic(() => import('@/components/arc/ArcProgressPanel').then(m => m.ArcProgressPanel), { ssr: false })
const VolumeOutlineEditor = dynamic(() => import('@/components/outline/VolumeOutlineEditor').then(m => m.VolumeOutlineEditor), { ssr: false })

interface Project { id: string; title: string; genre: string }
interface Volume { id: string; title: string; volume_idx: number }
interface Chapter {
  id: string
  title: string
  chapter_idx: number
  word_count: number
  status: string
  content_text?: string
  summary?: string | null
  outline_json?: Record<string, unknown> | null
  volume_id?: string
  volumeId?: string
  target_word_count?: number | null
}

interface Outline { id: string; level: string; content_json: Record<string, unknown>; is_confirmed?: number | boolean; version?: number; created_at?: string | null }
interface OutlineReadinessLayer { ready: boolean; detail?: string | null }
interface OutlineReadinessInfo {
  project_id: string
  chapter_id: string | null
  ready: boolean
  missing_layers: string[]
  block_message?: string | null
  layers: Record<'book' | 'volume' | 'chapter', OutlineReadinessLayer>
}

function compactText(value: unknown): string {
  if (value === null || value === undefined) return ''
  if (typeof value === 'string') return value.trim()
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  if (Array.isArray(value)) {
    return value
      .map((item, index) => {
        if (typeof item === 'string') return `${index + 1}. ${item}`
        if (item && typeof item === 'object') {
          const obj = item as Record<string, unknown>
          const title = compactText(obj.title || obj.name || obj.part)
          const body = compactText(obj.summary || obj.goal || obj.core || obj.main_progress || obj.hook)
          return `${index + 1}. ${[title, body].filter(Boolean).join('：')}`
        }
        return compactText(item)
      })
      .filter(Boolean)
      .join('\n')
  }
  if (typeof value === 'object') {
    return Object.entries(value as Record<string, unknown>)
      .map(([key, item]) => `${key}：${compactText(item)}`)
      .filter((line) => !line.endsWith('：'))
      .join('\n')
  }
  return ''
}

function formatOutline(data: Record<string, unknown> | null | undefined, kind: 'book' | 'volume' | 'chapter'): string {
  if (!data) return '暂无大纲内容。'
  if (kind === 'volume') return volumeOutlineContentText(data)
  const sections: string[] = []
  const add = (label: string, value: unknown) => {
    const text = compactText(value)
    if (text) sections.push(`${label}\n${text}`)
  }

  if (kind === 'book') {
    add('主线', data.main_plot || data.raw_text)
    add('世界设定', data.world_setting)
    add('核心人物', data.characters)
    add('三部结构', data.part_structure)
    add('分卷规划', data.volume_plan)
    add('创作约束', data.narrative_constraints)
  } else {
    add('章节摘要', data.summary || data.raw_text)
    add('本章功能', data.purpose)
    add('承接上文', data.continuity_from_previous)
    add('引向下章', data.continuity_to_next)
    add('线索', data.clues)
    add('人物推进', data.character_progress)
    add('正文约束', data.draft_rule || data.hierarchy_guard)
  }

  return sections.join('\n\n') || compactText(data.raw_text) || JSON.stringify(data, null, 2)
}

function MobileOutlineDisclosure({
  title,
  subtitle,
  outlineKey,
  openOutlineKey,
  onToggle,
  children,
}: {
  title: string
  subtitle?: string
  outlineKey: string
  openOutlineKey: string | null
  onToggle: (key: string) => void
  children: ReactNode
}) {
  const open = openOutlineKey === '__all__' || openOutlineKey === outlineKey
  return (
    <div className="bg-white border border-gray-200 rounded-lg overflow-hidden">
      <button
        type="button"
        onClick={() => onToggle(outlineKey)}
        className="w-full px-3 py-2.5 text-left flex items-center justify-between active:bg-gray-50"
      >
        <span className="min-w-0">
          <span className="block text-sm font-semibold text-gray-900 truncate">{title}</span>
          {subtitle && <span className="block text-xs text-gray-500 mt-0.5 truncate">{subtitle}</span>}
        </span>
        <span className="text-xs text-gray-500 ml-3 shrink-0">{open ? '收起' : '查看'}</span>
      </button>
      {open && (
        <pre className="whitespace-pre-wrap text-sm text-gray-700 bg-gray-50 border-t border-gray-100 p-3 max-h-[52vh] overflow-y-auto leading-relaxed">
          {children}
        </pre>
      )}
    </div>
  )
}

export default function MobileWorkspace() {
  const router = useRouter()
  const searchParams = useSearchParams()
  const urlProjectId = searchParams.get('id')

  const [projects, setProjects] = useState<Project[]>([])
  const [loadedProject, setLoadedProject] = useState<Project | null>(null)
  const currentProject = loadedProject?.id === urlProjectId ? loadedProject : null
  const [projectError, setProjectError] = useState<string | null>(null)
  const [volumes, setVolumes] = useState<Volume[]>([])
  const [savedOutline, setSavedOutline] = useState('')
  const [bookOutlineData, setBookOutlineData] = useState<Record<string, unknown> | null>(null)
  const [volumeOutlines, setVolumeOutlines] = useState<Record<string, Record<string, unknown>>>({})
  const [volumeEditor, setVolumeEditor] = useState<{ projectId: string; volumeId?: string; candidateTaskId?: string } | null>(null)
  const [openOutlineKey, setOpenOutlineKey] = useState<string | null>('__all__')
  const [chapters, setChapters] = useState<Chapter[]>([])
  const [selection, setSelection] = useState<{ projectId: string; chapter: Chapter } | null>(null)
  const selectedChapter = currentProject && selection?.projectId === currentProject.id ? selection.chapter : null
  const targetVolume = selectedChapter
    ? volumes.find((volume) => volume.id === (selectedChapter.volume_id ?? selectedChapter.volumeId))
    : volumes.length === 1 ? volumes[0] : null
  const { session: draftSession, state: draftState } = useChapterDraft(currentProject?.id, selectedChapter?.id ?? null)
  const { session: submission, state: submissionState } = useGenerationSubmission(currentProject?.id)
  const submissionBlocked = Boolean(submissionState?.pending || submissionState?.storageError || !submissionState?.initialized)
  const editorContent = draftState?.text ?? ''
  const [tab, setTab] = useState<'list' | 'editor' | 'tools' | 'create' | 'outline'>('list')
  const [creativeInput, setCreativeInput] = useState('')
  const [outlineReadiness, setOutlineReadiness] = useState<OutlineReadinessInfo | null>(null)
  const [outlineReadinessLoading, setOutlineReadinessLoading] = useState(false)
  const [readinessRevision, setReadinessRevision] = useState(0)
  const [newTitle, setNewTitle] = useState('')
  const [newGenre, setNewGenre] = useState('')
  const [toolsTab, setToolsTab] = useState<'generate' | 'guide' | 'foreshadow' | 'settings' | 'cognition' | 'eval' | 'arc'>('generate')
  const [taskList, setTaskList] = useState<{ projectId: string; rows: MobileTask[]; ready: boolean; error: string | null } | null>(null)
  const tasks = taskList?.projectId === currentProject?.id ? taskList?.rows ?? [] : []
  const tasksReady = taskList?.projectId === currentProject?.id && taskList?.ready === true
  const [pollingTask, setPollingTask] = useState<MobileTask | null>(null)
  const currentTask = pollingTask?.projectId === currentProject?.id ? pollingTask : null
  const taskProgress = useMobileTaskProgress(currentTask)
  const [submittingProject, setSubmittingProject] = useState<string | null>(null)
  const operation = useRef<object | null>(null)
  const projectRequest = useRef(0)
  const dataRequest = useRef(0)
  const taskListRequest = useRef(0)
  const taskListBusy = useRef(false)
  const [projectReload, setProjectReload] = useState(0)
  const isGenerating = submittingProject === currentProject?.id || tasks.some((task) =>
    !isMobileTaskTerminal(task.taskId === taskProgress.detail?.taskId ? taskProgress.detail.status : task.status))
  const currentReadiness = currentProject && outlineReadiness?.project_id === currentProject.id &&
    outlineReadiness.chapter_id === selectedChapter?.id ? outlineReadiness : null
  const canGenerateProse = !!draftSession?.canGenerate() && tasksReady && !isGenerating && !submissionBlocked &&
    !outlineReadinessLoading && !!currentReadiness?.ready

  const toggleOutline = useCallback((key: string) => {
    setOpenOutlineKey((previous) => previous === key ? null : key)
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    apiFetch<{ projects: Project[] }>('/api/projects', { signal: controller.signal })
      .then((data) => { if (!controller.signal.aborted) setProjects(data.projects) })
      .catch(() => { /* The project-specific read below reports loading failures. */ })
    return () => controller.abort()
  }, [])

  const refreshProjectData = useCallback(async (projectId: string, epoch: number, signal?: AbortSignal) => {
    const request = ++dataRequest.current
    const valid = () => projectRequest.current === epoch && dataRequest.current === request && !signal?.aborted
    try {
      const [vols, chs, outlines] = await Promise.all([
        apiFetch<Volume[]>(`/api/projects/${projectId}/volumes`, { signal }),
        apiFetch<Chapter[]>(`/api/projects/${projectId}/chapters?lightweight=true`, { signal }),
        apiFetch<Outline[]>(`/api/projects/${projectId}/outlines`, { signal }),
      ])
      if (!valid()) return
      if (!Array.isArray(vols) || !Array.isArray(chs) || !Array.isArray(outlines)) throw new Error('项目资料返回不完整。')
      setVolumes(vols)
      setChapters(chs)
      setSelection((previous) => previous?.projectId === projectId
        ? { projectId, chapter: chs.find((chapter) => chapter.id === previous.chapter.id) ?? previous.chapter } : previous)
      const bookOutlines = outlines.filter((outline) => outline.level === 'book' &&
        !('volume_idx' in (outline.content_json || {}))).sort((a, b) => (a.id < b.id ? -1 : 1))
      const book = bookOutlines.find((outline) => Boolean(outline.is_confirmed)) || bookOutlines[0]
      setBookOutlineData(book?.content_json ?? null)
      setSavedOutline(book ? formatOutline(book.content_json, 'book').replace(/<volume-plan>[\s\S]+?<\/volume-plan>\s*/g, '') : '')
      setVolumeOutlines(Object.fromEntries(Object.entries(selectVolumeOutlines(outlines, vols))
        .map(([id, outline]) => [id, outline.content_json])))
      setProjectError(null)
    } catch (error) {
      if (valid()) setProjectError(error instanceof Error ? error.message : '目录或大纲加载失败，请重新读取。')
    }
  }, [])

  const refreshTasks = useCallback(async (projectId: string, epoch: number, signal?: AbortSignal) => {
    const request = ++taskListRequest.current
    const valid = () => projectRequest.current === epoch && taskListRequest.current === request && !signal?.aborted
    taskListBusy.current = true
    setTaskList((previous) => ({ projectId, rows: previous?.projectId === projectId ? previous.rows : [], ready: false, error: null }))
    try {
      // Recent history may hide a still-active task in a long-running book.
      const [recentRaw, activeRaw] = await Promise.all([
        apiFetch<unknown>(`/api/generate/async/project/${projectId}?limit=20`, { signal }),
        apiFetch<unknown>(`/api/generate/async/project/${projectId}?limit=100&active_only=true`, { signal }),
      ])
      const recent = parseMobileTasks(recentRaw, projectId), active = parseMobileTasks(activeRaw, projectId)
      const rows = mergeMobileTaskLists(recent, active)
      if (!valid()) return
      setTaskList({ projectId, rows, ready: true, error: null })
      setPollingTask((previous) => rows.find((task) => !isMobileTaskTerminal(task.status)) ||
        rows.find((task) => task.taskId === previous?.taskId) || rows[0] || null)
    } catch (error) {
      if (valid()) setTaskList((previous) => ({ projectId, rows: previous?.projectId === projectId ? previous.rows : [], ready: false,
        error: error instanceof Error ? error.message : '后台任务未能确认，请重新读取。' }))
    } finally { if (valid()) taskListBusy.current = false }
  }, [])

  useEffect(() => {
    const epoch = ++projectRequest.current
    const controller = new AbortController()
    operation.current = null
    taskListBusy.current = false
    setLoadedProject(null)
    setSelection(null)
    setVolumes([])
    setChapters([])
    setBookOutlineData(null)
    setVolumeOutlines({})
    setVolumeEditor(null)
    setSavedOutline('')
    setCreativeInput('')
    setTaskList(null)
    setPollingTask(null)
    setSubmittingProject(null)
    setProjectError(null)
    setOpenOutlineKey('__all__')
    setTab('list')
    if (!urlProjectId) { router.replace('/'); return () => { controller.abort(); projectRequest.current += 1 } }
    const valid = () => epoch === projectRequest.current && !controller.signal.aborted
    void (async () => {
      try {
        const project = await apiFetch<Project>(`/api/projects/${urlProjectId}`, { signal: controller.signal })
        if (!valid()) return
        if (project.id !== urlProjectId) throw new Error('作品来源不一致，请重新读取。')
        setLoadedProject(project)
        await Promise.allSettled([
          refreshProjectData(project.id, epoch, controller.signal),
          refreshTasks(project.id, epoch, controller.signal),
        ])
      } catch (error) {
        if (valid()) setProjectError(error instanceof Error ? error.message : '作品加载失败，请重新读取。')
      }
    })()
    return () => { controller.abort(); projectRequest.current += 1; preserveChapterDrafts() }
  }, [urlProjectId, projectReload, refreshProjectData, refreshTasks, router])

  const selectChapter = useCallback((chapter: Chapter) => {
    if (!currentProject || !chapters.some((item) => item.id === chapter.id)) return
    draftSession?.preserve()
    setSelection({ projectId: currentProject.id, chapter })
    setTab('editor')
  }, [currentProject, chapters, draftSession])

  useEffect(() => {
    const source = draftState?.source
    if (!source || source.project_id !== currentProject?.id || source.chapter_id !== selectedChapter?.id) return
    setChapters((previous) => previous.map((chapter) => chapter.id === source.chapter_id
      ? { ...chapter, word_count: source.word_count, status: source.status, title: source.title } : chapter))
  }, [draftState?.source, currentProject?.id, selectedChapter?.id])

  useEffect(() => {
    const controller = new AbortController()
    const projectId = currentProject?.id, chapterId = selectedChapter?.id
    setOutlineReadiness(null)
    if (!projectId || !chapterId) { setOutlineReadinessLoading(false); return () => controller.abort() }
    setOutlineReadinessLoading(true)
    apiFetch<OutlineReadinessInfo>(`/api/projects/${projectId}/outline-readiness?chapter_id=${chapterId}`, { signal: controller.signal })
      .then((report) => {
        if (!controller.signal.aborted) setOutlineReadiness(report.project_id === projectId && report.chapter_id === chapterId ? report : null)
      })
      .catch(() => { if (!controller.signal.aborted) setOutlineReadiness(null) })
      .finally(() => { if (!controller.signal.aborted) setOutlineReadinessLoading(false) })
    return () => controller.abort()
  }, [currentProject?.id, selectedChapter?.id, readinessRevision])

  // Task results never become the editor's source. Only a manuscript read can
  // acknowledge the body's current state; a dirty session retains its draft.
  const finishedTasks = useRef(new Set<string>())
  useEffect(() => {
    const detail = taskProgress.detail
    if (!detail || detail.projectId !== currentProject?.id) return
    setTaskList((previous) => previous?.projectId === detail.projectId
      ? { ...previous, rows: previous.rows.map((task) => task.taskId === detail.taskId ? { ...task, status: detail.status } : task) } : previous)
    if (!isMobileTaskTerminal(detail.status)) return
    const key = `${detail.projectId}:${detail.taskId}:${detail.status}`
    if (finishedTasks.current.has(key)) return
    finishedTasks.current.add(key)
    if (detail.taskType === 'chapter' && detail.chapterId) {
      void getChapterDraftSession(detail.projectId, detail.chapterId).reload()
    }
    void refreshProjectData(detail.projectId, projectRequest.current)
    void refreshTasks(detail.projectId, projectRequest.current)
    setReadinessRevision((value) => value + 1)
  }, [taskProgress.detail, currentProject?.id, refreshProjectData, refreshTasks])

  useEffect(() => {
    if (!currentTask || !isMobileTaskTerminal(taskProgress.detail?.status ?? currentTask.status)) return
    const next = tasks.find((task) => task.taskId !== currentTask.taskId && !isMobileTaskTerminal(task.status))
    if (next) setPollingTask(next)
  }, [currentTask, taskProgress.detail?.status, tasks])

  const handleCreateProject = async () => {
    if (!newTitle.trim()) return
    const epoch = projectRequest.current
    try {
      const project = await apiFetch<Project>('/api/projects', { method: 'POST', body: JSON.stringify({ title: newTitle, genre: newGenre }) })
      setProjects((previous) => [...previous, project])
      if (epoch !== projectRequest.current) return
      setNewTitle('')
      setNewGenre('')
      router.push(`/workspace?id=${encodeURIComponent(project.id)}`)
    } catch (error) {
      if (epoch === projectRequest.current) setProjectError(error instanceof Error ? error.message : '创建作品失败。')
    }
  }

  const beginOperation = () => {
    if (!currentProject || operation.current || taskListBusy.current || !tasksReady || isGenerating || submissionBlocked) return null
    const token = {}
    operation.current = token
    setSubmittingProject(currentProject.id)
    return { token, projectId: currentProject.id, epoch: projectRequest.current }
  }
  const finishOperation = (token: object) => {
    if (operation.current === token) { operation.current = null; setSubmittingProject(null) }
  }

  const acceptedSubmission = submissionState?.receipt
  useEffect(() => {
    if (!acceptedSubmission || acceptedSubmission.project_id !== currentProject?.id) return
    const task = parseMobileTask(acceptedSubmission, acceptedSubmission.project_id)
    taskListRequest.current += 1
    taskListBusy.current = false
    setTaskList((previous) => ({ projectId: task.projectId, ready: true, error: null,
      rows: mergeMobileTaskLists(previous?.projectId === task.projectId ? previous.rows : [], [task]) }))
    setPollingTask(task)
    void refreshTasks(task.projectId, projectRequest.current)
  }, [acceptedSubmission, currentProject?.id, refreshTasks])

  const submitTask = async (taskType: GenerationInput['task_type'], chapterId: string | null = null, volumeIdx: number | null = null) => {
    const owner = beginOperation()
    if (!owner || !submission) return
    const valid = () => operation.current === owner.token && projectRequest.current === owner.epoch
    try {
      await submission.submit({ project_id: owner.projectId, task_type: taskType,
          ...(volumeIdx ? { volume_idx: volumeIdx } : {}),
          ...(chapterId ? { chapter_id: chapterId, expected_content_text: draftSession?.getSnapshot().source?.content_text }
            : { user_input: creativeInput.trim() || currentProject!.title, structure_book_id: getSelectedStructureBookId() || undefined }),
      })
    } catch (error) {
      if (valid()) setTaskList((previous) => ({ projectId: owner.projectId, ready: false,
        rows: previous?.projectId === owner.projectId ? previous.rows : [],
        error: `${error instanceof Error ? error.message : '提交未获确认。'} 请在请求提示中核对原请求；编辑稿已保留。` }))
    } finally { finishOperation(owner.token) }
  }

  const handleGenerateOutline = async (level = 'book') => {
    if (!currentProject || !['book', 'volume', 'chapter'].includes(level)) return
    if (level === 'volume') {
      if (!targetVolume || !Number.isSafeInteger(targetVolume.volume_idx) || targetVolume.volume_idx < 1) {
        setProjectError('请先在目录选择本卷章节，再生成对应卷纲候选。')
        return
      }
      await submitTask('outline_volume', null, targetVolume.volume_idx)
      return
    }
    if (level === 'book') { await submitTask('outline_book'); return }
    if (!selectedChapter || outlineReadinessLoading || !currentReadiness ||
      currentReadiness.missing_layers.some((layer) => layer === 'book' || layer === 'volume')) {
      alert(currentReadiness?.block_message || '请先选择章节，并核对全书大纲和本卷大纲。')
      return
    }
    const owner = beginOperation()
    if (!owner) return
    const chapterId = selectedChapter.id
    try {
      await apiFetch(`/api/projects/${owner.projectId}/chapters/${chapterId}/outline/expand`, { method: 'POST' })
      if (operation.current !== owner.token || projectRequest.current !== owner.epoch) return
      await refreshProjectData(owner.projectId, owner.epoch)
      if (operation.current === owner.token && projectRequest.current === owner.epoch) setReadinessRevision((value) => value + 1)
    } catch (error) {
      if (operation.current === owner.token && projectRequest.current === owner.epoch) {
        setProjectError(error instanceof Error ? error.message : '章节大纲生成失败，请重新读取大纲核对。')
      }
    } finally { finishOperation(owner.token) }
  }

  const handleGenerateChapter = async () => {
    if (!currentProject || !selectedChapter || !draftSession?.canGenerate()) {
      alert('请先读取保存稿，并保存或核对编辑区修改。')
      return
    }
    if (outlineReadinessLoading || !currentReadiness?.ready) {
      alert(currentReadiness?.block_message || '请先核对当前章节的大纲链路。')
      return
    }
    setTab('editor')
    await submitTask('chapter', selectedChapter.id)
  }

  const statusLabel: Record<string, string> = { draft: '草稿', generating: '生成中', completed: '完成', needs_review: '需复核' }
  const completedCount = chapters.filter((chapter) => chapter.status === 'completed').length
  const draftCount = chapters.filter((chapter) => chapter.status === 'draft').length
  const reviewCount = chapters.filter((chapter) => chapter.status === 'needs_review').length
  const chapterOutlineCount = chapters.filter((chapter) => Boolean(chapter.outline_json)).length
  const volumeOutlineCount = Object.keys(volumeOutlines).length
  const totalWords = chapters.reduce(
    (sum, chapter) => sum + Number(chapter.word_count || 0),
    0,
  )
  const mobileHubCards = [
    {
      label: '写作中枢',
      value: `${completedCount}/${chapters.length}`,
      icon: PenLine,
      onClick: () => {
        if (selectedChapter) setTab('editor')
        else setTab('list')
      },
    },
    {
      label: '审查中心',
      value: reviewCount > 0 ? `${reviewCount} 复核` : '待运行',
      icon: ShieldCheck,
      onClick: () => {
        setToolsTab('eval')
        setTab('tools')
      },
    },
    {
      label: '记忆中心',
      value: `${volumeOutlineCount + chapterOutlineCount} 层`,
      icon: Brain,
      onClick: () => setOpenOutlineKey('__all__'),
    },
    {
      label: '图谱洞察',
      value: '关系/伏笔',
      icon: Network,
      onClick: () => {
        setToolsTab('foreshadow')
        setTab('tools')
      },
    },
  ]

  return (
    <div className="flex flex-col h-screen pt-12 bg-gray-50">
      <div className="flex-1 overflow-y-auto">
        {projectError && <div className="m-4 space-y-2 rounded border border-red-200 bg-red-50 p-3 text-xs">
          <p role="alert">{projectError}</p>
          <button type="button" onClick={() => currentProject
            ? void refreshProjectData(currentProject.id, projectRequest.current)
            : setProjectReload((value) => value + 1)} className="text-blue-800">重新读取作品资料</button>
        </div>}
        {currentProject && <div className="mx-4 mt-3 space-y-2">
          <GenerationSubmissionNotice projectId={currentProject.id} />
          <div className="flex flex-wrap items-center gap-3 text-xs">
            {!tasksReady && !taskList?.error && <span role="status">正在核对后台任务…</span>}
            <button type="button" disabled={submittingProject === currentProject.id}
              onClick={() => void refreshTasks(currentProject.id, projectRequest.current)} className="text-blue-800 disabled:opacity-50">刷新后台任务</button>
            {tasks.length > 1 && <select aria-label="查看后台任务" value={currentTask?.taskId ?? ''} disabled={isGenerating}
              onChange={(event) => setPollingTask(tasks.find((task) => task.taskId === event.target.value) ?? null)}
              className="min-w-0 max-w-full rounded border bg-white p-1">
              {tasks.map((task) => <option key={task.taskId} value={task.taskId}>
                {taskTypeLabel(task.taskType)} · {chapters.find((chapter) => chapter.id === task.chapterId)?.title || '全书'} · {taskStatusLabel(task.status)}
              </option>)}
            </select>}
          </div>
          {taskList?.projectId === currentProject.id && taskList.error && <p role="alert" className="text-xs text-red-700">{taskList.error}</p>}
          {currentTask && <MobileTaskPanel task={currentTask} detail={taskProgress.detail} error={taskProgress.error}
            loading={taskProgress.loading} onRetry={taskProgress.retry}
            chapterTitle={chapters.find((chapter) => chapter.id === currentTask.chapterId)?.title ?? null}
            onReviewVolume={(candidateTaskId) => { setVolumeEditor({ projectId: currentProject.id, candidateTaskId }); setTab('outline') }}
            onOpenChapter={chapters.some((chapter) => chapter.id === currentTask.chapterId) ? () => {
              const chapter = chapters.find((item) => item.id === currentTask.chapterId)
              if (chapter) selectChapter(chapter)
            } : undefined} />}
        </div>}

        {tab === 'outline' && currentProject && volumeEditor?.projectId === currentProject.id && <div className="p-4">
          <VolumeOutlineEditor projectId={currentProject.id} volumeId={volumeEditor.volumeId} candidateTaskId={volumeEditor.candidateTaskId}
            onClose={() => setTab('list')} onSaved={() => {
              void refreshProjectData(currentProject.id, projectRequest.current)
              setReadinessRevision((value) => value + 1)
            }} />
        </div>}

        {/* 项目列表 / 章节列表 */}
        {tab === 'list' && (
          <div className="p-4" data-testid="mobile-outline-drawer">
            {!currentProject ? (
              <>
                <h2 className="text-lg font-bold text-gray-900 mb-3">我的项目</h2>
                {projects.map(p => (
                  <button key={p.id} onClick={() => router.push(`/workspace?id=${encodeURIComponent(p.id)}`)}
                    className="w-full text-left px-4 py-3 bg-white rounded-lg mb-2 border border-gray-200 active:bg-gray-50">
                    <div className="font-medium text-gray-900">{p.title}</div>
                    <div className="text-xs text-gray-500">{p.genre || '未分类'}</div>
                  </button>
                ))}
                {projects.length === 0 && <p className="text-sm text-gray-400 mb-4">暂无项目，点击下方创建</p>}
                <button onClick={() => setTab('create')}
                  className="w-full py-2.5 bg-blue-600 text-white rounded-lg text-sm font-medium">
                  + 新建项目
                </button>
              </>
            ) : (
              <>
                <div className="flex items-center justify-between mb-3">
                  <h2 className="text-lg font-bold text-gray-900 truncate">{currentProject.title}</h2>
                  <div className="flex items-center gap-2 shrink-0 ml-2">
                    <button
                      type="button"
                      onClick={() => setOpenOutlineKey((prev) => (prev === '__all__' ? null : '__all__'))}
                      className="text-xs text-emerald-700"
                    >
                      {openOutlineKey === '__all__' ? '收起大纲' : '展开大纲'}
                    </button>
                    <button onClick={() => { preserveChapterDrafts(); router.push('/') }}
                      className="text-xs text-blue-600">返回项目列表</button>
                  </div>
                </div>

                <div className="mb-3 rounded-lg border border-gray-200 bg-white p-3">
                  <div className="mb-2 flex items-center justify-between">
                    <div>
                      <div className="text-sm font-semibold text-gray-900">草稿闸门</div>
                      <div className="text-xs text-gray-500">
                        {totalWords > 0 ? `${(totalWords / 1000).toFixed(1)}k 字` : '尚无正文'}
                        {' · '}
                        {draftCount} 草稿 / {reviewCount} 复核
                      </div>
                    </div>
                    <FileCheck2 className="h-4 w-4 text-gray-500" />
                  </div>
                  <div className="grid grid-cols-2 gap-2">
                    {mobileHubCards.map((item) => {
                      const Icon = item.icon
                      return (
                        <button
                          key={item.label}
                          type="button"
                          onClick={item.onClick}
                          className="rounded-md border border-gray-200 bg-gray-50 px-2.5 py-2 text-left active:bg-gray-100"
                        >
                          <span className="flex items-center gap-1.5 text-xs font-semibold text-gray-800">
                            <Icon className="h-3.5 w-3.5 text-gray-500" />
                            {item.label}
                          </span>
                          <span className="mt-0.5 block text-[11px] text-gray-500">{item.value}</span>
                        </button>
                      )
                    })}
                  </div>
                </div>

                {/* Current saved outline; task candidates are displayed above. */}
                {volumes.length === 0 && savedOutline ? (
                  <div className="space-y-3">
                    <h3 className="text-sm font-semibold text-gray-700">全书大纲（保存稿）</h3>
                    <pre className="whitespace-pre-wrap text-sm text-gray-700 bg-gray-50 p-3 rounded-lg border max-h-[60vh] overflow-y-auto leading-relaxed">
                      {savedOutline}
                    </pre>
                  </div>
                ) : volumes.length === 0 ? (
                  <div className="space-y-3">
                    <p className="text-sm text-gray-500">暂无大纲，输入你的小说创意：</p>
                    <textarea value={creativeInput} onChange={e => setCreativeInput(e.target.value)}
                      placeholder="例如：都市修仙，主角是外卖员意外获得修炼功法..."
                      className="w-full h-28 p-3 border border-gray-300 rounded-lg text-sm resize-none" />
                    <button onClick={() => handleGenerateOutline('book')} disabled={isGenerating || submissionBlocked || !tasksReady || !creativeInput.trim()}
                      className="w-full py-2.5 bg-blue-600 text-white rounded-lg text-sm disabled:opacity-50">
                      {isGenerating ? '后台处理中...' : '生成全书大纲'}
                    </button>
                  </div>
                ) : (
                  <div className="space-y-3">
                    {bookOutlineData && (
                      <MobileOutlineDisclosure
                        title="全书大纲"
                        subtitle="整本书的主线、设定、人物和分卷规划"
                        outlineKey="book"
                        openOutlineKey={openOutlineKey}
                        onToggle={toggleOutline}
                      >
                        {formatOutline(bookOutlineData, 'book')}
                      </MobileOutlineDisclosure>
                    )}
                    {[...volumes].sort((a, b) => a.volume_idx - b.volume_idx).map(v => (
                      <div key={v.id} className="space-y-2">
                        <div className="flex items-center justify-between px-1">
                          <h3 className="text-sm font-semibold text-gray-700 truncate">{v.title}</h3>
                          <span className="text-xs text-gray-400 shrink-0 ml-2">
                            {chapters.filter(c => (c.volume_id ?? c.volumeId) === v.id).length} 章
                          </span>
                        </div>
                        <button type="button" onClick={() => { setVolumeEditor({ projectId: currentProject.id, volumeId: v.id }); setTab('outline') }}
                          className="min-h-10 px-1 text-sm text-blue-800">核对与编辑本卷大纲</button>
                        {volumeOutlines[v.id] && (
                          <MobileOutlineDisclosure
                            title="本卷大纲"
                            subtitle={`第 ${v.volume_idx} 卷剧情承载、线索推进和卷尾钩子`}
                            outlineKey={`volume:${v.id}`}
                            openOutlineKey={openOutlineKey}
                            onToggle={toggleOutline}
                          >
                            {formatOutline(volumeOutlines[v.id], 'volume')}
                          </MobileOutlineDisclosure>
                        )}
                        {chapters
                          .filter(c => (c.volume_id ?? c.volumeId) === v.id)
                          .sort((a, b) => a.chapter_idx - b.chapter_idx)
                          .map(ch => {
                            const chapterOutline = ch.outline_json || null
                            return (
                              <div key={ch.id} className="bg-white rounded-lg border border-gray-100 overflow-hidden">
                                <div className="flex items-center">
                                  <button onClick={() => selectChapter(ch)}
                                    className="min-w-0 flex-1 text-left px-3 py-2.5 flex justify-between items-center active:bg-gray-50">
                                    <span className="text-sm text-gray-800 truncate">{ch.title}</span>
                                    <span className="flex items-center gap-1.5 shrink-0 ml-2">
                                      {ch.word_count > 0 && <span className="text-xs text-gray-400">{(ch.word_count/1000).toFixed(1)}k</span>}
                                      <span className={`text-[10px] px-1.5 py-0.5 rounded ${
                                        ch.status === 'completed' ? 'bg-green-100 text-green-700' :
                                        ch.status === 'generating' ? 'bg-yellow-100 text-yellow-700' :
                                        'bg-gray-100 text-gray-500'
                                      }`}>{statusLabel[ch.status] || ch.status}</span>
                                    </span>
                                  </button>
                                  {chapterOutline && (
                                    <button
                                      type="button"
                                      onClick={() => toggleOutline(`chapter:${ch.id}`)}
                                      className="px-3 py-2.5 text-xs text-amber-700 border-l border-gray-100 shrink-0 active:bg-amber-50"
                                    >
                                      章节大纲
                                    </button>
                                  )}
                                </div>
                                {chapterOutline && (openOutlineKey === '__all__' || openOutlineKey === `chapter:${ch.id}`) && (
                                  <pre className="whitespace-pre-wrap text-sm text-gray-700 bg-amber-50/60 border-t border-amber-100 p-3 max-h-[46vh] overflow-y-auto leading-relaxed">
                                    {formatOutline(chapterOutline, 'chapter')}
                                  </pre>
                                )}
                              </div>
                            )
                          })}
                      </div>
                    ))}
                  </div>
                )}
              </>
            )}
          </div>
        )}

        {/* 编辑器 */}
        {tab === 'editor' && selectedChapter && (
          <div className="p-4">
            <div className="flex items-center justify-between mb-3">
              <div className="flex items-center gap-2 min-w-0">
                <button
                  type="button"
                  onClick={() => setTab('list')}
                  aria-label="open outline"
                  data-testid="mobile-outline-toggle"
                  className="shrink-0 p-1.5 rounded hover:bg-gray-100 text-gray-700 border border-gray-200"
                >
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="18" x2="21" y2="18"/></svg>
                </button>
                <h2 className="text-base font-bold text-gray-900 truncate">{selectedChapter.title}</h2>
              </div>
              <button onClick={handleGenerateChapter} disabled={!canGenerateProse}
                className="px-3 py-1.5 bg-green-600 text-white rounded text-xs disabled:opacity-50 shrink-0 ml-2">
                {isGenerating ? '生成中...' : 'AI 生成'}
              </button>
            </div>
            {draftSession && draftState && <ChapterSaveStatus key={`${draftState.projectId}:${draftState.chapterId}`} session={draftSession} state={draftState} />}
            <textarea value={editorContent} onChange={event => draftSession?.edit(event.target.value)}
              placeholder="章节内容将在这里显示..." aria-label="章节编辑稿" readOnly={!draftSession?.canEdit()}
              className="w-full h-[60vh] p-3 border border-gray-200 rounded-lg text-sm leading-relaxed resize-none" />
            {editorContent && <p className="text-xs text-gray-400 mt-1 text-right">{[...editorContent].length} 字</p>}
          </div>
        )}

        {/* 工具面板（按需加载） */}
        {tab === 'tools' && (
          <div className="p-4">
            <div className="flex gap-1 mb-3 overflow-x-auto">
              {([
                { key: 'generate' as const, label: '生成' },
                { key: 'arc' as const, label: '弧进度' },
                { key: 'guide' as const, label: '写作指南' },
                { key: 'foreshadow' as const, label: '伏笔' },
                { key: 'settings' as const, label: '设定' },
                { key: 'cognition' as const, label: '人物认知' },
                { key: 'eval' as const, label: '评估' },
              ]).map(t => (
                <button key={t.key} onClick={() => setToolsTab(t.key)}
                  className={`px-3 py-1.5 text-xs rounded-full whitespace-nowrap ${
                    toolsTab === t.key ? 'bg-blue-600 text-white' : 'bg-gray-200 text-gray-600'
                  }`}>
                  {t.label}
                </button>
              ))}
            </div>

            {toolsTab === 'generate' && (
              <GeneratePanel
                key={`${currentProject?.id}:${selectedChapter?.id ?? ''}`}
                busy={isGenerating || submissionBlocked || !tasksReady}
                proseDisabled={!draftSession?.canGenerate()}
                volumeDisabled={!targetVolume}
                projectId={currentProject?.id}
                selectedChapterId={selectedChapter?.id}
                outlineReadiness={currentReadiness}
                outlineReadinessLoading={outlineReadinessLoading}
                onGenerate={handleGenerateChapter}
                onGenerateOutline={handleGenerateOutline}
                onViewOutline={() => { setOpenOutlineKey('__all__'); setTab('list') }}
              />
            )}
            {toolsTab === 'guide' && <WritingGuidePanel projectId={urlProjectId} />}
            {toolsTab === 'foreshadow' && currentProject && <ForeshadowPanel projectId={currentProject.id} chapterId={selectedChapter?.id} editorContent={editorContent} />}
            {toolsTab === 'settings' && currentProject && <SettingsPanel projectId={currentProject.id} />}
            {toolsTab === 'cognition' && currentProject && <CognitionPanel projectId={currentProject.id} />}
            {toolsTab === 'arc' && currentProject && <ArcProgressPanel projectId={currentProject.id} volumes={volumes}
              onCreated={() => refreshProjectData(currentProject.id, projectRequest.current)}
              currentVolumeId={targetVolume?.id} onOpenChapter={chapterId => {
                const chapter = chapters.find(item => item.id === chapterId)
                if (chapter) { setSelection({ projectId: currentProject.id, chapter }); setTab('editor') }
              }} />}
            {toolsTab === 'eval' && selectedChapter && <EvaluationPanel chapterId={selectedChapter.id} />}
          </div>
        )}

        {/* 新建项目 */}
        {tab === 'create' && (
          <div className="p-4 space-y-3">
            <h2 className="text-lg font-bold text-gray-900">新建项目</h2>
            <input value={newTitle} onChange={e => setNewTitle(e.target.value)} placeholder="小说标题"
              className="w-full px-3 py-2.5 border border-gray-300 rounded-lg text-sm" />
            <select value={newGenre} onChange={e => setNewGenre(e.target.value)}
              className="w-full px-3 py-2.5 border border-gray-300 rounded-lg text-sm">
              <option value="">选择题材</option>
              {['玄幻','仙侠','都市','言情','悬疑','科幻','历史','末世','其他'].map(g => (
                <option key={g} value={g}>{g}</option>
              ))}
            </select>
            <div className="flex gap-2">
              <button onClick={handleCreateProject} disabled={!newTitle.trim()}
                className="flex-1 py-2.5 bg-blue-600 text-white rounded-lg text-sm disabled:opacity-50">创建</button>
              <button onClick={() => setTab('list')}
                className="flex-1 py-2.5 bg-gray-200 text-gray-700 rounded-lg text-sm">取消</button>
            </div>
          </div>
        )}
      </div>

      {/* 底部导航栏 */}
      <div className="flex border-t border-gray-200 bg-white">
        {([
          { key: 'list' as const, label: '目录', icon: '📁' },
          { key: 'editor' as const, label: '编辑', icon: '✏️' },
          { key: 'tools' as const, label: '工具', icon: '🛠' },
        ]).map(t => (
          <button key={t.key}
            onClick={() => { if (t.key === 'editor' && !selectedChapter) return; setTab(t.key) }}
            className={`flex-1 py-2.5 text-center text-xs font-medium ${
              tab === t.key ? 'text-blue-600 bg-blue-50' : 'text-gray-500'
            } ${t.key === 'editor' && !selectedChapter ? 'opacity-40' : ''}`}>
            <div className="text-base mb-0.5">{t.icon}</div>{t.label}
          </button>
        ))}
      </div>
    </div>
  )
}
