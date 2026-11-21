'use client'

import { useEffect, useState } from 'react'
import { apiFetch } from '@/lib/api'
import { cognitionKindLabels, cognitionKinds, validCognitionChange } from '@/lib/cognitionTypes'
import type { CognitionDetail, CognitionKind } from '@/lib/cognitionTypes'
import {
  changesFromRows, newCognitionDraft, readCognitionDraft, validateCognitionDraft, writeCognitionDraft,
} from '@/lib/cognitionDrafts'
import type { CognitionDraft, CognitionDraftRow } from '@/lib/cognitionDrafts'
import type { CognitionIntent } from '@/lib/cognitionRequests'
import { CognitionChanges } from './CognitionChanges'

const buttonClass = 'rounded px-2 py-1 text-blue-700 hover:bg-blue-50 disabled:opacity-40'
const fieldClass = 'w-full rounded border border-gray-300 bg-white px-2 py-1.5 text-gray-900'

export function CognitionEditor({ projectId, chapterId, currentVersion, disabled, pending, onSubmit }: {
  projectId: string; chapterId: string; currentVersion: string | null; disabled: boolean; pending: boolean
  onSubmit: (intent: CognitionIntent) => Promise<void>
}) {
  const [detail, setDetail] = useState<CognitionDetail | null>(null)
  const [draft, setDraft] = useState<CognitionDraft | null>(null)
  const [readError, setReadError] = useState('')
  const [writeError, setWriteError] = useState('')
  const [storageWarning, setStorageWarning] = useState(false)
  const [selection, setSelection] = useState('')
  const [reload, setReload] = useState(0)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let live = true
    const controller = new AbortController()
    setLoading(true); setReadError(''); setSelection('')
    try {
      const saved = readCognitionDraft(projectId, chapterId)
      if (saved) setDraft(saved)
    } catch {
      setLoading(false); setReadError('本地草稿无法读取，请保留本页并核对已保存的认知记录。')
      return () => { live = false; controller.abort() }
    }
    void apiFetch<CognitionDetail>(`/api/projects/${projectId}/cognition/chapters/${chapterId}?include_source=true`,
      { signal: controller.signal }).then(data => {
      if (!live) return
      if (data.chapter_id !== chapterId || !data.can_edit || !data.source_hash || !data.version ||
          typeof data.source_content !== 'string' || !data.source_content.trim() ||
          (data.changes !== null && (!Array.isArray(data.changes) || !data.changes.every(validCognitionChange)))) {
        throw new Error('unavailable edit context')
      }
      const saved = readCognitionDraft(projectId, chapterId)
      setDetail(data); setDraft(saved || newCognitionDraft(data)); setLoading(false)
    }).catch(() => {
      if (live) { setLoading(false); setReadError('无法读取完整保存稿或本地草稿。已有输入会保留，请重新读取后核对。') }
    })
    return () => { live = false; controller.abort() }
  }, [projectId, chapterId, currentVersion, reload])

  const updateDraft = (next: CognitionDraft) => {
    setDraft(next); setWriteError('')
    setStorageWarning(!writeCognitionDraft(projectId, chapterId, next))
  }
  const changeRow = (id: number, fields: Partial<CognitionDraftRow>) => {
    if (draft) updateDraft({ ...draft, rows: draft.rows.map(row => row.id === id ? { ...row, ...fields } : row) })
  }
  const stale = !!detail && !!draft && (detail.source_hash !== draft.source_hash || detail.version !== draft.version)
  const canSubmit = !!detail && !!draft && !loading && !readError && !stale && !disabled && !pending
  const save = () => {
    if (!canSubmit || !draft || !detail?.source_content) return
    const error = validateCognitionDraft(draft, detail.source_content)
    if (error) { setWriteError(error); return }
    // Persist even an unchanged/empty decision, so unknown acknowledgments
    // can be reconciled without discarding later edits.
    updateDraft(draft)
    void onSubmit({ operation: 'edit', chapter_id: chapterId, source_hash: draft.source_hash,
      version: draft.version, changes: changesFromRows(draft.rows) })
  }

  return <section aria-label="本章认知校订" className="space-y-3 border-t border-gray-200 pt-3">
    <p>校订本章人物当时的知情状态。每条保留逐字原句；人物的误信和怀疑不等于故事事实。</p>
    <button className={buttonClass} disabled={loading} onClick={() => setReload(n => n + 1)}>重新读取保存稿与当前记录</button>
    {loading && <p role="status">正在读取完整保存稿…</p>}
    {readError && <p role="alert" className="text-red-700">{readError}</p>}
    {storageWarning && <p role="alert" className="text-amber-800">本地草稿暂时只能保留在当前页面，刷新前请复制校订内容。</p>}
    {draft && !detail && <div className="space-y-2 rounded bg-amber-50 p-3">
      <p>本地校订草稿仍已保留。读取完整保存稿后才能核对和提交。</p>
      {draft.rows.map(row => <p key={row.id} className="whitespace-pre-wrap break-words">
        {row.character === '__reader__' ? '读者' : row.character} · {cognitionKindLabels[row.kind]}：{row.fact}{'\n'}依据原句：{row.evidence}
      </p>)}
    </div>}
    {draft && detail && <>
      {stale && <div className="space-y-2 rounded bg-amber-50 p-2">
        <p>保存稿或认知记录已变化。这份草稿仍保留原版本，请对照下面的当前记录与完整正文后再保存。</p>
        <button className={buttonClass} disabled={loading || !!readError || pending || disabled}
          onClick={() => updateDraft({ ...draft, source_hash: detail.source_hash!, version: detail.version! })}>
          已核对当前记录，沿用这份草稿</button>
      </div>}
      <details>
        <summary className="cursor-pointer text-blue-700">对照当前生效记录</summary>
        {detail.changes === null ? <p>当前没有可用认知记录。</p> : <CognitionChanges changes={detail.changes}
          emptyLabel={detail.source_mode === 'author' ? '作者已确认本章不记录认知变化。' : '模型未提取出明确认知变化。'} />}
      </details>
      <details>
        <summary className="cursor-pointer text-blue-700">对照原模型结果</summary>
        {detail.model_changes === null ? <p>原模型没有可用结果。</p> : <CognitionChanges changes={detail.model_changes} />}
      </details>
      <label className="block space-y-1">
        <span className="font-medium">已保存的完整正文</span>
        <textarea readOnly rows={10} className={`${fieldClass} leading-7`} value={detail.source_content || ''}
          onSelect={event => {
            const target = event.currentTarget
            setSelection(target.value.slice(target.selectionStart, target.selectionEnd))
          }} />
      </label>
      <p className="text-xs text-gray-500">先在正文中选中连续原句，再点条目里的“使用选中的原句”。原句不会自动改写。</p>
      {draft.rows.map((row, index) => <fieldset key={row.id} className="space-y-2 rounded border border-gray-200 p-3">
        <legend className="px-1">第 {index + 1} 条校订</legend>
        <div className="grid gap-2 sm:grid-cols-2">
          <label className="space-y-1"><span>认知主体</span>
            <select className={fieldClass} value={row.character === '__reader__' ? 'reader' : 'character'}
              onChange={event => changeRow(row.id, event.target.value === 'reader'
                ? { character: '__reader__', kind: 'learns' } : { character: '' })}>
              <option value="character">人物</option><option value="reader">读者</option>
            </select>
          </label>
          {row.character !== '__reader__' && <label className="space-y-1"><span>人物称呼</span>
            <input className={fieldClass} value={row.character} onChange={event => changeRow(row.id, { character: event.target.value })} />
          </label>}
          <label className="space-y-1"><span>认知类型</span>
            <select className={fieldClass} value={row.kind} onChange={event => changeRow(row.id, { kind: event.target.value as CognitionKind })}>
              {cognitionKinds.map(kind => <option key={kind} value={kind} disabled={row.character === '__reader__' && kind !== 'learns'}>{cognitionKindLabels[kind]}</option>)}
            </select>
          </label>
        </div>
        <label className="block space-y-1"><span>认知内容</span>
          <textarea rows={2} className={fieldClass} value={row.fact} onChange={event => changeRow(row.id, { fact: event.target.value })} />
        </label>
        <label className="block space-y-1"><span>依据原句</span>
          <textarea rows={3} className={fieldClass} value={row.evidence} onChange={event => changeRow(row.id, { evidence: event.target.value })} />
        </label>
        <div className="flex flex-wrap gap-2">
          <button className={buttonClass} disabled={!selection} onClick={() => changeRow(row.id, { evidence: selection })}>使用选中的原句</button>
          <button className={buttonClass} onClick={() => updateDraft({ ...draft, rows: draft.rows.filter(item => item.id !== row.id) })}>删除这条校订</button>
        </div>
      </fieldset>)}
      <div className="flex flex-wrap gap-2">
        <button className={buttonClass} onClick={() => updateDraft({ ...draft, rows: [...draft.rows, {
          id: draft.rows.reduce((max, row) => Math.max(max, row.id), -1) + 1, character: '', kind: 'learns', fact: '', evidence: '',
        }] })}>新增认知条目</button>
        <button className={buttonClass} disabled={loading || !!readError || disabled || pending}
          onClick={() => updateDraft(newCognitionDraft(detail))}>用当前记录重新开始</button>
      </div>
      {draft.rows.length === 0 && <p className="text-amber-800">保存空校订表示作者确认本章不记录认知变化，后续自动提取不会补回条目。</p>}
      {writeError && <p role="alert" className="text-red-700">{writeError}</p>}
      {pending && <p className="text-amber-800">本章有待确认提交，请在面板上方确认。现在仍可修改草稿。</p>}
      <div className="flex flex-wrap gap-2">
        <button disabled={!canSubmit} className="rounded bg-blue-600 px-3 py-2 text-white disabled:opacity-40" onClick={save}>
          {draft.rows.length ? '保存作者校订' : '确认本章无认知变化'}</button>
        {(detail.source_mode === 'author' || detail.status === 'invalid' || detail.revision_id) &&
          <button className={buttonClass} disabled={!canSubmit} onClick={() => {
            updateDraft(draft)
            void onSubmit({ operation: 'restore_auto', chapter_id: chapterId,
              source_hash: detail.source_hash!, version: detail.version! })
          }}>恢复自动提取</button>}
      </div>
      <p className="text-xs text-gray-500">校订仅适用于当前保存稿。恢复自动提取会使用有效的原模型结果，或重新安排提取；校订历史与未保存草稿仍会保留。</p>
    </>}
  </section>
}
