'use client'

import { useState, useCallback, useEffect, useRef } from 'react'
import dynamic from 'next/dynamic'
import { useRouter, useSearchParams } from 'next/navigation'
import { WorkspaceLayout } from '@/components/workspace/WorkspaceLayout'
import { OutlineTree } from '@/components/outline/OutlineTree'
import { VolumeOutlineBlock } from '@/components/outline/VolumeOutlineBlock'
import { GeneratePanel, getSelectedStyleId } from '@/components/panels/GeneratePanel'

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
}// ----------------------------------------------------------------
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
  const [activeView, setActiveView] = useState<'editor' | 'outline' | 'wizard'>(
    'wizard'
  )

  // Wizard state
  const [wizardStep, setWizardStep] = useState(1)
  const [wizardProgress, setWizardProgress] = useState('')
  const [confirmedOutlineId, setConfirmedOutlineId] = useState<string | null>(null)
  // Volume generation config & results
  const [volumeCountInput, setVolumeCountInput] = useState('')
  const [volumeOutlines, setVolumeOutlines] = useState<Record<number, Record<string, unknown>>>({})
  // Tracks the backend auto-saved outline id for the current in-progress book outline
  const pendingBookOutlineIdRef = useRef<string | null>(null)

  // Drawer panel
  const [drawerPanel, setDrawerPanel] = useState<string | null>(null)

  // Auto-save ref
  const saveTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const lastSavedRef = useRef<string>('')

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
          `/api/projects/${projectId}/chapters`
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
        } else {
          setOutlinePreview('')
          setConfirmedOutlineId(null)
        }

        // Index volume outlines by volume_idx (pick the most recent per idx)
        const volOutlineMap: Record<number, Record<string, unknown>> = {}
        for (const o of outlines) {
          if (o.level !== 'volume') continue
          const cj = (o.content_json as Record<string, unknown>) || {}
          const idx = typeof cj.volume_idx === 'number' ? cj.volume_idx : null
          if (idx !== null) {
            volOutlineMap[idx] = cj
          }
        }
        setVolumeOutlines(volOutlineMap)

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

  // ----------------------------------------------------------------
  // Outline generation (SSE)
  // ----------------------------------------------------------------
  const handleGenerateOutline = useCallback(
    (level: string) => {
      if (isGenerating) return
      setIsGenerating(true)
      setOutlinePreview('')
      if (level === 'book') {
        pendingBookOutlineIdRef.current = null
        setConfirmedOutlineId(null)
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
        },
        (evt) => {
          if (level === 'book' && evt.status === 'saved' && typeof evt.outline_id === 'string') {
            pendingBookOutlineIdRef.current = evt.outline_id
          }
        },
      )
    },
    [isGenerating, currentProject, creativeInput, setIsGenerating]
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
            apiSSE(
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
            )
          })
          return { text, outlineId, parsed: parseVolumeOutline(text) }
        }

        setWizardProgress(`正在生成第 ${i}/${count} 卷大纲...`)
        let { text: volumeOutlineText, outlineId: volumeOutlineId, parsed } = await runOnce()
        if (isEmptyOrInvalid(parsed)) {
          setWizardProgress((prev) => prev + `\n第 ${i} 卷首次生成无效，重试中...`)
          const retry = await runOnce()
          volumeOutlineText = retry.text
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
        style_id: getSelectedStyleId(currentProject.id),
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
            {drawerPanel === 'relationship' && <RelationshipGraph projectId={currentProject.id} />}
          </div>
        </div>
      </div>
    )}

    <WorkspaceLayout
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
                onChanged={() => {
                  if (currentProject) loadProjectData(currentProject.id)
                }}
                onSelectChapter={(chapterId) => {
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
                        {isGenerating ? '正在生成...' : '生成分卷大纲'}
                      </button>
                      <button
                        onClick={() => setWizardStep(1)}
                        disabled={isGenerating}
                        className="px-6 py-2.5 border border-gray-300 text-gray-700 rounded-xl hover:bg-gray-50 disabled:opacity-50 text-sm"
                      >
                        返回修改大纲
                      </button>
                    </div>
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
                        onClick={() => setActiveView('editor')}
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
                    <button onClick={() => { setActiveView('wizard'); setWizardStep(2) }}
                      className="px-3 py-1.5 text-xs bg-indigo-600 text-white rounded-lg">
                      继续生成分卷
                    </button>
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
                projectId={currentProject?.id}
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

            {/* Panels that need more space — open as drawers */}
            {currentProject && (
              <div className="border-b border-gray-200 px-4 py-3 space-y-2">
                <p className="text-xs font-semibold text-gray-500 uppercase tracking-wide">详细面板</p>
                {[
                  { label: '三线平衡', key: 'strand' },
                  { label: '伏笔追踪', key: 'foreshadow' },
                  { label: '设定集', key: 'settings' },
                  { label: '角色关系', key: 'relationship' },
                ].map(item => (
                  <button key={item.key} onClick={() => setDrawerPanel(item.key)}
                    className="w-full text-left px-3 py-2 text-sm text-gray-700 bg-gray-50 rounded-lg hover:bg-gray-100 transition-colors flex items-center justify-between">
                    <span>{item.label}</span>
                    <svg className="w-4 h-4 text-gray-400" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                      <path strokeLinecap="round" strokeLinejoin="round" d="M9 5l7 7-7 7" />
                    </svg>
                  </button>
                ))}
              </div>
            )}
          </div>

          {/* Token dashboard always at bottom */}
          <div className="border-t border-gray-200 p-3 bg-white">
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
