'use client'

import React, { useState, useEffect, useMemo, useCallback } from 'react'
import { apiFetch } from '@/lib/api'
import { useT } from '@/lib/i18n/I18nProvider'

interface Character {
  id: string
  name: string
  profile_json: Record<string, unknown> | null
}
interface Relationship {
  id: string
  source_id: string
  target_id: string
  rel_type: string
  label?: string
  note?: string
  sentiment?: string
}
interface CharState {
  id: string
  character_id: string
  chapter_start: number
  chapter_end: number | null
  status_json: Record<string, unknown> | null
}
interface CharResp { characters?: Character[]; total?: number }
interface RelResp { relationships?: Relationship[]; total?: number }
interface StateResp { states?: CharState[]; total?: number }

const REL_TYPE_LABEL: Record<string, string> = {
  lover: "恋人", friend: "朋友", enemy: "敌人", family: "家人", mentor: "师徒", mentee: "弟子",
  colleague: "同事", rival: "宿敌", ally: "盟友", subordinate: "下属", superior: "上司",
  parent: "父母", child: "子女", sibling: "兄姊", spouse: "配偶", acquaintance: "熟人",
}
const SENTIMENT_COLOR: Record<string, string> = {
  positive: "text-emerald-600",
  neutral:  "text-stone-500",
  negative: "text-rose-500",
}
// status_json 与 profile_json 的字段名都尝试翻译
const FIELD_LABEL: Record<string, string> = {
  identity: "身份", 身份: "身份",
  personality: "性格", 性格: "性格",
  appearance: "外貌", 外貌: "外貌",
  abilities: "能力", 能力: "能力", 能力等级: "能力等级",
  biography: "小传", 小传: "小传",
  current_status: "当前状态", 状态: "状态",
  background: "背景", 背景: "背景",
  motivation: "动机", 动机: "动机",
  goal: "目标", 目标: "目标",
  情绪: "情绪", emotion: "情绪",
}

export type Importance = "protagonist" | "key" | "supporting" | "minor"
const IMPORTANCE_LABEL: Record<Importance, string> = {
  protagonist: "主角",
  key: "关键剧情角色",
  supporting: "配角",
  minor: "路人",
}
const IMPORTANCE_ORDER: Importance[] = ["protagonist", "key", "supporting", "minor"]

// ---------------------------------------------------------------------------
// Client-only importance overrides (localStorage, keyed by project). Lets the
// author pin a character's tier instead of the relation-count heuristic.
// ---------------------------------------------------------------------------

const OVERRIDE_KEY_PREFIX = "char_importance_override:"

/** Read the author's per-character importance overrides (name → tier).
 *  Corrupted or unknown values are dropped. Exported for tests. */
export function getImportanceOverrides(projectId: string): Record<string, Importance> {
  if (typeof window === "undefined" || !projectId) return {}
  try {
    const raw = window.localStorage.getItem(OVERRIDE_KEY_PREFIX + projectId)
    if (!raw) return {}
    const parsed: unknown = JSON.parse(raw)
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return {}
    const out: Record<string, Importance> = {}
    Object.entries(parsed as Record<string, unknown>).forEach(([name, v]) => {
      if (typeof v === "string" && (IMPORTANCE_ORDER as string[]).includes(v)) out[name] = v as Importance
    })
    return out
  } catch { return {} }
}

/** Set (or clear with null) one character's override; returns the new map. */
export function setImportanceOverride(projectId: string, name: string, imp: Importance | null): Record<string, Importance> {
  const next = { ...getImportanceOverrides(projectId) }
  if (imp === null) delete next[name]
  else next[name] = imp
  if (typeof window !== "undefined" && projectId) {
    const key = OVERRIDE_KEY_PREFIX + projectId
    if (Object.keys(next).length === 0) window.localStorage.removeItem(key)
    else window.localStorage.setItem(key, JSON.stringify(next))
  }
  return next
}

/** Build the PUT /neo4j-settings/relationships body. Relationships are matched
 *  by (source, target, rel_type) names; new_rel_type is sent only when the
 *  type actually changes. Exported for tests. */
