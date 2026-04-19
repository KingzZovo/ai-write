'use client'

import { useState, useEffect, useCallback } from 'react'
import dynamic from 'next/dynamic'
import { useRouter, useSearchParams } from 'next/navigation'
import { apiFetch, apiSSE } from '@/lib/api'
import { getSelectedStructureBookId } from '@/components/panels/GeneratePanel'

// Lazy load panels — only when user opens the tools tab
const GeneratePanel = dynamic(() => import('@/components/panels/GeneratePanel').then(m => ({ default: m.GeneratePanel })), { ssr: false })
const ForeshadowPanel = dynamic(() => import('@/components/panels/ForeshadowPanel').then(m => ({ default: m.ForeshadowPanel })), { ssr: false })
const EvaluationPanel = dynamic(() => import('@/components/panels/EvaluationPanel').then(m => ({ default: m.EvaluationPanel })), { ssr: false })
const WritingGuidePanel = dynamic(() => import('@/components/panels/WritingGuidePanel').then(m => ({ default: m.WritingGuidePanel })), { ssr: false })
const SettingsPanel = dynamic(() => import('@/components/panels/SettingsPanel').then(m => ({ default: m.SettingsPanel })), { ssr: false })

interface Project { id: string; title: string; genre: string }
interface Volume { id: string; title: string; volume_idx: number }
interface Chapter { id: string; title: string; chapter_idx: number; word_count: number; status: string; content_text: string; volume_id?: string; volumeId?: string }

