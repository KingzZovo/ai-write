'use client'

import { useEffect, useState } from 'react'
import { apiFetch } from '@/lib/api'
import { pipelineActive, useBookPipeline } from './useBookPipeline'

const labels: Record<string, string> = {
  planning: '准备就绪', queued: '排队中', generating: '生成中', pausing: '暂停中',
  paused: '已暂停', completed: '已完成', needs_review: '待审核', failed: '失败',
  pending: '待生成', reviewing: '待恢复', polishing: '待恢复',
}
const button = 'rounded-md border border-gray-300 px-3 py-2 text-xs font-medium hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed focus-visible:outline-2 focus-visible:outline-blue-500'

export function BookPipelinePanel({ projectId }: { projectId: string }) {
  // Project key prevents responses/form state from crossing book boundaries.
  return <ProjectPipeline key={projectId} projectId={projectId} />
}

function ProjectPipeline({ projectId }: { projectId: string }) {
  const pipeline = useBookPipeline(projectId)
  const { run, runs, volumes, busy, loading, error } = pipeline
  const [volumeId, setVolumeId] = useState('')
  const [instruction, setInstruction] = useState('')
  const [targetWords, setTargetWords] = useState('')
  const [confirmation, setConfirmation] = useState<'start' | 'rollback' | null>(null)
  const [page, setPage] = useState(0)
  const [previewId, setPreviewId] = useState<string | null>(null)
  const active = pipelineActive(run?.state)
  const chapters = run?.chapters ?? []
  const pageCount = Math.max(1, Math.ceil(chapters.length / 25))
  const currentPage = Math.min(page, pageCount - 1)
  const anyActive = runs.some(item => pipelineActive(item.state))

  return <section aria-label="整书生成" className="space-y-3 border-t border-gray-200 pt-4">
    <div className="flex items-center justify-between gap-2">
      <h3 className="text-sm font-semibold text-gray-900">整书生成</h3>
      <button className={button} disabled={busy || loading} onClick={() => void pipeline.refresh()}>刷新任务</button>
    </div>
    <p className="text-xs leading-relaxed text-gray-500">按大纲逐章生成，沿用本书风格设置。关闭页面后任务继续运行；可在章节完成后暂停。</p>
    {loading && <p role="status" className="text-xs text-gray-500">加载整书任务…</p>}
    {error && <p role="alert" className="rounded-md bg-red-50 p-2 text-xs text-red-700">{error}</p>}
    {!loading && <details open={!run}>
      <summary className="cursor-pointer text-xs font-medium text-blue-700">创建生成任务</summary>
      <form className="mt-3 space-y-3" onSubmit={async event => {
        event.preventDefault()
        setConfirmation(null)
        if (await pipeline.create(volumeId, { user_input: instruction, max_attempts: 2,
          ...(targetWords ? { target_words: Number(targetWords) } : {}),
        })) setPage(0)
      }}>
        <label className="block text-xs text-gray-700">生成范围
          <select value={volumeId} onChange={event => setVolumeId(event.target.value)} className="mt-1 w-full rounded-md border p-2">
            <option value="">整本书</option>
            {volumes.map(volume => <option key={volume.id} value={volume.id}>第{volume.volume_idx}卷 · {volume.title}</option>)}
          </select>
        </label>
        <label className="block text-xs text-gray-700">每章目标字数（留空沿用章节设置）
          <input type="number" min={500} max={20000} step={1} value={targetWords} onChange={event => setTargetWords(event.target.value)} className="mt-1 w-full rounded-md border p-2" />
        </label>
        <label className="block text-xs text-gray-700">本次写作要求
          <textarea value={instruction} maxLength={20000} rows={3} onChange={event => setInstruction(event.target.value)} placeholder="例如：保持有限视角，延续上一章的行动与情绪。" className="mt-1 w-full rounded-md border p-2" />
        </label>
        <button type="submit" className={button} disabled={busy || anyActive}>创建任务并检查章节</button>
        {anyActive && <p className="text-xs text-amber-700">本书已有运行中的任务，请先暂停或等待完成。</p>}
      </form>
    </details>}
    {runs.length > 1 && <label className="block text-xs text-gray-700">最近任务（最多20条）
      <select className="mt-1 w-full rounded-md border p-2" value={run?.id ?? ''} disabled={busy} onChange={event => {
        pipeline.select(event.target.value); setConfirmation(null); setPage(0)
      }}>
        {runs.map(item => <option key={item.id} value={item.id}>{labels[item.state] ?? item.state} · {item.total_chapters}章 · {item.created_at ? new Date(item.created_at).toLocaleString() : item.id.slice(0, 8)}</option>)}
      </select>
    </label>}
    {run && <>
      <div role="status" className="space-y-2 rounded-md bg-gray-50 p-3 text-xs">
        <p className="font-semibold text-gray-900">{labels[run.state] ?? run.state} · {run.volume_id ? '分卷任务' : '整书任务'}</p>
        <progress aria-label="已完成章节" className="h-2 w-full accent-blue-600" value={run.completed_chapters} max={Math.max(1, run.total_chapters)} />
        <p>已完成 {run.completed_chapters} / {run.total_chapters} 章 · 待审核 {run.needs_review_chapters} 章</p>
        {run.state === 'queued' && <p>任务已保存，等待开始。队列暂时不可用时会自动重试。</p>}
        {run.state === 'pausing' && <p>正在完成当前章节并保存，随后暂停。</p>}
        {run.error_message && <p className="text-amber-800">{run.error_message}</p>}
      </div>
      <div className="flex flex-wrap gap-2">
        {!active && run.state !== 'completed' && <button className={button} disabled={busy} onClick={() => setConfirmation('start')}>
          {run.state === 'planning' ? '启动生成' : run.state === 'paused' ? '继续生成' : '重试未完成章节'}
        </button>}
        {active && <button className={button} disabled={busy || run.state === 'pausing'} onClick={() => void pipeline.command('pause')}>暂停任务</button>}
        {run.can_rollback && <button className={button} disabled={busy || active} onClick={() => setConfirmation('rollback')}>恢复生成前版本</button>}
      </div>
      {confirmation && <div className="space-y-2 rounded-md border border-amber-200 bg-amber-50 p-3 text-xs" role="group" aria-label="确认操作">
        <p>{confirmation === 'start'
          ? `将处理 ${chapters.filter(chapter => chapter.state !== 'completed').length} 个未完成章节，已有正文可能被更新。任务保留首次启动前的恢复快照。待审核章节也会重新生成。`
          : '将恢复本任务生成前的正文、摘要与章节状态。如果之后有人工修改，系统会拒绝自动覆盖。'}</p>
        <div className="flex gap-2">
          <button className={button} disabled={busy} onClick={async () => {
            if (await pipeline.command(confirmation)) setConfirmation(null)
          }}>{busy ? '处理中…' : '确认执行'}</button>
          <button className={button} disabled={busy} onClick={() => setConfirmation(null)}>取消</button>
        </div>
      </div>}
      <ol className="divide-y divide-gray-100" aria-label="章节进度">
        {chapters.slice(currentPage * 25, (currentPage + 1) * 25).map(chapter => <li key={chapter.chapter_id} className="py-2 text-xs">
          <div className="flex items-center justify-between gap-2">
            <span>第{chapter.volume_idx ?? '?'}卷 · 第{chapter.chapter_idx}章</span>
            <span className={chapter.state === 'needs_review' || chapter.state === 'failed' ? 'text-amber-700' : 'text-gray-500'}>{labels[chapter.state] ?? chapter.state}</span>
          </div>
          {chapter.error_message && <p className="mt-1 text-amber-800">{chapter.error_message}</p>}
          {chapter.word_count > 0 && <button className="mt-1 py-1 text-blue-700 underline" onClick={() => setPreviewId(chapter.chapter_id)}>查看正文 · {chapter.word_count}字</button>}
        </li>)}
      </ol>
      {pageCount > 1 && <nav aria-label="章节分页" className="flex items-center justify-between gap-2 text-xs">
        <button className={button} disabled={currentPage === 0} onClick={() => setPage(currentPage - 1)}>上一页</button>
        <span>{currentPage + 1} / {pageCount}</span>
        <button className={button} disabled={currentPage + 1 === pageCount} onClick={() => setPage(currentPage + 1)}>下一页</button>
      </nav>}
    </>}
    {previewId && <ChapterPreview key={previewId} projectId={projectId} chapterId={previewId} onClose={() => setPreviewId(null)} />}
  </section>
}

