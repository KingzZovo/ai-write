'use client'

import React, { useState, useEffect, useMemo } from 'react'
import { apiFetch } from '@/lib/api'

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

type Importance = "protagonist" | "key" | "supporting" | "minor"
const IMPORTANCE_LABEL: Record<Importance, string> = {
  protagonist: "主角",
  key: "关键剧情角色",
  supporting: "配角",
  minor: "路人",
}
const IMPORTANCE_ORDER: Importance[] = ["protagonist", "key", "supporting", "minor"]

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

  useEffect(() => {
    if (!projectId) return
    setLoading(true)
    setError(null)
    Promise.all([
      apiFetch<Character[] | CharResp>(`/api/projects/${projectId}/characters`).catch(() => ({ characters: [] } as CharResp)),
      apiFetch<Relationship[] | RelResp>(`/api/projects/${projectId}/relationships`).catch(() => ({ relationships: [] } as RelResp)),
      apiFetch<CharState[] | StateResp>(`/api/projects/${projectId}/character-states`).catch(() => ({ states: [] } as StateResp)),
    ]).then(([c, r, s]) => {
      setChars(Array.isArray(c) ? c : (c.characters || []))
      setRels(Array.isArray(r) ? r : (r.relationships || []))
      setStates(Array.isArray(s) ? s : (s.states || []))
    }).catch(e => setError(String(e?.message || e))).finally(() => setLoading(false))
  }, [projectId])

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
    return out
  }, [chars, relsByChar, statesByChar])

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
                    {identity && <span className="text-[10px] text-stone-500 truncate">{identity}</span>}
                    <span className="ml-auto text-[10px] text-stone-400 flex-shrink-0">{totalRel} 关系 · {st.length} 状态</span>
                    <svg className={`w-3 h-3 text-stone-400 transition-transform flex-shrink-0 ${isOpen ? "rotate-90" : ""}`} viewBox="0 0 12 12">
                      <path d="M4 2l4 4-4 4" stroke="currentColor" strokeWidth="1.5" fill="none" />
                    </svg>
                  </button>
                  {isOpen && (
                    <div className="px-2.5 pb-2.5 space-y-2 text-[11px]">
                      <ProfileBlock fields={merged} />
                      {st.length > 0 && (
                        <div className="border-t border-stone-200 pt-1.5">
                          <div className="text-[10px] font-medium text-stone-500 mb-1">状态变化 ({st.length})</div>
                          <div className="space-y-1">
                            {st.map(s => {
                              const status = (s.status_json || {}) as Record<string, unknown>
                              const desc = Object.entries(status)
                                .filter(([, v]) => v !== "" && v !== null && v !== undefined)
                                .map(([k, v]) => `${FIELD_LABEL[k] || k}: ${typeof v === "string" ? v : JSON.stringify(v)}`)
                                .join(" · ")
                              return (
                                <div key={s.id} className="flex items-baseline gap-2">
                                  <span className="text-stone-400 w-16 flex-shrink-0">第 {s.chapter_start}{s.chapter_end ? `–${s.chapter_end}` : ""} 章</span>
                                  <span className="text-stone-700">{desc || "无具体状态"}</span>
                                </div>
                              )
                            })}
                          </div>
                        </div>
                      )}
                      {totalRel > 0 && (
                        <div className="border-t border-stone-200 pt-1.5">
                          <div className="text-[10px] font-medium text-stone-500 mb-1">人物关系 ({totalRel})</div>
                          <div className="space-y-0.5">
                            {rel.out.map(r => {
                              const t = charById[r.target_id]
                              return <RelationLine key={r.id} from={c.name} to={t?.name || "?"} rel={r} />
                            })}
                            {rel.in.map(r => {
                              const s = charById[r.source_id]
                              return <RelationLine key={r.id} from={s?.name || "?"} to={c.name} rel={r} reverse />
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

function RelationLine({ from, to, rel, reverse }: { from: string; to: string; rel: Relationship; reverse?: boolean }) {
  const sentColor = SENTIMENT_COLOR[rel.sentiment || "neutral"] || "text-stone-500"
  const typeLabel = REL_TYPE_LABEL[rel.rel_type] || rel.rel_type || "关系"
  return (
    <div className="flex items-center gap-1.5 flex-wrap">
      <span className="text-stone-700 font-medium">{from}</span>
      <span className={`text-[10px] ${sentColor}`}>→ {typeLabel}{rel.label ? `(${rel.label})` : ""} →</span>
      <span className="text-stone-700 font-medium">{to}</span>
      {reverse && <span className="text-[9px] text-stone-300">[被动]</span>}
      {rel.note ? <span className="text-stone-400 text-[10px] ml-1 truncate">{rel.note}</span> : null}
    </div>
  )
}