export function buildRelationshipUpdatePayload(input: {
  source: string
  target: string
  relType: string
  newRelType?: string
  label?: string
  sentiment?: string
  note?: string
}): Record<string, string> {
  const body: Record<string, string> = {
    source: input.source,
    target: input.target,
    rel_type: input.relType,
  }
  const newType = input.newRelType?.trim()
  if (newType && newType !== input.relType) body.new_rel_type = newType
  if (input.label !== undefined) body.label = input.label
  if (input.sentiment !== undefined) body.sentiment = input.sentiment
  if (input.note !== undefined) body.note = input.note
  return body
}

export function CharacterCardPanel({ projectId }: { projectId: string }) {
  const [chars, setChars] = useState<Character[]>([])
  const [rels, setRels] = useState<Relationship[]>([])
  const [states, setStates] = useState<CharState[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [expanded, setExpanded] = useState<string | null>(null)
  const [search, setSearch] = useState("")
  const [groupBy, setGroupBy] = useState<"importance" | "identity" | "name" | "none">("importance")
  const [hideMinor, setHideMinor] = useState(true)
  const [overrides, setOverrides] = useState<Record<string, Importance>>({})
  const t = useT()

  const load = useCallback((withSpinner: boolean) => {
    if (!projectId) return
    if (withSpinner) setLoading(true)
    setError(null)
    Promise.all([
      apiFetch<Character[] | CharResp>(`/api/projects/${projectId}/characters`).catch(() => ({ characters: [] } as CharResp)),
      apiFetch<Relationship[] | RelResp>(`/api/projects/${projectId}/relationships`).catch(() => ({ relationships: [] } as RelResp)),
      apiFetch<CharState[] | StateResp>(`/api/projects/${projectId}/character-states`).catch(() => ({ states: [] } as StateResp)),
    ]).then(([c, r, s]) => {
      setChars(Array.isArray(c) ? c : (c.characters || []))
      setRels(Array.isArray(r) ? r : (r.relationships || []))
      setStates(Array.isArray(s) ? s : (s.states || []))
    }).catch(e => setError(String(e?.message || e))).finally(() => { if (withSpinner) setLoading(false) })
  }, [projectId])

  useEffect(() => {
    load(true)
    setOverrides(getImportanceOverrides(projectId))
  }, [projectId, load])

  const charById = useMemo(() => Object.fromEntries(chars.map(c => [c.id, c])), [chars])

  const relsByChar = useMemo(() => {
    const out: Record<string, { out: Relationship[]; in: Relationship[] }> = {}
    rels.forEach(r => {
      out[r.source_id] = out[r.source_id] || { out: [], in: [] }
      out[r.target_id] = out[r.target_id] || { out: [], in: [] }
      out[r.source_id].out.push(r)
      out[r.target_id].in.push(r)
    })
    return out
  }, [rels])

  const statesByChar = useMemo(() => {
    const out: Record<string, CharState[]> = {}
    states.forEach(s => { (out[s.character_id] = out[s.character_id] || []).push(s) })
    Object.values(out).forEach(arr => arr.sort((a, b) => a.chapter_start - b.chapter_start))
    return out
  }, [states])

  // 启发式分类：基于关系数 + 状态数 + 章节跨度
  const importanceById = useMemo(() => {
    const scores: Array<{ id: string; score: number; relCnt: number; stateCnt: number }> = chars.map(c => {
      const rel = relsByChar[c.id] || { out: [], in: [] }
      const st = statesByChar[c.id] || []
      const relCnt = rel.out.length + rel.in.length
      const stateCnt = st.length
      // 关系权重高（社交网络中心 = 主角）；状态记录是出场频率
      const score = relCnt * 3 + stateCnt * 1
      return { id: c.id, score, relCnt, stateCnt }
    })
    scores.sort((a, b) => b.score - a.score)
    const total = scores.length
    const topProtagonist = Math.min(5, Math.max(1, Math.floor(total * 0.06))) // 大约 5-6%
    const topSupporting = Math.min(20, Math.max(3, Math.floor(total * 0.30))) // 约 30%
    const out: Record<string, Importance> = {}
    scores.forEach((s, idx) => {
      if (idx < topProtagonist && s.score >= 4) out[s.id] = "protagonist"
      else if (s.relCnt === 0 && s.stateCnt === 0) out[s.id] = "minor"
      else if (s.stateCnt > 0 && s.relCnt < 2) out[s.id] = "key" // 有状态记录但社交少 = 关键剧情人物
      else if (idx < topProtagonist + topSupporting) out[s.id] = "supporting"
      else out[s.id] = "minor"
    })
    // 作者手动覆盖（仅本浏览器，按角色名）优先于启发式
    chars.forEach(c => { const o = overrides[c.name]; if (o) out[c.id] = o })
    return out
  }, [chars, relsByChar, statesByChar, overrides])

  // 把 profile_json + 最新一条 status_json 合并成 "档案"
  const profileMerged = useMemo(() => {
    const out: Record<string, Record<string, string>> = {}
    chars.forEach(c => {
      const pj = (c.profile_json || {}) as Record<string, unknown>
      const sts = statesByChar[c.id] || []
      const last = sts[sts.length - 1]?.status_json || {}
      const merged: Record<string, string> = {}
      const collect = (src: Record<string, unknown>) => {
        Object.entries(src).forEach(([k, v]) => {
          if (typeof v === "string" && v.length > 0 && !merged[k]) merged[k] = v
        })
      }
      collect(pj)
      collect(last as Record<string, unknown>)
      out[c.id] = merged
    })
    return out
  }, [chars, statesByChar])

  const filtered = useMemo(() => {
    let list = chars
    if (hideMinor) list = list.filter(c => importanceById[c.id] !== "minor")
    const q = search.trim().toLowerCase()
    if (q) {
      list = list.filter(c =>
        c.name.toLowerCase().includes(q) ||
        JSON.stringify(profileMerged[c.id] || {}).toLowerCase().includes(q)
      )
    }
    return list
  }, [chars, search, hideMinor, importanceById, profileMerged])

  const grouped = useMemo(() => {
    const map: Record<string, Character[]> = {}
    if (groupBy === "none") {
      map["全部"] = [...filtered]
    } else if (groupBy === "importance") {
      filtered.forEach(c => {
        const imp = importanceById[c.id] || "minor"
        const key = IMPORTANCE_LABEL[imp]
        ;(map[key] = map[key] || []).push(c)
      })
    } else {
      filtered.forEach(c => {
        let key: string
        if (groupBy === "identity") {
          const merged = profileMerged[c.id] || {}
          key = merged.identity || merged.身份 || "未分类"
        } else {
          key = c.name[0] || "未"
        }
        ;(map[key] = map[key] || []).push(c)
      })
    }
    return map
  }, [filtered, groupBy, importanceById, profileMerged])

  const groupOrder = useMemo(() => {
    const keys = Object.keys(grouped)
    if (groupBy === "importance") {
      return IMPORTANCE_ORDER.map(o => IMPORTANCE_LABEL[o]).filter(k => keys.includes(k))
    }
    return keys.sort((a, b) => grouped[b].length - grouped[a].length)
  }, [grouped, groupBy])

  if (loading) return <p className="text-xs text-stone-400 px-2 py-3">加载人物卡...</p>
  if (error) return <p className="text-xs text-rose-500 px-2 py-3">加载失败：{error}</p>
  if (chars.length === 0) return <p className="text-xs text-stone-400 px-2 py-3">暂无人物。开始写作 + 运行抽取后会自动出现。</p>

  const minorCount = chars.filter(c => importanceById[c.id] === "minor").length

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <h3 className="text-sm font-semibold text-stone-900">人物卡 / 角色关系</h3>
        <span className="text-[10px] text-stone-400">{chars.length} 人 · {rels.length} 关系 · {states.length} 状态</span>
      </div>

      <div className="flex items-center gap-2 flex-wrap">
        <input value={search} onChange={e => setSearch(e.target.value)} placeholder="搜索姓名 / 身份 / 描述..." className="flex-1 min-w-[140px] px-2 py-1 text-xs border border-stone-200 rounded" />
        <select value={groupBy} onChange={e => setGroupBy(e.target.value as typeof groupBy)} className="text-xs border border-stone-200 rounded px-1.5 py-1">
          <option value="importance">按重要程度</option>
          <option value="identity">按身份</option>
          <option value="name">按姓氏</option>
          <option value="none">不分组</option>
        </select>
        <label className="text-[11px] text-stone-600 flex items-center gap-1 cursor-pointer">
          <input type="checkbox" checked={hideMinor} onChange={e => setHideMinor(e.target.checked)} className="w-3 h-3" />
          隐藏路人 ({minorCount})
        </label>
      </div>

      {groupOrder.map(group => {
        const list = grouped[group] || []
        if (list.length === 0) return null
        return (
          <div key={group} className="space-y-1.5">
            <h4 className="text-[10px] font-medium text-stone-500 uppercase tracking-wider sticky top-0 bg-white py-0.5 z-10">
              {group} <span className="text-stone-300">({list.length})</span>
            </h4>
            {list.map(c => {
              const isOpen = expanded === c.id
              const rel = relsByChar[c.id] || { out: [], in: [] }
              const st = statesByChar[c.id] || []
              const merged = profileMerged[c.id] || {}
              const identity = merged.identity || merged.身份 || ""
              const totalRel = rel.out.length + rel.in.length
              const imp = importanceById[c.id] || "minor"
              return (
                <div key={c.id} className={`border rounded-lg transition-colors ${isOpen ? "border-blue-300 bg-blue-50/40" : "border-stone-200 bg-white hover:border-stone-300"}`}>
                  <button onClick={() => setExpanded(isOpen ? null : c.id)} className="w-full text-left px-2.5 py-2 flex items-center gap-2">
                    <span className="text-sm font-medium text-stone-800">{c.name}</span>
                    {imp === "protagonist" && <span className="text-[9px] px-1 rounded bg-amber-100 text-amber-700">主</span>}
                    {imp === "key" && <span className="text-[9px] px-1 rounded bg-purple-100 text-purple-700">关键</span>}
                    {isGenericName(c.name) && <span className="text-[9px] px-1 rounded bg-amber-100 text-amber-700" title="通用角色名，可能合并了多个实例。抽取需加场景修饰词区分。">⚠ 通用</span>}
                    {identity && <span className="text-[10px] text-stone-500 truncate">{identity}</span>}
                    <span className="ml-auto text-[10px] text-stone-400 flex-shrink-0">{totalRel} 关系 · {st.length} 状态</span>
                    <svg className={`w-3 h-3 text-stone-400 transition-transform flex-shrink-0 ${isOpen ? "rotate-90" : ""}`} viewBox="0 0 12 12">
                      <path d="M4 2l4 4-4 4" stroke="currentColor" strokeWidth="1.5" fill="none" />
                    </svg>
                  </button>
                  {isOpen && (
                    <div className="px-2.5 pb-2.5 space-y-2 text-[11px]">
                      <div className="flex items-center gap-1.5" title={t('charCard.importance.localHint')}>
                        <span className="text-[10px] text-stone-400">{t('charCard.importance.label')}</span>
                        <select
                          value={overrides[c.name] ?? "auto"}
                          onChange={e => {
                            const v = e.target.value
                            setOverrides(setImportanceOverride(projectId, c.name, v === "auto" ? null : (v as Importance)))
                          }}
                          className="text-[10px] border border-stone-200 rounded px-1 py-0.5"
                        >
                          <option value="auto">{t('charCard.importance.auto')}</option>
                          {IMPORTANCE_ORDER.map(o => <option key={o} value={o}>{IMPORTANCE_LABEL[o]}</option>)}
                        </select>
                        <span className="text-[9px] text-stone-300">{t('charCard.importance.localHint')}</span>
                      </div>
                      <ProfileBlock fields={merged} />
                      {(() => {
                        const stClean = dedupeStates(st)
                        return stClean.length > 0 && (
                        <div className="border-t border-stone-200 pt-1.5">
                          <div className="text-[10px] font-medium text-stone-500 mb-1">状态变化 ({stClean.length}{stClean.length !== st.length ? ` / 合并自 ${st.length}` : ""})</div>
                          <div className="space-y-1">
                            {stClean.map(s => {
                              const status = (s.status_json || {}) as Record<string, unknown>
                              const desc = Object.entries(status)
                                .filter(([, v]) => v !== "" && v !== null && v !== undefined)
                                .map(([k, v]) => `${FIELD_LABEL[k] || k}: ${typeof v === "string" ? v : JSON.stringify(v)}`)
                                .join(" · ")
                              return (
                                <div key={s.id} className="flex items-baseline gap-2">
                                  <span className="text-stone-400 w-16 flex-shrink-0">{s.chapter_start === 0 ? (s.chapter_end ? `初始–${s.chapter_end}章` : "初始") : `第 ${s.chapter_start}${s.chapter_end && s.chapter_end !== s.chapter_start ? `–${s.chapter_end}` : ""} 章`}</span>
                                  <span className="text-stone-700">{desc || "无具体状态"}</span>
                                </div>
                              )
                            })}
                          </div>
                        </div>
                        )
                      })()}
                      {totalRel > 0 && (
                        <div className="border-t border-stone-200 pt-1.5">
                          <div className="text-[10px] font-medium text-stone-500 mb-1">人物关系 ({totalRel})</div>
                          <div className="space-y-0.5">
                            {rel.out.map(r => {
                              const target = charById[r.target_id]
                              return <RelationLine key={r.id} from={c.name} to={target?.name || "?"} rel={r} projectId={projectId} onChanged={() => load(false)} />
                            })}
                            {rel.in.map(r => {
                              const s = charById[r.source_id]
                              return <RelationLine key={r.id} from={s?.name || "?"} to={c.name} rel={r} reverse projectId={projectId} onChanged={() => load(false)} />
                            })}
                          </div>
                        </div>
                      )}
                      {totalRel === 0 && st.length === 0 && Object.keys(merged).length === 0 && (
                        <div className="text-stone-400 text-[10px] pt-1">该人物还没有详细信息。运行一次全量抽取可以补齐。</div>
                      )}
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        )
      })}
    </div>
  )
}

function ProfileBlock({ fields }: { fields: Record<string, string> }) {
  const list = Object.entries(fields)
  if (list.length === 0) return null
  return (
    <div className="grid grid-cols-1 gap-1 pt-1 border-t border-stone-200">
      {list.map(([k, v]) => (
        <div key={k}>
          <span className="text-stone-400 mr-1.5">{FIELD_LABEL[k] || k}:</span>
          <span className="text-stone-700">{v}</span>
        </div>
      ))}
    </div>
  )
}

function RelationLine({ from, to, rel, reverse, projectId, onChanged }: { from: string; to: string; rel: Relationship; reverse?: boolean; projectId: string; onChanged: () => void }) {
  const t = useT()
  const [editing, setEditing] = useState(false)
  const [busy, setBusy] = useState(false)
  const sentColor = SENTIMENT_COLOR[rel.sentiment || "neutral"] || "text-stone-500"
  const typeLabel = REL_TYPE_LABEL[rel.rel_type] || rel.rel_type || "关系"

  // Relationship writes match Neo4j edges by (source, target, rel_type) names.
  const handleDelete = async () => {
    if (!window.confirm(t('charCard.rel.deleteConfirm'))) return
    setBusy(true)
    try {
      await apiFetch(`/api/projects/${projectId}/neo4j-settings/relationships`, {
        method: 'DELETE',
        body: JSON.stringify({ source: from, target: to, rel_type: rel.rel_type }),
      })
      onChanged()
    } catch (e) {
      window.alert(e instanceof Error ? e.message : String(e))
    } finally { setBusy(false) }
  }

  return (
    <div>
      <div className="flex items-center gap-1.5 flex-wrap">
        <span className="text-stone-700 font-medium">{from}</span>
        <span className={`text-[10px] ${sentColor}`}>→ {typeLabel}{rel.label ? `(${rel.label})` : ""} →</span>
        <span className="text-stone-700 font-medium">{to}</span>
        {reverse && <span className="text-[9px] text-stone-300">[被动]</span>}
        {rel.note ? <span className="text-stone-400 text-[10px] ml-1 truncate">{rel.note}</span> : null}
        <span className="ml-auto flex items-center gap-1.5 flex-shrink-0">
          <button onClick={() => setEditing(!editing)} disabled={busy} className="text-[10px] text-blue-500 hover:text-blue-600 disabled:opacity-50">{t('common.edit')}</button>
          <button onClick={handleDelete} disabled={busy} className="text-[10px] text-rose-400 hover:text-rose-500 disabled:opacity-50">{t('common.delete')}</button>
        </span>
      </div>
      {editing && (
        <RelationEditForm
          from={from}
          to={to}
          rel={rel}
          projectId={projectId}
          onDone={() => { setEditing(false); onChanged() }}
          onCancel={() => setEditing(false)}
        />
      )}
    </div>
  )
}

function RelationEditForm({ from, to, rel, projectId, onDone, onCancel }: { from: string; to: string; rel: Relationship; projectId: string; onDone: () => void; onCancel: () => void }) {
  const t = useT()
  const [relType, setRelType] = useState(rel.rel_type)
  const [label, setLabel] = useState(rel.label || "")
  const [sentiment, setSentiment] = useState(rel.sentiment || "")
  const [note, setNote] = useState(rel.note || "")
  const [saving, setSaving] = useState(false)

  const handleSave = async () => {
    if (!relType.trim()) return
    setSaving(true)
    try {
      await apiFetch(`/api/projects/${projectId}/neo4j-settings/relationships`, {
        method: 'PUT',
        body: JSON.stringify(buildRelationshipUpdatePayload({
          source: from,
          target: to,
          relType: rel.rel_type,
          newRelType: relType,
          label,
          sentiment,
          note,
        })),
      })
      onDone()
    } catch (e) {
      window.alert(e instanceof Error ? e.message : String(e))
    } finally { setSaving(false) }
  }

  return (
    <div className="mt-1 mb-1.5 bg-stone-50 border border-stone-200 rounded p-2 space-y-1.5">
      <div className="grid grid-cols-2 gap-1.5">
        <div>
          <label className="text-[10px] text-stone-400">{t('charCard.rel.type')}</label>
          <input value={relType} onChange={e => setRelType(e.target.value)} list={`rel-type-options-${rel.id}`} className="w-full px-1.5 py-0.5 text-[11px] border border-stone-200 rounded" />
          <datalist id={`rel-type-options-${rel.id}`}>
            {Object.entries(REL_TYPE_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </datalist>
        </div>
        <div>
          <label className="text-[10px] text-stone-400">{t('charCard.rel.sentiment')}</label>
          <select value={sentiment} onChange={e => setSentiment(e.target.value)} className="w-full px-1.5 py-0.5 text-[11px] border border-stone-200 rounded">
            <option value="">{t('charCard.sentiment.none')}</option>
            <option value="positive">{t('charCard.sentiment.positive')}</option>
            <option value="neutral">{t('charCard.sentiment.neutral')}</option>
            <option value="negative">{t('charCard.sentiment.negative')}</option>
          </select>
        </div>
      </div>
      <div>
        <label className="text-[10px] text-stone-400">{t('charCard.rel.label')}</label>
        <input value={label} onChange={e => setLabel(e.target.value)} className="w-full px-1.5 py-0.5 text-[11px] border border-stone-200 rounded" />
      </div>
      <div>
        <label className="text-[10px] text-stone-400">{t('charCard.rel.note')}</label>
        <input value={note} onChange={e => setNote(e.target.value)} className="w-full px-1.5 py-0.5 text-[11px] border border-stone-200 rounded" />
      </div>
      <div className="flex gap-1.5">
        <button onClick={handleSave} disabled={saving || !relType.trim()} className="flex-1 px-2 py-1 text-[11px] bg-blue-600 text-white rounded hover:bg-blue-700 disabled:opacity-50">{saving ? t('common.saving') : t('common.save')}</button>
        <button onClick={onCancel} disabled={saving} className="flex-1 px-2 py-1 text-[11px] bg-stone-200 text-stone-600 rounded disabled:opacity-50">{t('common.cancel')}</button>
      </div>
    </div>
  )
}

// 相邻 status_json 完全相同的状态合并为一条，范围拓展到最大 chapter_end
function dedupeStates(states: CharState[]): CharState[] {
  if (states.length <= 1) return states
  const out: CharState[] = []
  for (const s of states) {
    const last = out[out.length - 1]
    if (last && JSON.stringify(last.status_json || {}) === JSON.stringify(s.status_json || {})) {
      // 合并：拓展 chapter_end
      const lastEnd = last.chapter_end ?? last.chapter_start
      const sEnd = s.chapter_end ?? s.chapter_start
      last.chapter_end = Math.max(lastEnd, sEnd)
    } else {
      out.push({ ...s, status_json: s.status_json })
    }
  }
  return out
}

// 检测否为“通用角色名”（可能多实例被合并）
function isGenericName(name: string): boolean {
  if (!name) return false
  // 有姓名特征：双字+常见姓压、三字全中文、带干、老/小/阿前缀且 ≤ 3 字
  // 通用词柄：以专业职业/身份为主体
  const generic = /司机|护士|女人|学姐|学长|学生|路人|孩子|女孩|男孩|老人|交警|警官|保安|院监|院长|老板|货车|送货|外卖|研究员|青年|中年人|女生|男生|服务生|销售|中年男人|中年女人|抱孩子的|值班/
  return generic.test(name)
}
