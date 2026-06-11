'use client'

import { useState, useEffect, useCallback } from 'react'
import type { ReactNode } from 'react'
import dynamic from 'next/dynamic'
import { useRouter, useSearchParams } from 'next/navigation'
import { Brain, FileCheck2, Network, PenLine, ShieldCheck } from 'lucide-react'
import { apiFetch } from '@/lib/api'
import { usePolling } from '@/lib/usePolling'
import { getSelectedStructureBookId } from '@/components/panels/GeneratePanel'

// Lazy load panels — only when user opens the tools tab
const GeneratePanel = dynamic(() => import('@/components/panels/GeneratePanel').then(m => ({ default: m.GeneratePanel })), { ssr: false })
const ForeshadowPanel = dynamic(() => import('@/components/panels/ForeshadowPanel').then(m => ({ default: m.ForeshadowPanel })), { ssr: false })
const EvaluationPanel = dynamic(() => import('@/components/panels/EvaluationPanel').then(m => ({ default: m.EvaluationPanel })), { ssr: false })
const WritingGuidePanel = dynamic(() => import('@/components/panels/WritingGuidePanel').then(m => ({ default: m.WritingGuidePanel })), { ssr: false })
const SettingsPanel = dynamic(() => import('@/components/panels/SettingsPanel').then(m => ({ default: m.SettingsPanel })), { ssr: false })

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

interface Outline { id: string; level: string; content_json: Record<string, unknown>; is_confirmed?: number | boolean }
interface OutlineReadinessLayer { ready: boolean; detail?: string | null }
interface OutlineReadinessInfo {
  ready: boolean
  missing_layers: string[]
  block_message?: string | null
  layers: Record<'book' | 'volume' | 'chapter', OutlineReadinessLayer>
}

interface AsyncTaskSummary {
  task_id: string
  status: 'pending' | 'running' | 'polishing' | 'completed' | 'failed' | string
  task_type?: string | null
}

interface AsyncTaskDetail extends AsyncTaskSummary {
  progress_text?: string | null
  result_text?: string | null
  polished_text?: string | null
  error_message?: string | null
}

