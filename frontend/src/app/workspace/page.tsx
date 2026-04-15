'use client'

import { useState, useCallback } from 'react'
import { WorkspaceLayout } from '@/components/workspace/WorkspaceLayout'
import { OutlineTree } from '@/components/outline/OutlineTree'
import { GeneratePanel } from '@/components/panels/GeneratePanel'
import { ForeshadowPanel } from '@/components/panels/ForeshadowPanel'
import { SettingsPanel } from '@/components/panels/SettingsPanel'
import { EvaluationPanel } from '@/components/panels/EvaluationPanel'
import { VersionPanel } from '@/components/panels/VersionPanel'
import { TokenDashboard } from '@/components/panels/TokenDashboard'
import { RelationshipGraph } from '@/components/panels/RelationshipGraph'
import { useProjectStore } from '@/stores/projectStore'
import { useGenerationStore } from '@/stores/generationStore'
import { apiSSE } from '@/lib/api'

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

export default function WorkspacePage() {
  const { currentProject, selectedChapterId } = useProjectStore()
  const { isGenerating, setIsGenerating, appendStreamContent, resetStreamContent } =
    useGenerationStore()

  const [editorContent, setEditorContent] = useState('')
  const [creativeInput, setCreativeInput] = useState('')
  const [outlinePreview, setOutlinePreview] = useState('')
  const [activeView, setActiveView] = useState<'editor' | 'outline' | 'creative'>('creative')

  const handleGenerateOutline = useCallback(
    (level: string) => {
      if (isGenerating) return
      setIsGenerating(true)
      setOutlinePreview('')
      setActiveView('outline')

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

  const handleGenerateChapter = useCallback(() => {
    if (isGenerating || !selectedChapterId) return
    setIsGenerating(true)
    resetStreamContent()
    setActiveView('editor')

    apiSSE(
      '/api/generate/chapter',
      {
        project_id: currentProject?.id || '',
        chapter_id: selectedChapterId,
      },
      (text) => {
        appendStreamContent(text)
        setEditorContent((prev) => prev + text)
      },
      () => {
        setIsGenerating(false)
      }
    )
  }, [
    isGenerating,
    selectedChapterId,
    currentProject,
    setIsGenerating,
    resetStreamContent,
    appendStreamContent,
  ])

  return (
    <WorkspaceLayout
      sidebar={
        <div className="flex flex-col h-full">
          <div className="p-4 border-b border-gray-200">
            <h2 className="text-lg font-semibold text-gray-900">AI Write</h2>
            <p className="text-xs text-gray-500 mt-1">
              {currentProject ? currentProject.title : 'No project selected'}
            </p>
          </div>

          <div className="flex-1 overflow-y-auto">
            <div className="p-3">
              <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-2">
                Volumes & Chapters
              </h3>
              <OutlineTree onSelectChapter={() => setActiveView('editor')} />
            </div>
          </div>

          <div className="p-3 border-t border-gray-200">
            <button
              onClick={() => setActiveView('creative')}
              className="w-full px-3 py-2 text-sm bg-gray-900 text-white rounded-lg hover:bg-gray-800"
            >
              + New Project
            </button>
          </div>
        </div>
      }
      editor={
        <div className="h-full flex flex-col">
          {activeView === 'creative' && (
            <div className="flex-1 p-8 max-w-2xl mx-auto w-full">
              <h2 className="text-2xl font-bold text-gray-900 mb-2">Create New Novel</h2>
              <p className="text-gray-500 mb-6">
                Describe your novel idea, and AI will generate the complete outline structure.
              </p>
              <textarea
                value={creativeInput}
                onChange={(e) => setCreativeInput(e.target.value)}
                placeholder={`Example:\n都市修仙，主角是一个外卖员，意外获得一本修炼功法...\n\nDescribe the genre, setting, main character, and core premise.`}
                className="w-full h-64 px-4 py-3 text-base border border-gray-300 rounded-xl resize-none focus:ring-2 focus:ring-blue-500 focus:border-transparent"
                disabled={isGenerating}
              />
              <button
                onClick={() => handleGenerateOutline('book')}
                disabled={isGenerating || !creativeInput.trim()}
                className="mt-4 px-6 py-3 bg-blue-600 text-white rounded-xl hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed text-base font-medium"
              >
                {isGenerating ? 'Generating Outline...' : 'Generate Book Outline'}
              </button>
            </div>
          )}

          {activeView === 'outline' && (
            <div className="flex-1 p-8 overflow-y-auto">
              <h2 className="text-xl font-bold text-gray-900 mb-4">Generated Outline</h2>
              <pre className="whitespace-pre-wrap text-sm text-gray-800 bg-gray-50 p-6 rounded-xl border">
                {outlinePreview || 'Generating...'}
              </pre>
              {!isGenerating && outlinePreview && (
                <div className="mt-4 flex gap-3">
                  <button className="px-4 py-2 bg-green-600 text-white rounded-lg hover:bg-green-700 text-sm">
                    Confirm Outline
                  </button>
                  <button
                    onClick={() => handleGenerateOutline('book')}
                    className="px-4 py-2 bg-gray-200 text-gray-700 rounded-lg hover:bg-gray-300 text-sm"
                  >
                    Regenerate
                  </button>
                </div>
              )}
            </div>
          )}

          {activeView === 'editor' && (
            <div className="flex-1 overflow-y-auto">
              <div className="max-w-3xl mx-auto py-8 px-6">
                <textarea
                  value={editorContent}
                  onChange={(e) => setEditorContent(e.target.value)}
                  placeholder="Chapter content will appear here after generation..."
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
            <CollapsibleSection title="Generation Settings" defaultOpen>
              <GeneratePanel
                onGenerate={handleGenerateChapter}
                onGenerateOutline={handleGenerateOutline}
              />
            </CollapsibleSection>

            {selectedChapterId && (
              <CollapsibleSection title="Quality Evaluation">
                <div className="px-4">
                  <EvaluationPanel chapterId={selectedChapterId} />
                </div>
              </CollapsibleSection>
            )}

            {selectedChapterId && (
              <CollapsibleSection title="Version History">
                <div className="px-4">
                  <VersionPanel chapterId={selectedChapterId} />
                </div>
              </CollapsibleSection>
            )}

            {currentProject && (
              <CollapsibleSection title="Foreshadows">
                <div className="px-4">
                  <ForeshadowPanel projectId={currentProject.id} />
                </div>
              </CollapsibleSection>
            )}

            {currentProject && (
              <CollapsibleSection title="Settings">
                <div className="px-4">
                  <SettingsPanel projectId={currentProject.id} />
                </div>
              </CollapsibleSection>
            )}

            {currentProject && (
              <CollapsibleSection title="Character Relations">
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
