'use client'

import { useEffect, useState } from 'react'
import { apiFetch } from '@/lib/api'
import type { CognitionChange } from '@/lib/cognitionTypes'
import { CognitionChanges } from './CognitionChanges'

type Revision = {
  revision_id: string; mode: string; changes: CognitionChange[] | null; actor: string | null
  created_at: string; valid: boolean; matches_current_source: boolean; is_current: boolean
  model_snapshot: { changes?: CognitionChange[] | null; status?: string } | null
}
const buttonClass = 'rounded px-2 py-1 text-blue-700 hover:bg-blue-50 disabled:opacity-40'

export function CognitionHistory({ projectId, chapterId, currentVersion }: {
  projectId: string; chapterId: string; currentVersion: string | null
}) {
  const [data, setData] = useState<{ revisions: Revision[]; has_more: boolean } | null>(null)
  const [page, setPage] = useState(0)
  const [reload, setReload] = useState(0)
  const [error, setError] = useState('')
  useEffect(() => {
    let live = true
    const controller = new AbortController()
    setData(null); setError('')
    void apiFetch<{ revisions: Revision[]; has_more: boolean }>(
      `/api/projects/${projectId}/cognition/chapters/${chapterId}/revisions?limit=10&offset=${page * 10}`,
      { signal: controller.signal }).then(result => { if (live) setData(result) })
      .catch(() => { if (live) setError('无法读取校订历史，请重试。') })
    return () => { live = false; controller.abort() }
  }, [projectId, chapterId, currentVersion, page, reload])
  return <section aria-label="认知校订历史" className="space-y-3 border-t border-gray-200 pt-3">
    <div className="flex items-center justify-between"><h4 className="font-medium">校订历史</h4>
      <button className={buttonClass} onClick={() => setReload(n => n + 1)}>刷新校订历史</button></div>
    {error ? <p role="alert" className="text-red-700">{error}</p> : !data && <p role="status">正在读取校订历史…</p>}
    {data?.revisions.length === 0 && <p>暂无作者校订记录。</p>}
    {data?.revisions.map(item => <article key={item.revision_id} className="space-y-2 rounded bg-gray-50 p-3">
      <p>{item.mode === 'author' ? '作者校订' : '恢复自动提取'} · {new Date(item.created_at).toLocaleString('zh-CN')}
        {item.is_current ? ' · 当前决定' : ''}</p>
      <p className="text-xs text-gray-500">{item.actor || '未记录登录账号'} · {item.matches_current_source
        ? '读取时与当前保存稿一致' : '对应历史保存稿，当前原文已变化或不可用'}</p>
      {!item.valid ? <p role="alert" className="text-red-700">这条历史记录完整性未通过核对。</p>
        : item.mode === 'author' && item.changes && <CognitionChanges changes={item.changes} emptyLabel="作者确认本章不记录认知变化。" />}
      {item.valid && <details><summary className="cursor-pointer text-blue-700">操作时的模型结果</summary>
        <p className="text-xs text-gray-500">这里保留操作时存储的模型条目，可能来自未完成或已失效的提取。</p>
        {Array.isArray(item.model_snapshot?.changes) ? <CognitionChanges changes={item.model_snapshot.changes}
          emptyLabel="当时保存的模型条目为空。" />
          : <p>当时没有可用模型结果。</p>}
      </details>}
    </article>)}
    {(page > 0 || data?.has_more) && <div className="flex justify-between">
      <button className={buttonClass} disabled={page === 0 || !data} onClick={() => setPage(n => n - 1)}>上一页校订</button>
      <button className={buttonClass} disabled={!data?.has_more} onClick={() => setPage(n => n + 1)}>下一页校订</button>
    </div>}
  </section>
}
