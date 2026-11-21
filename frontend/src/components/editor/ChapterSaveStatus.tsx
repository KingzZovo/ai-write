'use client'

import { useState } from 'react'
import { ChapterDraftSession, ChapterDraftSnapshot } from './chapterDraftSession'

export function ChapterSaveStatus({ session, state }: { session: ChapterDraftSession; state: ChapterDraftSnapshot }) {
  const [force, setForce] = useState(false)
  const busy = state.loading || state.saving || state.streaming
  const dirty = session.isDirty()
  const label = state.streaming ? '生成稿预览，结束后核对保存结果'
    : state.loading ? '正在读取保存稿…' : state.saving ? '正在保存…'
      : state.blocked ? '等待核对，编辑区稿件已保留' : dirty ? '尚有未保存修改' : '正文已保存'

  const download = () => {
    const url = URL.createObjectURL(new Blob([state.text], { type: 'text/plain;charset=utf-8' }))
    const link = document.createElement('a')
    link.href = url
    link.download = `${(state.source?.title || state.chapterId).replace(/[\\/:*?"<>|]/g, '_')}-编辑稿.txt`
    document.body.appendChild(link)
    link.click()
    link.remove()
    URL.revokeObjectURL(url)
  }

  return <section aria-label="正文保存状态" className="mb-3 space-y-2 rounded-lg border border-gray-200 bg-gray-50 p-3 text-xs">
    <p role="status" className={state.blocked || dirty ? 'text-amber-800' : 'text-gray-600'}>{label}</p>
    {state.error && <p role="alert" className="text-red-700">{state.error}</p>}
    {state.backupError && <p role="alert" className="text-red-700">{state.backupError}</p>}
    {state.notice && <p className="text-gray-600">{state.notice}</p>}
    <div className="flex flex-wrap gap-3">
      <button type="button" disabled={busy || state.blocked || !dirty} onClick={() => void session.save()} className="text-blue-700 disabled:opacity-50">立即保存</button>
      <button type="button" disabled={busy} onClick={() => { setForce(false); void session.reload() }} className="text-blue-700 disabled:opacity-50">读取保存稿并核对</button>
      <button type="button" onClick={download} className="text-blue-700">下载编辑区备份</button>
    </div>
    {state.comparison && <div className="space-y-2 border-t border-gray-200 pt-2">
      <p className="text-amber-800">下方是这次读取的完整保存稿；编辑区保留你的稿件。请核对后选择。</p>
      <textarea aria-label="当前保存稿对照" readOnly value={state.comparison.content_text} className="h-60 w-full resize-y rounded border bg-white p-3 text-sm leading-relaxed" />
      <label className="flex items-center gap-2 text-amber-800">
        <input type="checkbox" checked={force} disabled={busy} onChange={(event) => setForce(event.target.checked)} />
        我已核对，允许正文清空或明显缩短，并保留替换前全文
      </label>
      <div className="flex flex-wrap gap-3">
        <button type="button" disabled={busy} onClick={() => {
          if (window.confirm('确认以编辑区稿件更新刚才核对的保存稿？')) void session.saveReviewedDraft(force)
        }} className="rounded bg-blue-700 px-3 py-1.5 text-white disabled:opacity-50">确认保存编辑区稿件</button>
        <button type="button" disabled={busy} onClick={() => {
          if (window.confirm('确认放弃编辑区修改，载入下方保存稿？需要保留时请先下载编辑区备份。')) session.useSavedDraft()
        }} className="text-blue-700 disabled:opacity-50">使用保存稿</button>
      </div>
    </div>}
    <p className="text-gray-500">未保存稿会按章节暂存在当前浏览器标签页；关闭标签页前请保存或下载备份。</p>
  </section>
}
