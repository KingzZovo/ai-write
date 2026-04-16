'use client'

import { useState, useCallback, useEffect, useRef, lazy, Suspense } from 'react'
import dynamic from 'next/dynamic'
import { WorkspaceLayout } from '@/components/workspace/WorkspaceLayout'
import { OutlineTree } from '@/components/outline/OutlineTree'
import { GeneratePanel } from '@/components/panels/GeneratePanel'

// Lazy load heavy panels — only loaded when their CollapsibleSection is opened
const ForeshadowPanel = dynamic(() => import('@/components/panels/ForeshadowPanel').then(m => ({ default: m.ForeshadowPanel })), { ssr: false })
const SettingsPanel = dynamic(() => import('@/components/panels/SettingsPanel').then(m => ({ default: m.SettingsPanel })), { ssr: false })
const EvaluationPanel = dynamic(() => import('@/components/panels/EvaluationPanel').then(m => ({ default: m.EvaluationPanel })), { ssr: false })
const CheckerDashboard = dynamic(() => import('@/components/panels/CheckerDashboard').then(m => ({ default: m.CheckerDashboard })), { ssr: false })
const StrandPanel = dynamic(() => import('@/components/panels/StrandPanel').then(m => ({ default: m.StrandPanel })), { ssr: false })
const WritingGuidePanel = dynamic(() => import('@/components/panels/WritingGuidePanel').then(m => ({ default: m.WritingGuidePanel })), { ssr: false })
const AntiAIPanel = dynamic(() => import('@/components/panels/AntiAIPanel').then(m => ({ default: m.AntiAIPanel })), { ssr: false })
const VersionPanel = dynamic(() => import('@/components/panels/VersionPanel').then(m => ({ default: m.VersionPanel })), { ssr: false })
const TokenDashboard = dynamic(() => import('@/components/panels/TokenDashboard').then(m => ({ default: m.TokenDashboard })), { ssr: false })
const RelationshipGraph = dynamic(() => import('@/components/panels/RelationshipGraph').then(m => ({ default: m.RelationshipGraph })), { ssr: false })
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

interface ProjectListRes {
  projects: Project[]
  total: number
}

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

// ----------------------------------------------------------------
// Genres
// ----------------------------------------------------------------

const GENRES = ['玄幻', '仙侠', '都市', '言情', '悬疑', '科幻', '历史', '其他'] as const

// ----------------------------------------------------------------
// CollapsibleSection (unchanged from original)
// ----------------------------------------------------------------

function CollapsibleSection({
  title,
  defaultOpen = false,
  children,
}: {
  title: string
  defaultOpen?: boolean
  children: React.ReactNode
}) {
  const [open, setOpen] = useState(defaultOpen)

  return (
    <div className="border-b border-gray-200">
      <button
        onClick={() => setOpen(!open)}
        className="w-full flex items-center justify-between px-4 py-2.5 text-xs font-semibold text-gray-500 uppercase tracking-wide hover:bg-gray-50 transition-colors"
      >
        <span>{title}</span>
        <svg
          className={`w-3.5 h-3.5 text-gray-400 transition-transform ${
            open ? 'rotate-180' : ''
          }`}
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
          strokeWidth={2}
        >
          <path strokeLinecap="round" strokeLinejoin="round" d="M19 9l-7 7-7-7" />
        </svg>
      </button>
      {open && <div className="pb-3">{children}</div>}
    </div>
  )
}

// ================================================================
// WorkspacePage
// ================================================================