interface Outline { id: string; level: string; content_json: Record<string, unknown> }

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
  const [chapters, setChapters] = useState<Chapter[]>([])
  const [selectedChapter, setSelectedChapter] = useState<Chapter | null>(null)
  const [editorContent, setEditorContent] = useState('')
  const [isGenerating, setIsGenerating] = useState(false)
  const [tab, setTab] = useState<'list' | 'editor' | 'tools' | 'create'>('list')
  const [creativeInput, setCreativeInput] = useState('')
  const [outlinePreview, setOutlinePreview] = useState('')
  const [newTitle, setNewTitle] = useState('')
  const [newGenre, setNewGenre] = useState('')
  const [toolsTab, setToolsTab] = useState<'generate' | 'guide' | 'foreshadow' | 'settings' | 'eval'>('generate')

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
    setTab('list')
    try {
      const vols = await apiFetch<Volume[]>(`/api/projects/${p.id}/volumes`)
      setVolumes(vols)
      if (vols.length > 0) {
        const chs = await apiFetch<Chapter[]>(`/api/projects/${p.id}/chapters`)
        setChapters(chs)
      } else {
        setChapters([])
      }
      // Load saved outlines
      const outlines = await apiFetch<Outline[]>(`/api/projects/${p.id}/outlines`)
      const bookOutline = outlines.find(o => o.level === 'book')
      if (bookOutline) {
        const raw = String((bookOutline.content_json as any)?.raw_text || JSON.stringify(bookOutline.content_json, null, 2))
        setSavedOutline(raw)
      }
      // Check for running generation tasks
      try {
        const tasks = await apiFetch<any[]>(`/api/generate/async/project/${p.id}`)
        // Load polished text from completed task
        const completed = tasks.find((t: any) => t.status === 'completed' && t.task_type?.startsWith('outline'))
        if (completed) {
          const full = await apiFetch<any>(`/api/generate/async/${completed.task_id}`)
          if (full.polished_text) setPolishedOutline(full.polished_text)
          if (full.result_text) setSavedOutline(prev => prev || full.result_text)
        }
        const running = tasks.find((t: any) => t.status === 'pending' || t.status === 'running' || t.status === 'polishing')
        if (running) {
          setIsGenerating(true)
          setGenTaskId(running.task_id)
          // Resume polling
          const poll = setInterval(async () => {
            try {
              const status = await apiFetch<any>(`/api/generate/async/${running.task_id}`)
              if (status.task_type.startsWith('outline')) {
                setOutlinePreview(status.progress_text || '')
              } else {
                setEditorContent(status.progress_text || '')
              }
              if (status.status === 'completed') {
                clearInterval(poll)
                setIsGenerating(false)
                setGenTaskId(null)
                if (status.task_type.startsWith('outline')) {
                  setSavedOutline(status.result_text)
                  setPolishedOutline(status.polished_text || '')
                  setOutlinePreview('')
                }
              } else if (status.status === 'failed') {
                clearInterval(poll)
                setIsGenerating(false)
                setGenTaskId(null)
              }
            } catch { /* */ }
          }, 3000)
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
      setOutlinePreview('')
      setVolumes([])
      setChapters([])
      setTab('list')
    } catch { /* */ }
  }

  const [genTaskId, setGenTaskId] = useState<string | null>(null)

  const handleGenerateOutline = async (level?: string) => {
    if (!currentProject) { alert('请先选择一个项目'); return }
    if (isGenerating) { alert('正在生成中，请稍候'); return }
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
          structure_book_id: getSelectedStructureBookId(currentProject?.id) || undefined,
        }),
      })
      setGenTaskId(data.task_id)
      // Start polling
      const poll = setInterval(async () => {
        try {
          const status = await apiFetch<any>(`/api/generate/async/${data.task_id}`)
          setOutlinePreview(status.progress_text || '')
          if (status.status === 'polishing') {
            setOutlinePreview(status.progress_text || status.result_text || '')
          } else if (status.status === 'completed') {
            clearInterval(poll)
            setIsGenerating(false)
            setGenTaskId(null)
            setSavedOutline(status.result_text)
            setPolishedOutline(status.polished_text || '')
            setOutlinePreview('')
          } else if (status.status === 'failed') {
            clearInterval(poll)
            setIsGenerating(false)
            setGenTaskId(null)
            alert(`生成失败: ${status.error_message || '未知错误'}`)
          }
        } catch { /* */ }
      }, 3000)
    } catch (e) {
      setIsGenerating(false)
      alert(e instanceof Error ? e.message : '提交生成任务失败')
    }
  }

  const handleGenerateChapter = async () => {
    if (!currentProject) { alert('请先选择一个项目'); return }
    if (!selectedChapter) { alert('请先在目录中选择一个章节'); return }
    if (isGenerating) { alert('正在生成中，请稍候'); return }
    setIsGenerating(true)
    setEditorContent('')
    setTab('editor')
    try {
      const data = await apiFetch<{ task_id: string }>('/api/generate/async', {
        method: 'POST',
        body: JSON.stringify({ project_id: currentProject.id, task_type: 'chapter', chapter_id: selectedChapter.id }),
      })
      setGenTaskId(data.task_id)
      const poll = setInterval(async () => {
        try {
          const status = await apiFetch<any>(`/api/generate/async/${data.task_id}`)
          setEditorContent(status.progress_text || '')
          if (status.status === 'completed') {
            clearInterval(poll)
            setIsGenerating(false)
            setGenTaskId(null)
            setEditorContent(status.result_text)
          } else if (status.status === 'failed') {
            clearInterval(poll)
            setIsGenerating(false)
            setGenTaskId(null)
            alert(`生成失败: ${status.error_message || '未知错误'}`)
          }
        } catch { /* */ }
      }, 3000)
    } catch (e) {
      setIsGenerating(false)
      alert(e instanceof Error ? e.message : '提交生成任务失败')
    }
  }

  const statusLabel: Record<string, string> = { draft: '草稿', generating: '生成中', completed: '完成' }

  return (
    <div className="flex flex-col h-screen pt-12 bg-gray-50">
      <div className="flex-1 overflow-y-auto">

        {/* 项目列表 / 章节列表 */}
        {tab === 'list' && (
          <div className="p-4">
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
                  <button onClick={() => router.push('/')}
                    className="text-xs text-blue-600 shrink-0 ml-2">返回项目列表</button>
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
                      <pre className="whitespace-pre-wrap text-sm text-gray-700 bg-blue-50 p-3 rounded-lg border border-blue-100 max-h-[50vh] overflow-y-auto leading-relaxed"
                        style={{ fontFamily: "'Noto Serif SC', serif" }}>
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
                    <pre className="whitespace-pre-wrap text-sm text-gray-700 bg-gray-50 p-3 rounded-lg border max-h-[60vh] overflow-y-auto leading-relaxed"
                      style={{ fontFamily: "'Noto Serif SC', serif" }}>
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
                  <div>
                    {volumes.sort((a, b) => a.volume_idx - b.volume_idx).map(v => (
                      <div key={v.id} className="mb-3">
                        <h3 className="text-sm font-semibold text-gray-700 mb-1 px-1">{v.title}</h3>
                        {chapters
                          .filter(c => (c.volume_id ?? c.volumeId) === v.id)
                          .sort((a, b) => a.chapter_idx - b.chapter_idx)
                          .map(ch => (
                            <button key={ch.id} onClick={() => selectChapter(ch)}
                              className="w-full text-left px-3 py-2.5 bg-white rounded-lg mb-1 border border-gray-100 flex justify-between items-center active:bg-gray-50">
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
                          ))}
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
              <h2 className="text-base font-bold text-gray-900 truncate">{selectedChapter.title}</h2>
              <button onClick={handleGenerateChapter} disabled={isGenerating}
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
              <GeneratePanel onGenerate={handleGenerateChapter} onGenerateOutline={handleGenerateOutline} />
            )}
            {toolsTab === 'guide' && <WritingGuidePanel />}
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