// A backend async task currently being polled. `kind` selects which UI
// updates apply (mirrors the previous per-handler polling bodies).
interface PollingTask {
  taskId: string
  kind: 'resume' | 'outline' | 'chapter'
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
  } else if (kind === 'volume') {
    add('本卷摘要', data.summary || data.raw_text)
    add('核心冲突', data.core_conflict)
    add('主线推进', data.main_progress)
    add('人物变化', data.side_progress)
    add('伏笔状态', data.foreshadow_state)
    add('关键场景', data.key_scene)
    add('卷尾钩子', data.hook)
    add('章节规划', data.chapter_summaries)
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
  const [currentProject, setCurrentProject] = useState<Project | null>(null)
  const [volumes, setVolumes] = useState<Volume[]>([])
  const [savedOutline, setSavedOutline] = useState('')
  const [polishedOutline, setPolishedOutline] = useState('')
  const [outlineVersion, setOutlineVersion] = useState<'raw' | 'polished'>('polished')
  const [bookOutlineData, setBookOutlineData] = useState<Record<string, unknown> | null>(null)
  const [volumeOutlines, setVolumeOutlines] = useState<Record<number, Record<string, unknown>>>({})
  const [openOutlineKey, setOpenOutlineKey] = useState<string | null>('__all__')
  const [chapters, setChapters] = useState<Chapter[]>([])
  const [selectedChapter, setSelectedChapter] = useState<Chapter | null>(null)
  const [editorContent, setEditorContent] = useState('')
  const [isGenerating, setIsGenerating] = useState(false)
  const [tab, setTab] = useState<'list' | 'editor' | 'tools' | 'create'>('list')
  const [creativeInput, setCreativeInput] = useState('')
  const [outlinePreview, setOutlinePreview] = useState('')
  const [outlineReadiness, setOutlineReadiness] = useState<OutlineReadinessInfo | null>(null)
  const [outlineReadinessLoading, setOutlineReadinessLoading] = useState(false)
  const [newTitle, setNewTitle] = useState('')
  const [newGenre, setNewGenre] = useState('')
  const [toolsTab, setToolsTab] = useState<'generate' | 'guide' | 'foreshadow' | 'settings' | 'eval'>('generate')

  const toggleOutline = useCallback((key: string) => {
    setOpenOutlineKey((prev) => (prev === key ? null : key))
  }, [])

  useEffect(() => {
    apiFetch<{ projects: Project[] }>('/api/projects')
      .then(d => setProjects(d.projects))
      .catch(() => {})
  }, [])

  // Auto-load project specified in URL ?id=
  useEffect(() => {
    if (!urlProjectId) {
      router.replace('/')
      return
    }
    if (currentProject?.id === urlProjectId) return
    const target = projects.find((p) => p.id === urlProjectId)
    if (target) {
      loadProject(target)
    } else if (projects.length > 0) {
      // not found among loaded list; fetch directly
      apiFetch<Project>(`/api/projects/${urlProjectId}`)
        .then((p) => loadProject(p))
        .catch(() => router.replace('/'))
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [urlProjectId, projects, currentProject?.id])

  const loadProject = useCallback(async (p: Project) => {
    setCurrentProject(p)
    setSelectedChapter(null)
    setSavedOutline('')
    setPolishedOutline('')
    setBookOutlineData(null)
    setVolumeOutlines({})
    setOpenOutlineKey('__all__')
    setTab('list')
    try {
      const vols = await apiFetch<Volume[]>(`/api/projects/${p.id}/volumes`)
      setVolumes(vols)
      if (vols.length > 0) {
        const chs = await apiFetch<Chapter[]>(`/api/projects/${p.id}/chapters?lightweight=true`)
        setChapters(chs)
      } else {
        setChapters([])
      }
      // Load saved outlines
      const outlines = await apiFetch<Outline[]>(`/api/projects/${p.id}/outlines`)
      const bookOutlines = outlines
        .filter(o => o.level === 'book')
        .filter(o => !('volume_idx' in ((o.content_json || {}) as Record<string, unknown>)))
        .sort((a, b) => (a.id < b.id ? -1 : 1))
      const bookOutline = bookOutlines.find(o => Boolean(o.is_confirmed)) || bookOutlines[0]
      if (bookOutline) {
        const raw = formatOutline(bookOutline.content_json, 'book').replace(/<volume-plan>[\s\S]+?<\/volume-plan>\s*/g, '')
        setBookOutlineData(bookOutline.content_json)
        setSavedOutline(raw)
      }
      const nextVolumeOutlines: Record<number, Record<string, unknown>> = {}
      for (const outline of outlines) {
        if (outline.level !== 'volume') continue
        const idx = Number((outline.content_json || {}).volume_idx || 0)
        if (idx > 0) nextVolumeOutlines[idx] = outline.content_json
      }
      setVolumeOutlines(nextVolumeOutlines)
      // Check for running generation tasks
      try {
        const tasks = await apiFetch<AsyncTaskSummary[]>(`/api/generate/async/project/${p.id}`)
        // Load polished text from completed task
        const completed = tasks.find((t) => t.status === 'completed' && t.task_type?.startsWith('outline'))
        if (completed) {
          const full = await apiFetch<AsyncTaskDetail>(`/api/generate/async/${completed.task_id}`)
          if (full.polished_text) setPolishedOutline(full.polished_text)
          if (full.result_text) setSavedOutline(prev => prev || full.result_text || '')
        }
        const running = tasks.find((t) => t.status === 'pending' || t.status === 'running' || t.status === 'polishing')
        if (running) {
          setIsGenerating(true)
          // Resume polling (handled by the usePolling hook below)
          setPollingTask({ taskId: running.task_id, kind: 'resume' })
        }
      } catch { /* */ }
    } catch { /* */ }
  }, [])

  const selectChapter = useCallback(async (ch: Chapter) => {
    setSelectedChapter(ch)
    setTab('editor')
    try {
      const full = await apiFetch<Chapter>(
        `/api/projects/${currentProject!.id}/chapters/${ch.id}`
      )
      setEditorContent(full.content_text || '')
    } catch { /* */ }
  }, [currentProject])

  useEffect(() => {
    if (!currentProject?.id || !selectedChapter?.id) {
      setOutlineReadiness(null)
      return
    }
    setOutlineReadinessLoading(true)
    apiFetch<OutlineReadinessInfo>(
      `/api/projects/${currentProject.id}/outline-readiness?chapter_id=${selectedChapter.id}`,
    )
      .then(setOutlineReadiness)
      .catch(() => setOutlineReadiness(null))
      .finally(() => setOutlineReadinessLoading(false))
  }, [currentProject?.id, selectedChapter?.id])

  const handleCreateProject = async () => {
    if (!newTitle.trim()) return
    try {
      const p = await apiFetch<Project>('/api/projects', {
        method: 'POST',
        body: JSON.stringify({ title: newTitle, genre: newGenre }),
      })
      setProjects(prev => [...prev, p])
      setCurrentProject(p)
      setNewTitle('')
      setNewGenre('')
      setSavedOutline('')
      setPolishedOutline('')
      setBookOutlineData(null)
      setVolumeOutlines({})
      setOpenOutlineKey(null)
      setOutlinePreview('')
      setVolumes([])
      setChapters([])
      setTab('list')
    } catch { /* */ }
  }

  // Unified async-task polling: started by event handlers via setPollingTask,
  // cleaned up automatically on unmount / terminal task states.
  const [pollingTask, setPollingTask] = useState<PollingTask | null>(null)

  usePolling(async (stop) => {
    if (!pollingTask) return
    const { taskId, kind } = pollingTask
    const status = await apiFetch<AsyncTaskDetail>(`/api/generate/async/${taskId}`)
    const finish = () => {
      stop()
      setPollingTask(null)
      setIsGenerating(false)
    }
    if (kind === 'resume') {
      if (status.task_type?.startsWith('outline')) {
        setOutlinePreview(status.progress_text || '')
      } else {
        setEditorContent(status.progress_text || '')
      }
      if (status.status === 'completed') {
        finish()
        if (status.task_type?.startsWith('outline')) {
          setSavedOutline(status.result_text || '')
          setPolishedOutline(status.polished_text || '')
          setOutlinePreview('')
        }
      } else if (status.status === 'failed') {
        finish()
      }
    } else if (kind === 'outline') {
      setOutlinePreview(status.progress_text || '')
      if (status.status === 'polishing') {
        setOutlinePreview(status.progress_text || status.result_text || '')
      } else if (status.status === 'completed') {
        finish()
        setSavedOutline(status.result_text || '')
        setPolishedOutline(status.polished_text || '')
        setOutlinePreview('')
      } else if (status.status === 'failed') {
        finish()
        alert(`生成失败: ${status.error_message || '未知错误'}`)
      }
    } else {
      setEditorContent(status.progress_text || '')
      if (status.status === 'completed') {
        finish()
        setEditorContent(status.result_text || '')
      } else if (status.status === 'failed') {
        finish()
        alert(`生成失败: ${status.error_message || '未知错误'}`)
      }
    }
  }, 3000, pollingTask !== null)

  const handleGenerateOutline = async (level?: string) => {
    if (!currentProject) { alert('请先选择一个项目'); return }
    if (isGenerating) { alert('正在生成中，请稍候'); return }
    if (level === 'chapter') {
      if (!selectedChapter) { alert('请先在目录中选择一个章节'); return }
      if (outlineReadinessLoading) { alert('正在检查大纲链路，请稍候'); return }
      const missingUpstream = (outlineReadiness?.missing_layers || []).filter(
        (layer) => layer === 'book' || layer === 'volume',
      )
      if (missingUpstream.length > 0) {
        alert(outlineReadiness?.block_message || '请先补齐全书大纲和当前卷大纲。')
        return
      }
      setIsGenerating(true)
      setTab('editor')
      try {
        await apiFetch<{ outline_json?: Record<string, unknown> }>(
          `/api/projects/${currentProject.id}/chapters/${selectedChapter.id}/outline/expand`,
          { method: 'POST' },
        )
        const report = await apiFetch<OutlineReadinessInfo>(
          `/api/projects/${currentProject.id}/outline-readiness?chapter_id=${selectedChapter.id}`,
        )
        setOutlineReadiness(report)
      } catch (e) {
        alert(e instanceof Error ? e.message : '章节大纲生成失败')
      } finally {
        setIsGenerating(false)
      }
      return
    }
    const taskType = `outline_${level || 'book'}`
    const input = creativeInput.trim() || currentProject.title
    setIsGenerating(true)
    setOutlinePreview('')
    setSavedOutline('')
    setTab('list')
    try {
      const data = await apiFetch<{ task_id: string }>('/api/generate/async', {
        method: 'POST',
        body: JSON.stringify({
          project_id: currentProject.id, task_type: taskType, user_input: input,
          structure_book_id: getSelectedStructureBookId() || undefined,
        }),
      })
      // Start polling (handled by the usePolling hook above)
      setPollingTask({ taskId: data.task_id, kind: 'outline' })
    } catch (e) {
      setIsGenerating(false)
      alert(e instanceof Error ? e.message : '提交生成任务失败')
    }
  }

  const handleGenerateChapter = async () => {
    if (!currentProject) { alert('请先选择一个项目'); return }
    if (!selectedChapter) { alert('请先在目录中选择一个章节'); return }
    if (isGenerating) { alert('正在生成中，请稍候'); return }
    if (outlineReadinessLoading) { alert('正在检查大纲链路，请稍候'); return }
    if (!outlineReadiness?.ready) {
      alert(outlineReadiness?.block_message || '大纲链路未完成，不能生成正文。')
      return
    }
    setIsGenerating(true)
    setEditorContent('')
    setTab('editor')
    try {
      const data = await apiFetch<{ task_id: string }>('/api/generate/async', {
        method: 'POST',
        body: JSON.stringify({ project_id: currentProject.id, task_type: 'chapter', chapter_id: selectedChapter.id }),
      })
      // Start polling (handled by the usePolling hook above)
      setPollingTask({ taskId: data.task_id, kind: 'chapter' })
    } catch (e) {
      setIsGenerating(false)
      alert(e instanceof Error ? e.message : '提交生成任务失败')
    }
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

        {/* 项目列表 / 章节列表 */}
        {tab === 'list' && (
          <div className="p-4" data-testid="mobile-outline-drawer">
            {!currentProject ? (
              <>
                <h2 className="text-lg font-bold text-gray-900 mb-3">我的项目</h2>
                {projects.map(p => (
                  <button key={p.id} onClick={() => loadProject(p)}
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
                    <button onClick={() => router.push('/')}
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

                {/* Generation progress */}
                {isGenerating && (
                  <div className="space-y-2 mb-3">
                    <div className="flex items-center gap-2">
                      <div className="w-2 h-2 bg-blue-500 rounded-full animate-pulse" />
                      <span className="text-sm font-semibold text-blue-600">后台生成中...</span>
                      {outlinePreview && <span className="text-xs text-gray-400">{outlinePreview.length} 字</span>}
                    </div>
                    {outlinePreview ? (
                      <pre className="whitespace-pre-wrap text-sm text-gray-700 bg-blue-50 p-3 rounded-lg border border-blue-100 max-h-[50vh] overflow-y-auto leading-relaxed">
                        {outlinePreview}
                      </pre>
                    ) : (
                      <div className="bg-blue-50 rounded-lg p-4 text-center">
                        <div className="w-8 h-8 border-2 border-blue-300 border-t-blue-600 rounded-full animate-spin mx-auto mb-2" />
                        <p className="text-sm text-blue-600">{outlinePreview ? '润色去AI中...' : '大纲生成中，请稍候...'}</p>
                        <p className="text-xs text-gray-400 mt-1">后台处理中，可离开此页面稍后回来查看</p>
                      </div>
                    )}
                  </div>
                )}

                {/* Saved outline */}
                {!isGenerating && volumes.length === 0 && (savedOutline || outlinePreview) ? (
                  <div className="space-y-3">
                    <div className="flex items-center justify-between">
                      <h3 className="text-sm font-semibold text-gray-700">全书大纲</h3>
                      {polishedOutline && savedOutline && (
                        <div className="flex gap-1">
                          <button onClick={() => setOutlineVersion('polished')}
                            className={`px-2 py-1 text-xs rounded ${outlineVersion === 'polished' ? 'bg-blue-600 text-white' : 'bg-gray-200 text-gray-600'}`}>
                            润色版
                          </button>
                          <button onClick={() => setOutlineVersion('raw')}
                            className={`px-2 py-1 text-xs rounded ${outlineVersion === 'raw' ? 'bg-blue-600 text-white' : 'bg-gray-200 text-gray-600'}`}>
                            原始版
                          </button>
                        </div>
                      )}
                    </div>
                    <pre className="whitespace-pre-wrap text-sm text-gray-700 bg-gray-50 p-3 rounded-lg border max-h-[60vh] overflow-y-auto leading-relaxed">
                      {outlineVersion === 'polished' && polishedOutline ? polishedOutline : (savedOutline || outlinePreview)}
                    </pre>
                  </div>
                ) : !isGenerating && volumes.length === 0 ? (
                  <div className="space-y-3">
                    <p className="text-sm text-gray-500">暂无大纲，输入你的小说创意：</p>
                    <textarea value={creativeInput} onChange={e => setCreativeInput(e.target.value)}
                      placeholder="例如：都市修仙，主角是外卖员意外获得修炼功法..."
                      className="w-full h-28 p-3 border border-gray-300 rounded-lg text-sm resize-none" />
                    <button onClick={() => handleGenerateOutline('book')} disabled={isGenerating || !creativeInput.trim()}
                      className="w-full py-2.5 bg-blue-600 text-white rounded-lg text-sm disabled:opacity-50">
                      {isGenerating ? '大纲生成中...' : '生成全书大纲'}
                    </button>
                    {outlinePreview && (
                      <pre className="p-3 bg-white rounded-lg text-xs text-gray-700 whitespace-pre-wrap border max-h-60 overflow-y-auto">
                        {outlinePreview}
                      </pre>
                    )}
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
                    {volumes.sort((a, b) => a.volume_idx - b.volume_idx).map(v => (
                      <div key={v.id} className="space-y-2">
                        <div className="flex items-center justify-between px-1">
                          <h3 className="text-sm font-semibold text-gray-700 truncate">{v.title}</h3>
                          <span className="text-xs text-gray-400 shrink-0 ml-2">
                            {chapters.filter(c => (c.volume_id ?? c.volumeId) === v.id).length} 章
                          </span>
                        </div>
                        {volumeOutlines[v.volume_idx] && (
                          <MobileOutlineDisclosure
                            title="本卷大纲"
                            subtitle={`第 ${v.volume_idx} 卷剧情承载、线索推进和卷尾钩子`}
                            outlineKey={`volume:${v.id}`}
                            openOutlineKey={openOutlineKey}
                            onToggle={toggleOutline}
                          >
                            {formatOutline(volumeOutlines[v.volume_idx], 'volume')}
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
              <button onClick={handleGenerateChapter} disabled={isGenerating || outlineReadinessLoading || !outlineReadiness?.ready}
                className="px-3 py-1.5 bg-green-600 text-white rounded text-xs disabled:opacity-50 shrink-0 ml-2">
                {isGenerating ? '生成中...' : 'AI 生成'}
              </button>
            </div>
            <textarea value={editorContent} onChange={e => setEditorContent(e.target.value)}
              placeholder="章节内容将在这里显示..." readOnly={isGenerating}
              className="w-full h-[60vh] p-3 border border-gray-200 rounded-lg text-sm leading-relaxed resize-none" />
            {editorContent && (
              <p className="text-xs text-gray-400 mt-1 text-right">{editorContent.length} 字</p>
            )}
          </div>
        )}

        {/* 工具面板（按需加载） */}
        {tab === 'tools' && (
          <div className="p-4">
            <div className="flex gap-1 mb-3 overflow-x-auto">
              {([
                { key: 'generate' as const, label: '生成' },
                { key: 'guide' as const, label: '写作指南' },
                { key: 'foreshadow' as const, label: '伏笔' },
                { key: 'settings' as const, label: '设定' },
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
                projectId={currentProject?.id}
                selectedChapterId={selectedChapter?.id}
                outlineReadiness={outlineReadiness}
                outlineReadinessLoading={outlineReadinessLoading}
                onGenerate={handleGenerateChapter}
                onGenerateOutline={handleGenerateOutline}
              />
            )}
            {toolsTab === 'guide' && <WritingGuidePanel projectId={urlProjectId} />}
            {toolsTab === 'foreshadow' && currentProject && <ForeshadowPanel projectId={currentProject.id} />}
            {toolsTab === 'settings' && currentProject && <SettingsPanel projectId={currentProject.id} />}
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