export default function DesktopWorkspace() {
  const {
    projects,
    projectsLoaded,
    setProjects,
    setProjectsLoaded,
    currentProject,
    selectedChapterId,
    setCurrentProject,
    setVolumes,
    setChapters,
    addChapters,
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
  const [activeView, setActiveView] = useState<'editor' | 'outline' | 'creative' | 'wizard'>(
    'creative'
  )

  // New project modal
  const [showNewProject, setShowNewProject] = useState(false)
  const [newTitle, setNewTitle] = useState('')
  const [newGenre, setNewGenre] = useState<string>(GENRES[0])
  const [newPremise, setNewPremise] = useState('')
  const [creating, setCreating] = useState(false)

  // Project selector dropdown
  const [selectorOpen, setSelectorOpen] = useState(false)

  // Wizard state
  const [wizardStep, setWizardStep] = useState(1)
  const [wizardProgress, setWizardProgress] = useState('')
  const [confirmedOutlineId, setConfirmedOutlineId] = useState<string | null>(null)

  // Auto-save ref
  const saveTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const lastSavedRef = useRef<string>('')

  // ----------------------------------------------------------------
  // Load project list on mount
  // ----------------------------------------------------------------
  useEffect(() => {
    if (!projectsLoaded) {
      apiFetch<ProjectListRes>('/api/projects')
        .then((data) => {
          setProjects(data.projects)
          setProjectsLoaded(true)
        })
        .catch((err) => console.error('Failed to load projects:', err))
    }
  }, [projectsLoaded]) // eslint-disable-line react-hooks/exhaustive-deps

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
          `/api/projects/${projectId}/chapters`
        )
        const normChs = chs.map((c) => normalizeChapter(c as unknown as Record<string, unknown>))
        setChapters(normChs)

        // Check if project has outlines; if not, show wizard
        const outlines = await apiFetch<OutlineRes[]>(
          `/api/projects/${projectId}/outlines`
        )
        if (outlines.length === 0 && normalized.length === 0) {
          setActiveView('wizard')
          setWizardStep(1)
        } else if (normalized.length > 0) {
          setActiveView('editor')
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

  // ----------------------------------------------------------------
  // Select a project from the dropdown
  // ----------------------------------------------------------------
  const handleSelectProject = useCallback(
    (project: Project) => {
      setCurrentProject(project)
      selectChapter(null)
      setEditorContent('')
      setOutlinePreview('')
      setSelectorOpen(false)
    },
    [setCurrentProject, selectChapter]
  )

  // ----------------------------------------------------------------
  // Create new project
  // ----------------------------------------------------------------
  const handleCreateProject = useCallback(async () => {
    if (!newTitle.trim() || creating) return
    setCreating(true)
    try {
      const project = await apiFetch<Project>('/api/projects', {
        method: 'POST',
        body: JSON.stringify({
          title: newTitle.trim(),
          genre: newGenre,
          premise: newPremise.trim() || null,
        }),
      })
      setProjects([project, ...projects])
      setCurrentProject(project)
      selectChapter(null)
      setVolumes([])
      setChapters([])
      setShowNewProject(false)
      setNewTitle('')
      setNewPremise('')
      setActiveView('wizard')
      setWizardStep(1)
    } catch (err) {
      console.error('Failed to create project:', err)
    } finally {
      setCreating(false)
    }
  }, [
    newTitle,
    newGenre,
    newPremise,
    creating,
    projects,
    setProjects,
    setCurrentProject,
    selectChapter,
    setVolumes,
    setChapters,
  ])

  // ----------------------------------------------------------------
  // Outline generation (SSE)
  // ----------------------------------------------------------------
  const handleGenerateOutline = useCallback(
    (level: string) => {
      if (isGenerating) return
      setIsGenerating(true)
      setOutlinePreview('')
      if (level === 'book') {
        setActiveView('wizard')
      } else {
        setActiveView('outline')
      }

      apiSSE(
        '/api/generate/outline',
        {
          project_id: currentProject?.id || '',
          level,
          user_input: creativeInput,
        },
        (text) => {
          setOutlinePreview((prev) => prev + text)
        },
        () => {
          setIsGenerating(false)
        }
      )
    },
    [isGenerating, currentProject, creativeInput, setIsGenerating]
  )

  // ----------------------------------------------------------------
  // Confirm outline => POST to outlines API, advance wizard
  // ----------------------------------------------------------------
  const handleConfirmOutline = useCallback(async () => {
    if (!currentProject || !outlinePreview) return

    try {
      // Try to parse the outline as JSON; if not, store as raw text
      let contentJson: Record<string, unknown>
      try {
        contentJson = JSON.parse(outlinePreview)
      } catch {
        contentJson = { raw_text: outlinePreview }
      }

      const outline = await apiFetch<OutlineRes>(
        `/api/projects/${currentProject.id}/outlines`,
        {
          method: 'POST',
          body: JSON.stringify({
            level: 'book',
            content_json: contentJson,
          }),
        }
      )

      // Confirm it
      await apiFetch<OutlineRes>(
        `/api/projects/${currentProject.id}/outlines/${outline.id}/confirm`,
        { method: 'POST' }
      )

      setConfirmedOutlineId(outline.id)
      setWizardStep(2)
    } catch (err) {
      console.error('Failed to save outline:', err)
    }
  }, [currentProject, outlinePreview])

  // ----------------------------------------------------------------
  // Wizard Step 2: Generate volume outlines
  // ----------------------------------------------------------------
  const handleGenerateVolumeOutlines = useCallback(async () => {
    if (!currentProject || isGenerating) return
    setIsGenerating(true)
    setWizardProgress('正在分析大纲，生成分卷结构...')

    try {
      // Generate volume outline via SSE
      let volumeOutlineText = ''
      await new Promise<void>((resolve) => {
        apiSSE(
          '/api/generate/outline',
          {
            project_id: currentProject.id,
            level: 'volume',
            user_input: creativeInput,
            parent_outline_id: confirmedOutlineId,
          },
          (text) => {
            volumeOutlineText += text
            setWizardProgress(`正在生成分卷大纲...\n${volumeOutlineText.slice(-200)}`)
          },
          () => resolve()
        )
      })

      // Save volume outline
      let volContentJson: Record<string, unknown>
      try {
        volContentJson = JSON.parse(volumeOutlineText)
      } catch {
        volContentJson = { raw_text: volumeOutlineText }
      }

      await apiFetch<OutlineRes>(
        `/api/projects/${currentProject.id}/outlines`,
        {
          method: 'POST',
          body: JSON.stringify({
            level: 'volume',
            parent_id: confirmedOutlineId,
            content_json: volContentJson,
          }),
        }
      )

      // Create volume records from the outline
      // Try to extract volume titles from the outline text
      const volumeTitles = extractVolumeTitles(volumeOutlineText)
      const createdVolumes: Volume[] = []

      for (let i = 0; i < volumeTitles.length; i++) {
        const vol = await apiFetch<VolumeRes>(
          `/api/projects/${currentProject.id}/volumes`,
          {
            method: 'POST',
            body: JSON.stringify({
              title: volumeTitles[i],
              volume_idx: i + 1,
            }),
          }
        )
        createdVolumes.push(
          normalizeVolume(vol as unknown as Record<string, unknown>)
        )
      }

      if (createdVolumes.length > 0) {
        setVolumes(createdVolumes)
      }

      setWizardProgress('分卷大纲已生成！')
      setWizardStep(3)
    } catch (err) {
      console.error('Failed to generate volume outlines:', err)
      setWizardProgress('生成失败，请重试')
    } finally {
      setIsGenerating(false)
    }
  }, [
    currentProject,
    isGenerating,
    creativeInput,
    confirmedOutlineId,
    setIsGenerating,
    setVolumes,
  ])

  // ----------------------------------------------------------------
  // Wizard Step 3: Generate chapter outlines + create chapters
  // ----------------------------------------------------------------
  const handleGenerateChapterOutlines = useCallback(async () => {
    if (!currentProject || isGenerating) return

    const { volumes: currentVolumes } = useProjectStore.getState()
    if (currentVolumes.length === 0) {
      setWizardProgress('请先生成分卷大纲')
      return
    }

    setIsGenerating(true)
    setWizardProgress('正在生成章节大纲...')

    try {
      for (let vi = 0; vi < currentVolumes.length; vi++) {
        const vol = currentVolumes[vi]
        setWizardProgress(
          `正在生成第 ${vi + 1}/${currentVolumes.length} 卷的章节大纲...`
        )

        // Generate chapter outline for this volume
        let chapterOutlineText = ''
        await new Promise<void>((resolve) => {
          apiSSE(
            '/api/generate/outline',
            {
              project_id: currentProject.id,
              level: 'chapter',
              user_input: creativeInput,
              volume_idx: vi + 1,
            },
            (text) => {
              chapterOutlineText += text
            },
            () => resolve()
          )
        })

        // Extract chapter titles from the generated text
        const chapterTitles = extractChapterTitles(chapterOutlineText)

        // Create chapter records
        const createdChs: Chapter[] = []
        for (let ci = 0; ci < chapterTitles.length; ci++) {
          const ch = await apiFetch<ChapterRes>(
            `/api/projects/${currentProject.id}/chapters`,
            {
              method: 'POST',
              body: JSON.stringify({
                volume_id: vol.id,
                title: chapterTitles[ci],
                chapter_idx: ci + 1,
                outline_json: { raw_text: chapterOutlineText },
              }),
            }
          )
          createdChs.push(
            normalizeChapter(ch as unknown as Record<string, unknown>)
          )
        }
        addChapters(createdChs)
      }

      setWizardProgress('所有章节大纲已生成！')
      // Reload all chapters
      const finalChs = await apiFetch<ChapterRes[]>(
        `/api/projects/${currentProject.id}/chapters`
      )
      setChapters(
        finalChs.map((c) => normalizeChapter(c as unknown as Record<string, unknown>))
      )
      setActiveView('editor')
    } catch (err) {
      console.error('Failed to generate chapter outlines:', err)
      setWizardProgress('生成失败，请重试')
    } finally {
      setIsGenerating(false)
    }
  }, [
    currentProject,
    isGenerating,
    creativeInput,
    addChapters,
    setChapters,
    setIsGenerating,
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
    setIsGenerating(true)
    resetStreamContent()
    setEditorContent('')
    setActiveView('editor')
    updateChapterStatus(selectedChapterId, 'generating')

    apiSSE(
      '/api/generate/chapter',
      {
        project_id: currentProject.id,
        chapter_id: selectedChapterId,
      },
      (text) => {
        appendStreamContent(text)
        setEditorContent((prev) => prev + text)
      },
      () => {
        setIsGenerating(false)
        updateChapterStatus(selectedChapterId, 'completed')
        // Save the generated content
        const finalContent = useProjectStore.getState().chapters.find(
          (c) => c.id === selectedChapterId
        )
        if (finalContent) {
          apiFetch(
            `/api/projects/${currentProject.id}/chapters/${selectedChapterId}`,
            {
              method: 'PUT',
              body: JSON.stringify({
                content_text: finalContent.contentText,
                status: 'completed',
              }),
            }
          ).catch((err) => console.error('Failed to save generated chapter:', err))
        }
      }
    )
  }, [
    isGenerating,
    selectedChapterId,
    currentProject,
    setIsGenerating,
    resetStreamContent,
    appendStreamContent,
    updateChapterStatus,
  ])

  // ----------------------------------------------------------------
  // Helpers: extract volume/chapter titles from outline text
  // ----------------------------------------------------------------

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      if (saveTimerRef.current) clearTimeout(saveTimerRef.current)
    }
  }, [])

  // ----------------------------------------------------------------
  // Get current chapter info
  // ----------------------------------------------------------------
  const currentChapter = selectedChapterId
    ? chapters.find((c) => c.id === selectedChapterId)
    : null

  // ================================================================
  // RENDER
  // ================================================================

  return (
    <WorkspaceLayout
      sidebar={
        <div className="flex flex-col h-full">
          {/* ---- Project selector ---- */}
          <div className="p-4 border-b border-gray-200">
            <h2 className="text-lg font-semibold text-gray-900 mb-2">AI Write</h2>

            {/* Project dropdown */}
            <div className="relative">
              <button
                onClick={() => setSelectorOpen(!selectorOpen)}
                className="w-full flex items-center justify-between px-3 py-2 text-sm border border-gray-200 rounded-lg hover:bg-gray-50 transition-colors"
              >
                <span className="truncate text-left flex-1">
                  {currentProject ? currentProject.title : '选择项目...'}
                </span>
                <svg
                  className={`w-4 h-4 text-gray-400 ml-1 transition-transform ${
                    selectorOpen ? 'rotate-180' : ''
                  }`}
                  fill="none"
                  viewBox="0 0 24 24"
                  stroke="currentColor"
                  strokeWidth={2}
                >
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    d="M19 9l-7 7-7-7"
                  />
                </svg>
              </button>

              {selectorOpen && (
                <div className="absolute z-50 w-full mt-1 bg-white border border-gray-200 rounded-lg shadow-lg max-h-60 overflow-y-auto">
                  {projects.length === 0 && (
                    <div className="px-3 py-2 text-sm text-gray-400">
                      暂无项目
                    </div>
                  )}
                  {projects.map((p) => (
                    <button
                      key={p.id}
                      onClick={() => handleSelectProject(p)}
                      className={`w-full text-left px-3 py-2 text-sm hover:bg-blue-50 transition-colors ${
                        currentProject?.id === p.id
                          ? 'bg-blue-50 text-blue-700 font-medium'
                          : 'text-gray-700'
                      }`}
                    >
                      <div className="truncate">{p.title}</div>
                      {p.genre && (
                        <div className="text-[10px] text-gray-400">{p.genre}</div>
                      )}
                    </button>
                  ))}
                </div>
              )}
            </div>
          </div>

          {/* ---- Volume/Chapter tree ---- */}
          <div className="flex-1 overflow-y-auto">
            <div className="p-3">
              <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-2">
                卷 / 章节
              </h3>
              <OutlineTree
                onSelectChapter={(chapterId) => {
                  selectChapter(chapterId)
                  setActiveView('editor')
                }}
              />
            </div>
          </div>

          {/* ---- New project button ---- */}
          <div className="p-3 border-t border-gray-200">
            <button
              onClick={() => setShowNewProject(true)}
              className="w-full px-3 py-2 text-sm bg-gray-900 text-white rounded-lg hover:bg-gray-800 transition-colors"
            >
              + 新建项目
            </button>
          </div>
        </div>
      }
      editor={
        <div className="h-full flex flex-col">
          {/* ---- New Project Modal ---- */}
          {showNewProject && (
            <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
              <div className="bg-white rounded-2xl shadow-xl w-full max-w-md mx-4 p-6">
                <h3 className="text-lg font-bold text-gray-900 mb-4">
                  新建项目
                </h3>

                <div className="space-y-4">
                  <div>
                    <label className="block text-sm font-medium text-gray-700 mb-1">
                      书名 <span className="text-red-500">*</span>
                    </label>
                    <input
                      type="text"
                      value={newTitle}
                      onChange={(e) => setNewTitle(e.target.value)}
                      placeholder="输入小说名称"
                      className="w-full px-3 py-2 text-sm border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent"
                      autoFocus
                    />
                  </div>

                  <div>
                    <label className="block text-sm font-medium text-gray-700 mb-1">
                      类型
                    </label>
                    <select
                      value={newGenre}
                      onChange={(e) => setNewGenre(e.target.value)}
                      className="w-full px-3 py-2 text-sm border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent"
                    >
                      {GENRES.map((g) => (
                        <option key={g} value={g}>
                          {g}
                        </option>
                      ))}
                    </select>
                  </div>

                  <div>
                    <label className="block text-sm font-medium text-gray-700 mb-1">
                      故事梗概
                    </label>
                    <textarea
                      value={newPremise}
                      onChange={(e) => setNewPremise(e.target.value)}
                      placeholder="简要描述你的小说设定和核心创意..."
                      className="w-full px-3 py-2 text-sm border border-gray-300 rounded-lg resize-none h-24 focus:ring-2 focus:ring-blue-500 focus:border-transparent"
                    />
                  </div>
                </div>

                <div className="flex gap-3 mt-6">
                  <button
                    onClick={() => setShowNewProject(false)}
                    className="flex-1 px-4 py-2 text-sm border border-gray-300 rounded-lg hover:bg-gray-50 text-gray-700"
                  >
                    取消
                  </button>
                  <button
                    onClick={handleCreateProject}
                    disabled={!newTitle.trim() || creating}
                    className="flex-1 px-4 py-2 text-sm bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
                  >
                    {creating ? '创建中...' : '创建项目'}
                  </button>
                </div>
              </div>
            </div>
          )}

          {/* ---- Creative input (no project selected) ---- */}
          {activeView === 'creative' && (
            <div className="flex-1 p-8 max-w-2xl mx-auto w-full flex flex-col items-center justify-center">
              <h2 className="text-2xl font-bold text-gray-900 mb-2">
                开始你的创作之旅
              </h2>
              <p className="text-gray-500 mb-6 text-center">
                选择一个现有项目，或者点击左下角 &quot;新建项目&quot; 创建一本新书。
              </p>
              {!currentProject && projects.length > 0 && (
                <div className="w-full max-w-sm space-y-2">
                  {projects.slice(0, 5).map((p) => (
                    <button
                      key={p.id}
                      onClick={() => handleSelectProject(p)}
                      className="w-full text-left px-4 py-3 border border-gray-200 rounded-lg hover:bg-blue-50 hover:border-blue-200 transition-colors"
                    >
                      <div className="font-medium text-gray-800">{p.title}</div>
                      {p.genre && (
                        <div className="text-xs text-gray-500 mt-0.5">
                          {p.genre}
                        </div>
                      )}
                    </button>
                  ))}
                </div>
              )}
            </div>
          )}

          {/* ---- Outline Wizard ---- */}
          {activeView === 'wizard' && currentProject && (
            <div className="flex-1 p-8 overflow-y-auto">
              <div className="max-w-2xl mx-auto">
                {/* Wizard steps indicator */}
                <div className="flex items-center gap-2 mb-6">
                  {[1, 2, 3].map((step) => (
                    <div key={step} className="flex items-center gap-2">
                      <div
                        className={`w-8 h-8 rounded-full flex items-center justify-center text-sm font-medium ${
                          wizardStep === step
                            ? 'bg-blue-600 text-white'
                            : wizardStep > step
                              ? 'bg-green-500 text-white'
                              : 'bg-gray-200 text-gray-500'
                        }`}
                      >
                        {wizardStep > step ? '✓' : step}
                      </div>
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
                        <h3 className="text-sm font-semibold text-gray-700 mb-2">
                          大纲预览
                        </h3>
                        <pre className="whitespace-pre-wrap text-sm text-gray-800 bg-gray-50 p-4 rounded-xl border max-h-96 overflow-y-auto">
                          {outlinePreview}
                        </pre>

                        {!isGenerating && (
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
                      基于全书大纲，AI 将自动生成每卷的详细大纲和结构。
                    </p>

                    {wizardProgress && (
                      <pre className="whitespace-pre-wrap text-sm text-gray-700 bg-gray-50 p-4 rounded-xl border mb-4 max-h-64 overflow-y-auto">
                        {wizardProgress}
                      </pre>
                    )}

                    <button
                      onClick={handleGenerateVolumeOutlines}
                      disabled={isGenerating}
                      className="px-6 py-2.5 bg-indigo-600 text-white rounded-xl hover:bg-indigo-700 disabled:opacity-50 disabled:cursor-not-allowed text-sm font-medium"
                    >
                      {isGenerating ? '正在生成...' : '生成分卷大纲'}
                    </button>
                  </div>
                )}

                {/* Step 3: Chapter outlines */}
                {wizardStep === 3 && (
                  <div>
                    <h2 className="text-xl font-bold text-gray-900 mb-2">
                      第三步：生成章节大纲
                    </h2>
                    <p className="text-gray-500 mb-4 text-sm">
                      为每一卷生成章节大纲，并自动创建章节记录。
                    </p>

                    {wizardProgress && (
                      <pre className="whitespace-pre-wrap text-sm text-gray-700 bg-gray-50 p-4 rounded-xl border mb-4 max-h-64 overflow-y-auto">
                        {wizardProgress}
                      </pre>
                    )}

                    <div className="flex gap-3">
                      <button
                        onClick={handleGenerateChapterOutlines}
                        disabled={isGenerating}
                        className="px-6 py-2.5 bg-purple-600 text-white rounded-xl hover:bg-purple-700 disabled:opacity-50 disabled:cursor-not-allowed text-sm font-medium"
                      >
                        {isGenerating ? '正在生成...' : '生成章节大纲'}
                      </button>
                      <button
                        onClick={() => setActiveView('editor')}
                        className="px-6 py-2.5 border border-gray-300 text-gray-700 rounded-xl hover:bg-gray-50 text-sm"
                      >
                        跳过，直接进入编辑
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
              {currentChapter && (
                <div className="max-w-3xl mx-auto pt-4 px-6">
                  <div className="flex items-center justify-between mb-2">
                    <h3 className="text-lg font-semibold text-gray-800">
                      {currentChapter.title}
                    </h3>
                    <div className="flex items-center gap-2">
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
                              : 'bg-gray-100 text-gray-600'
                        }`}
                      >
                        {currentChapter.status === 'completed'
                          ? '完成'
                          : currentChapter.status === 'generating'
                            ? '生成中'
                            : '草稿'}
                      </span>
                    </div>
                  </div>
                </div>
              )}

              <div className="max-w-3xl mx-auto py-4 px-6">
                {selectedChapterId && (
                  <div className="mb-3">
                    <button
                      onClick={handleGenerateChapter}
                      disabled={isGenerating}
                      className="px-4 py-2 text-sm bg-green-600 text-white rounded-lg hover:bg-green-700 disabled:opacity-50 disabled:cursor-not-allowed"
                    >
                      {isGenerating ? '生成中...' : '生成本章'}
                    </button>
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
        </div>
      }
      panel={
        <div className="flex flex-col h-full">
          <div className="flex-1 overflow-y-auto">
            <CollapsibleSection title="生成设置" defaultOpen>
              <GeneratePanel
                onGenerate={handleGenerateChapter}
                onGenerateOutline={handleGenerateOutline}
              />
            </CollapsibleSection>

            {selectedChapterId && (
              <CollapsibleSection title="质量评估">
                <div className="px-4">
                  <EvaluationPanel chapterId={selectedChapterId} />
                </div>
              </CollapsibleSection>
            )}

            {selectedChapterId && (
              <CollapsibleSection title="质量检查详情">
                <div className="px-4">
                  <CheckerDashboard chapterId={selectedChapterId} />
                </div>
              </CollapsibleSection>
            )}

            {currentProject && (
              <CollapsibleSection title="三线平衡">
                <div className="px-4">
                  <StrandPanel projectId={currentProject.id} />
                </div>
              </CollapsibleSection>
            )}

            <CollapsibleSection title="写作指南">
              <div className="px-4">
                <WritingGuidePanel />
              </div>
            </CollapsibleSection>

            {selectedChapterId && (
              <CollapsibleSection title="去AI味检查">
                <div className="px-4">
                  <AntiAIPanel chapterId={selectedChapterId} />
                </div>
              </CollapsibleSection>
            )}

            {selectedChapterId && (
              <CollapsibleSection title="版本历史">
                <div className="px-4">
                  <VersionPanel chapterId={selectedChapterId} />
                </div>
              </CollapsibleSection>
            )}

            {currentProject && (
              <CollapsibleSection title="伏笔追踪">
                <div className="px-4">
                  <ForeshadowPanel projectId={currentProject.id} />
                </div>
              </CollapsibleSection>
            )}

            {currentProject && (
              <CollapsibleSection title="设定集">
                <div className="px-4">
                  <SettingsPanel projectId={currentProject.id} />
                </div>
              </CollapsibleSection>
            )}

            {currentProject && (
              <CollapsibleSection title="角色关系">
                <div className="px-4">
                  <RelationshipGraph projectId={currentProject.id} />
                </div>
              </CollapsibleSection>
            )}
          </div>

          {/* Token dashboard always at bottom */}
          <div className="border-t border-gray-200 p-3 bg-white">
            <TokenDashboard />
          </div>
        </div>
      }
    />
  )
}

// ================================================================
// Utility: extract volume titles from generated outline text
// ================================================================

function extractVolumeTitles(text: string): string[] {
  const titles: string[] = []

  // Try JSON first
  try {
    const parsed = JSON.parse(text)
    if (Array.isArray(parsed)) {
      for (const item of parsed) {
        if (typeof item === 'string') titles.push(item)
        else if (item && typeof item.title === 'string') titles.push(item.title)
        else if (item && typeof item.name === 'string') titles.push(item.name)
      }
      if (titles.length > 0) return titles
    }
    if (parsed && Array.isArray(parsed.volumes)) {
      for (const v of parsed.volumes) {
        if (typeof v === 'string') titles.push(v)
        else if (v && typeof v.title === 'string') titles.push(v.title)
        else if (v && typeof v.name === 'string') titles.push(v.name)
      }
      if (titles.length > 0) return titles
    }
  } catch {
    // not JSON, try regex
  }

  // Regex patterns for volume titles
  const patterns = [
    /第[一二三四五六七八九十百千\d]+卷[：:]\s*(.+)/g,
    /卷[一二三四五六七八九十百千\d]+[：:]\s*(.+)/g,
    /Volume\s*\d+[：:]\s*(.+)/gi,
    /第[一二三四五六七八九十百千\d]+卷\s+(.+)/g,
  ]

  for (const pattern of patterns) {
    let match
    while ((match = pattern.exec(text)) !== null) {
      titles.push(match[1].trim())
    }
    if (titles.length > 0) return titles
  }

  // Fallback: if we found nothing, create 3 default volumes
  if (titles.length === 0) {
    titles.push('第一卷', '第二卷', '第三卷')
  }

  return titles
}

function extractChapterTitles(text: string): string[] {
  const titles: string[] = []

  // Try JSON first
  try {
    const parsed = JSON.parse(text)
    if (Array.isArray(parsed)) {
      for (const item of parsed) {
        if (typeof item === 'string') titles.push(item)
        else if (item && typeof item.title === 'string') titles.push(item.title)
        else if (item && typeof item.name === 'string') titles.push(item.name)
      }
      if (titles.length > 0) return titles
    }
    if (parsed && Array.isArray(parsed.chapters)) {
      for (const c of parsed.chapters) {
        if (typeof c === 'string') titles.push(c)
        else if (c && typeof c.title === 'string') titles.push(c.title)
        else if (c && typeof c.name === 'string') titles.push(c.name)
      }
      if (titles.length > 0) return titles
    }
  } catch {
    // not JSON
  }

  // Regex patterns for chapter titles
  const patterns = [
    /第[一二三四五六七八九十百千\d]+章[：:]\s*(.+)/g,
    /章[一二三四五六七八九十百千\d]+[：:]\s*(.+)/g,
    /Chapter\s*\d+[：:]\s*(.+)/gi,
    /第[一二三四五六七八九十百千\d]+章\s+(.+)/g,
  ]

  for (const pattern of patterns) {
    let match
    while ((match = pattern.exec(text)) !== null) {
      titles.push(match[1].trim())
    }
    if (titles.length > 0) return titles
  }

  // Fallback: create some default chapters
  if (titles.length === 0) {
    for (let i = 1; i <= 10; i++) {
      titles.push(`第${i}章`)
    }
  }

  return titles
}
