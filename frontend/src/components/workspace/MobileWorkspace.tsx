'use client'

import { useState, useEffect, useCallback } from 'react'
import { apiFetch, apiSSE } from '@/lib/api'

interface Project {
  id: string
  title: string
  genre: string
}

interface Volume {
  id: string
  title: string
  volume_idx: number
}

interface Chapter {
  id: string
  title: string
  chapter_idx: number
  word_count: number
  status: string
  content_text: string
}

export default function MobileWorkspace() {
  const [projects, setProjects] = useState<Project[]>([])
  const [currentProject, setCurrentProject] = useState<Project | null>(null)
  const [volumes, setVolumes] = useState<Volume[]>([])
  const [chapters, setChapters] = useState<Chapter[]>([])
  const [selectedChapter, setSelectedChapter] = useState<Chapter | null>(null)
  const [editorContent, setEditorContent] = useState('')
  const [isGenerating, setIsGenerating] = useState(false)
  const [tab, setTab] = useState<'list' | 'editor' | 'create'>('list')
  const [creativeInput, setCreativeInput] = useState('')
  const [outlinePreview, setOutlinePreview] = useState('')
  const [newTitle, setNewTitle] = useState('')
  const [newGenre, setNewGenre] = useState('')

  // Load projects
  useEffect(() => {
    apiFetch<{ projects: Project[] }>('/api/projects')
      .then(d => setProjects(d.projects))
      .catch(() => {})
  }, [])

  // Load volumes + chapters
  const loadProject = useCallback(async (p: Project) => {
    setCurrentProject(p)
    setSelectedChapter(null)
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
      setTab('list')
    } catch { /* */ }
  }

  const handleGenerateOutline = () => {
    if (!currentProject || !creativeInput.trim() || isGenerating) return
    setIsGenerating(true)
    setOutlinePreview('')
    apiSSE(
      '/api/generate/outline',
      { project_id: currentProject.id, level: 'book', user_input: creativeInput },
      (text) => setOutlinePreview(prev => prev + text),
      () => setIsGenerating(false),
    )
  }

  const handleGenerateChapter = () => {
    if (!currentProject || !selectedChapter || isGenerating) return
    setIsGenerating(true)
    setEditorContent('')
    apiSSE(
      '/api/generate/chapter',
      { project_id: currentProject.id, chapter_id: selectedChapter.id },
      (text) => setEditorContent(prev => prev + text),
      () => setIsGenerating(false),
    )
  }

  return (
    <div className="flex flex-col h-screen pt-12 bg-gray-50">
      <div className="flex-1 overflow-y-auto">

        {/* Project list / Chapter list */}
        {tab === 'list' && (
          <div className="p-4">
            {!currentProject ? (
              <>
                <h2 className="text-lg font-bold text-gray-900 mb-3">选择项目</h2>
                {projects.map(p => (
                  <button
                    key={p.id}
                    onClick={() => loadProject(p)}
                    className="w-full text-left px-4 py-3 bg-white rounded-lg mb-2 border border-gray-200"
                  >
                    <div className="font-medium text-gray-900">{p.title}</div>
                    <div className="text-xs text-gray-500">{p.genre}</div>
                  </button>
                ))}
                {projects.length === 0 && (
                  <p className="text-sm text-gray-400 mb-4">暂无项目</p>
                )}
                <button
                  onClick={() => setTab('create')}
                  className="w-full py-2.5 bg-blue-600 text-white rounded-lg text-sm font-medium"
                >
                  + 新建项目
                </button>
              </>
            ) : (
              <>
                <div className="flex items-center justify-between mb-3">
                  <h2 className="text-lg font-bold text-gray-900">{currentProject.title}</h2>
                  <button
                    onClick={() => { setCurrentProject(null); setVolumes([]); setChapters([]) }}
                    className="text-xs text-blue-600"
                  >
                    切换项目
                  </button>
                </div>

                {volumes.length === 0 ? (
                  <div className="space-y-3">
                    <p className="text-sm text-gray-500">暂无大纲，输入创意开始生成：</p>
                    <textarea
                      value={creativeInput}
                      onChange={e => setCreativeInput(e.target.value)}
                      placeholder="描述你的小说创意..."
                      className="w-full h-32 p-3 border border-gray-300 rounded-lg text-sm resize-none"
                    />
                    <button
                      onClick={handleGenerateOutline}
                      disabled={isGenerating || !creativeInput.trim()}
                      className="w-full py-2.5 bg-blue-600 text-white rounded-lg text-sm disabled:opacity-50"
                    >
                      {isGenerating ? '生成中...' : '生成大纲'}
                    </button>
                    {outlinePreview && (
                      <pre className="p-3 bg-white rounded-lg text-xs text-gray-700 whitespace-pre-wrap border">
                        {outlinePreview}
                      </pre>
                    )}
                  </div>
                ) : (
                  <div>
                    {volumes.map(v => (
                      <div key={v.id} className="mb-3">
                        <h3 className="text-sm font-semibold text-gray-700 mb-1">{v.title}</h3>
                        {chapters
                          .filter(c => (c as any).volume_id ?? (c as any).volumeId === v.id)
                          .sort((a, b) => a.chapter_idx - b.chapter_idx)
                          .map(ch => (
                            <button
                              key={ch.id}
                              onClick={() => selectChapter(ch)}
                              className="w-full text-left px-3 py-2 bg-white rounded mb-1 border border-gray-100 flex justify-between items-center"
                            >
                              <span className="text-sm text-gray-800">{ch.title}</span>
                              <span className="text-xs text-gray-400">
                                {ch.word_count > 0 ? `${(ch.word_count/1000).toFixed(1)}k` : '草稿'}
                              </span>
                            </button>
                          ))
                        }
                      </div>
                    ))}
                  </div>
                )}
              </>
            )}
          </div>
        )}

        {/* Editor */}
        {tab === 'editor' && selectedChapter && (
          <div className="p-4">
            <div className="flex items-center justify-between mb-3">
              <h2 className="text-base font-bold text-gray-900">{selectedChapter.title}</h2>
              <button
                onClick={handleGenerateChapter}
                disabled={isGenerating}
                className="px-3 py-1.5 bg-green-600 text-white rounded text-xs disabled:opacity-50"
              >
                {isGenerating ? '生成中...' : '生成'}
              </button>
            </div>
            <textarea
              value={editorContent}
              onChange={e => setEditorContent(e.target.value)}
              placeholder="章节内容..."
              className="w-full h-[60vh] p-3 border border-gray-200 rounded-lg text-sm leading-relaxed resize-none"
              readOnly={isGenerating}
            />
          </div>
        )}

        {/* Create project */}
        {tab === 'create' && (
          <div className="p-4 space-y-3">
            <h2 className="text-lg font-bold text-gray-900">新建项目</h2>
            <input
              value={newTitle}
              onChange={e => setNewTitle(e.target.value)}
              placeholder="小说标题"
              className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm"
            />
            <select
              value={newGenre}
              onChange={e => setNewGenre(e.target.value)}
              className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm"
            >
              <option value="">选择题材</option>
              {['玄幻','仙侠','都市','言情','悬疑','科幻','历史','其他'].map(g => (
                <option key={g} value={g}>{g}</option>
              ))}
            </select>
            <div className="flex gap-2">
              <button onClick={handleCreateProject} className="flex-1 py-2 bg-blue-600 text-white rounded-lg text-sm">创建</button>
              <button onClick={() => setTab('list')} className="flex-1 py-2 bg-gray-200 text-gray-700 rounded-lg text-sm">取消</button>
            </div>
          </div>
        )}
      </div>

      {/* Bottom tabs */}
      <div className="flex border-t border-gray-200 bg-white">
        <button
          onClick={() => setTab('list')}
          className={`flex-1 py-2.5 text-center text-xs font-medium ${tab === 'list' ? 'text-blue-600 bg-blue-50' : 'text-gray-500'}`}
        >
          <div className="text-base mb-0.5">📁</div>目录
        </button>
        <button
          onClick={() => { if (selectedChapter) setTab('editor') }}
          className={`flex-1 py-2.5 text-center text-xs font-medium ${tab === 'editor' ? 'text-blue-600 bg-blue-50' : 'text-gray-500'}`}
        >
          <div className="text-base mb-0.5">✏️</div>编辑
        </button>
      </div>
    </div>
  )
}
