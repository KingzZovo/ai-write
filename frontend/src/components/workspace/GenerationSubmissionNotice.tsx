'use client'

import { useGenerationSubmission } from './useGenerationSubmission'

export function GenerationSubmissionNotice({ projectId }: { projectId: string }) {
  const { session, state } = useGenerationSubmission(projectId)
  if (!session || !state || (!state.pending && !state.storageError)) return null
  return <div className="space-y-2 rounded border border-amber-200 bg-amber-50 p-3 text-xs">
    <p role={state.error ? 'alert' : 'status'}>{state.error || '正在确认生成任务，原请求和章稿基准已保留。'}</p>
    {state.storageError && <p role="alert">{state.storageError}</p>}
    {state.pending && <>
      <p className="break-all text-gray-600">请求编号：{state.pending.id}</p>
      <p>重试会继续核对这份原请求，沿用最初选择的作品、章节和保存稿。</p>
      <button type="button" disabled={state.busy} onClick={() => void session.retry().catch(() => {})}
        className="mr-3 text-blue-800 disabled:opacity-50">{state.busy ? '正在确认…' : '核对并继续原请求'}</button>
      {state.pending.rejected && <button type="button" disabled={state.busy} onClick={session.dismissRejected}
        className="text-gray-700 disabled:opacity-50">清除已拒绝请求</button>}
    </>}
    {!state.pending && state.storageError && <button type="button" onClick={session.restore}
      className="text-blue-800">重新读取本页请求记录</button>}
  </div>
}
