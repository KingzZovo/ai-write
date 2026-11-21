'use client'

import { MobileTask, MobileTaskDetail, isMobileTaskTerminal } from './mobileTaskProgress'
import { statusLabel, taskTypeLabel } from './asyncTaskQueue'

export function MobileTaskPanel({ task, detail, error, loading, chapterTitle, onRetry, onOpenChapter, onReviewVolume }: {
  task: MobileTask
  detail: MobileTaskDetail | null
  error: string | null
  loading: boolean
  chapterTitle: string | null
  onRetry: () => void
  onOpenChapter?: () => void
  onReviewVolume?: (taskId: string) => void
}) {
  const status = detail?.status ?? task.status
  const download = () => {
    if (!detail) return
    const url = URL.createObjectURL(new Blob([detail.resultText || detail.progressText], { type: 'text/plain;charset=utf-8' }))
    const link = document.createElement('a')
    link.href = url
    link.download = `生成任务-${task.taskId}.txt`
    document.body.appendChild(link)
    link.click()
    link.remove()
    URL.revokeObjectURL(url)
  }
  return <section aria-label="后台生成任务" className="space-y-2 rounded-lg border border-blue-200 bg-blue-50 p-3 text-xs">
    <p className="font-semibold text-blue-900">{taskTypeLabel(task.taskType)} · {statusLabel(status)}</p>
    {task.chapterId && <p className="text-gray-700">来源章节：{chapterTitle || '章节暂不可用'}</p>}
    {task.taskType === 'outline_volume' && <p className="text-gray-700">{task.volumeIdx ? `任务指定：第 ${task.volumeIdx} 卷` : '任务未记录明确卷号，请核对候选内容。'}</p>}
    {loading && <p role="status">正在读取任务进度…</p>}
    {error && <p role="alert" className="text-red-700">{error}</p>}
    {detail?.errorMessage && <p className="text-amber-800">{detail.errorMessage}</p>}
    <p className="text-gray-600">{isMobileTaskTerminal(status)
      ? task.taskType === 'outline_volume' ? '任务交付的是卷纲候选。请核对候选与本卷保存稿，再决定是否采用；已采用内容以保存稿为准。'
        : task.taskType.startsWith('outline') ? '任务已结束，请核对目录中对应大纲的实际保存稿。润色候选尚未自动采用。'
          : '任务已结束，请核对来源章节的实际保存稿。任务状态不代表小说已经过作者审定。'
      : '任务在后台处理，可切换章节；编辑区保留作者稿件。'}</p>
    <div className="flex flex-wrap gap-3">
      <button type="button" onClick={onRetry} disabled={loading} className="text-blue-800 disabled:opacity-50">重新读取任务</button>
      {onOpenChapter && <button type="button" onClick={onOpenChapter} className="text-blue-800">查看来源章节</button>}
      {task.taskType === 'outline_volume' && status === 'completed' && onReviewVolume && <button type="button"
        onClick={() => onReviewVolume(task.taskId)} className="min-h-10 text-blue-800">核对并采用卷纲候选</button>}
      {detail && (detail.resultText || detail.progressText) && <button type="button" onClick={download} className="text-blue-800">下载任务文本</button>}
    </div>
    {detail?.resultText && <details>
      <summary className="cursor-pointer text-blue-800">查看任务候选全文</summary>
      <p className="mt-2 text-amber-800">这是任务返回的候选文本，请与实际保存稿及作者修改核对。</p>
      <textarea aria-label="任务候选全文" value={detail.resultText} readOnly className="mt-2 h-64 w-full resize-y rounded border bg-white p-3 text-sm leading-relaxed" />
    </details>}
    {detail?.progressText && detail.progressText !== detail.resultText && <details>
      <summary className="cursor-pointer text-blue-800">查看任务进度记录</summary>
      <p className="mt-2 text-gray-600">进度可能包含状态说明或未完成片段。</p>
      <pre className="mt-2 max-h-64 overflow-y-auto whitespace-pre-wrap rounded bg-white p-3 leading-relaxed">{detail.progressText}</pre>
    </details>}
    {detail?.polishedText && <details>
      <summary className="cursor-pointer text-blue-800">任务润色候选（待复核）</summary>
      <p className="mt-2 text-amber-800">历史润色候选尚未自动采用，请与当前大纲或正文核对。</p>
      <textarea aria-label="任务润色候选全文" value={detail.polishedText} readOnly className="mt-2 h-64 w-full resize-y rounded border bg-white p-3 text-sm leading-relaxed" />
    </details>}
  </section>
}