function ChapterPreview({ projectId, chapterId, onClose }: { projectId: string; chapterId: string; onClose: () => void }) {
  const [chapter, setChapter] = useState<{ title: string; content_text: string } | null>(null)
  const [error, setError] = useState('')
  useEffect(() => {
    const controller = new AbortController()
    void apiFetch<{ title: string; content_text: string }>(`/api/projects/${projectId}/chapters/${chapterId}`, { signal: controller.signal })
      .then(result => { if (!controller.signal.aborted) setChapter(result) })
      .catch(err => { if (!controller.signal.aborted) setError(err instanceof Error ? err.message : '正文加载失败') })
    return () => controller.abort()
  }, [projectId, chapterId])
  return <section aria-label="正文预览" className="space-y-2 rounded-md border border-blue-200 p-3">
    <div className="flex items-center justify-between gap-2"><h4 className="text-sm font-medium">{chapter?.title ?? '正文预览'}</h4><button onClick={onClose} className={button}>关闭预览</button></div>
    <p className="text-xs text-gray-500">预览不会更改编辑区。需要修改时，请在章节目录中打开本章。</p>
    {error ? <p role="alert" className="text-xs text-red-700">{error}</p> : chapter
      ? <textarea readOnly aria-label="已保存正文" value={chapter.content_text} rows={12} className="w-full rounded border p-2 text-sm leading-relaxed" />
      : <p role="status" className="text-xs">加载正文…</p>}
  </section>
}
