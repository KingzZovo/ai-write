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
  lover: '恋人', friend: '朋友', enemy: '敌人', family: '家人', mentor: '师徒', mentee: '弟子',
  colleague: '同事', rival: '宿敌', ally: '盟友', subordinate: '下属', superior: '上司',
  parent: '父母', child: '子女', sibling: '兄姊', spouse: '配偶', acquaintance: '熟人',
}
const SENTIMENT_COLOR: Record<string, string> = {
  positive: 'text-emerald-600',
  neutral:  'text-stone-500',
  negative: 'text-rose-500',
}
const PROFILE_LABEL: Record<string, string> = {
  identity: '身份', personality: '性格', appearance: '外貌',
  abilities: '能力', biography: '小传', current_status: '当前状态',
  background: '背景', motivation: '动机', goal: '目标',
}

export function CharacterCardPanel({ projectId }: { projectId: string }) {
  const [chars, setChars] = useState<Character[]>([])
  const [rels, setRels] = useState<Relationship[]>([])
  const [states, setStates] = useState<CharState[]>([])
  const [loading, setLoading] = useState(false)
  const [expanded, setExpanded] = useState<string | null>(null)
  const [search, setSearch] = useState('')
  const [groupBy, setGroupBy] = useState<'identity' | 'name' | 'none'>('identity')

  useEffect(() => {
    if (!projectId) return
    setLoading(true)
    Promise.all([
      apiFetch<Character[] | CharResp>(`/api/projects/${projectId}/characters`).catch(() => ({ characters: [] } as CharResp)),
      apiFetch<Relationship[] | RelResp>(`/api/projects/${projectId}/relationships`).catch(() => ({ relationships: [] } as RelResp)),
      apiFetch<CharState[] | StateResp>(`/api/projects/${projectId}/character-states`).catch(() => ({ states: [] } as StateResp)),
    ]).then(([c, r, s]) => {
      setChars(Array.isArray(c) ? c : (c.characters || []))
      setRels(Array.isArray(r) ? r : (r.relationships || []))
      setStates(Array.isArray(s) ? s : (s.states || []))
    }).finally(() => setLoading(false))
  }, [projectId])

  const charById = useMemo(() => Object.fromEntries(chars.map(c => [c.id, c])), [chars])

  const filtered = useMemo(() => {
    if (!search.trim()) return chars
    const q = search.toLowerCase()
    return chars.filter(c =>
      c.name.toLowerCase().includes(q) ||
      JSON.stringify(c.profile_json || {}).toLowerCase().includes(q)
    )
  }, [chars, search])

  const grouped = useMemo(() => {
    const map: Record<string, Character[]> = {}
    if (groupBy === 'none') {
      map['全部'] = [...filtered]
    } else {
      filtered.forEach(c => {
        let key: string
        if (groupBy === 'identity') {
          const id = (c.profile_json as Record<string, unknown> | null)?.identity
          key = (typeof id === 'string' && id) ? id : '未分类'
        } else {
          key = c.name[0] || '未'
        }
        ;(map[key] = map[key] || []).push(c)
      })
    }
    return map
  }, [filtered, groupBy])

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

  if (loading) return <p className="text-xs text-stone-400">加载人物卡...</p>
  if (chars.length === 0) return <p className="text-xs text-stone-400">暂无人物。开始写作 + 运行抽取后会自动出现。</p>

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <h3 className="text-sm font-semibold text-stone-900">人物卡 / 角色关系</h3>
        <span className="text-[10px] text-stone-400">{chars.length} 人 · {rels.length} 关系 · {states.length} 状态记录</span>
      </div>

      <div className="flex items-center gap-2">
        <input value={search} onChange={e => setSearch(e.target.value)} placeholder="搜索姓名 / 身份 / 描述..." className="flex-1 px-2 py-1 text-xs border border-stone-200 rounded" />
        <select value={groupBy} onChange={e => setGroupBy(e.target.value as typeof groupBy)} className="text-xs border border-stone-200 rounded px-1.5 py-1">
          <option value="identity">按身份</option>
          <option value="name">按姓氏</option>
          <option value="none">不分组</option>
        </select>
      </div>

      {Object.entries(grouped)
        .sort((a, b) => b[1].length - a[1].length)
        .map(([group, list]) => (
          <div key={group} className="space-y-1.5">
            <h4 className="text-[10px] font-medium text-stone-500 uppercase tracking-wider sticky top-0 bg-white py-0.5 z-10">
              {group} <span className="text-stone-300">({list.length})</span>
            </h4>
            {list.map(c => {
              const isOpen = expanded === c.id
              const rel = relsByChar[c.id] || { out: [], in: [] }
              const st = statesByChar[c.id] || []
              const profile = (c.profile_json || {}) as Record<string, unknown>
              const identity = typeof profile.identity === 'string' ? profile.identity : ''
              const totalRel = rel.out.length + rel.in.length
              return (
                <div key={c.id} className={`border rounded-lg transition-colors ${isOpen ? 'border-blue-300 bg-blue-50/40' : 'border-stone-200 bg-white hover:border-stone-300'}`}>
                  <button onClick={() => setExpanded(isOpen ? null : c.id)} className="w-full text-left px-2.5 py-2 flex items-center gap-2">
                    <span className="text-sm font-medium text-stone-800">{c.name}</span>
                    {identity && <span className="text-[10px] text-stone-500 truncate">{identity}</span>}
                    <span className="ml-auto text-[10px] text-stone-400 flex-shrink-0">{totalRel} 关系 · {st.length} 状态</span>
                    <svg className={`w-3 h-3 text-stone-400 transition-transform flex-shrink-0 ${isOpen ? 'rotate-90' : ''}`} viewBox="0 0 12 12">
                      <path d="M4 2l4 4-4 4" stroke="currentColor" strokeWidth="1.5" fill="none" />
                    </svg>
                  </button>
                  {isOpen && (
                    <div className="px-2.5 pb-2.5 space-y-2 text-[11px]">
                      {/* Profile fields */}
                      <ProfileBlock profile={profile} />
                      {/* States timeline */}
                      {st.length > 0 && (
                        <div className="border-t border-stone-200 pt-1.5">
                          <div className="text-[10px] font-medium text-stone-500 mb-1">状态变化 ({st.length})</div>
                          <div className="space-y-1">
                            {st.map(s => {
                              const status = (s.status_json || {}) as Record<string, unknown>
                              const desc = Object.entries(status)
                                .filter(([, v]) => v !== '' && v !== null && v !== undefined)
                                .map(([k, v]) => `${k}: ${typeof v === 'string' ? v : JSON.stringify(v)}`)
                                .join(' · ')
                              return (
                                <div key={s.id} className="flex items-baseline gap-2">
                                  <span className="text-stone-400 w-14 flex-shrink-0">第 {s.chapter_start}{s.chapter_end ? `–${s.chapter_end}` : ''} 章</span>
                                  <span className="text-stone-700">{desc || '无具体状态'}</span>
                                </div>
                              )
                            })}
                          </div>
                        </div>
                      )}
                      {/* Relationships */}
                      {totalRel > 0 && (
                        <div className="border-t border-stone-200 pt-1.5">
                          <div className="text-[10px] font-medium text-stone-500 mb-1">人物关系 ({totalRel})</div>
                          <div className="space-y-0.5">
                            {rel.out.map(r => {
                              const t = charById[r.target_id]
                              return <RelationLine key={r.id} from={c.name} to={t?.name || '?'} rel={r} />
                            })}
                            {rel.in.map(r => {
                              const s = charById[r.source_id]
                              return <RelationLine key={r.id} from={s?.name || '?'} to={c.name} rel={r} reverse />
                            })}
                          </div>
                        </div>
                      )}
                      {totalRel === 0 && st.length === 0 && Object.keys(profile).length === 0 && (
                        <div className="text-stone-400 text-[10px] pt-1">该人物还没有详细信息。运行一次全量抽取可以补齐。</div>
                      )}
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        ))}
    </div>
  )
}

function ProfileBlock({ profile }: { profile: Record<string, unknown> }) {
  const fields = Object.entries(profile).filter(([k, v]) => typeof v === 'string' && (v as string).length > 0)
  if (fields.length === 0) return null
  return (
    <div className="grid grid-cols-1 gap-1 pt-1 border-t border-stone-200">
      {fields.map(([k, v]) => (
        <div key={k}>
          <span className="text-stone-400 mr-1.5">{PROFILE_LABEL[k] || k}:</span>
          <span className="text-stone-700">{v as string}</span>
        </div>
      ))}
    </div>
  )
}

function RelationLine({ from, to, rel, reverse }: { from: string; to: string; rel: Relationship; reverse?: boolean }) {
  const sentColor = SENTIMENT_COLOR[rel.sentiment || 'neutral'] || 'text-stone-500'
  const typeLabel = REL_TYPE_LABEL[rel.rel_type] || rel.rel_type || '关系'
  return (
    <div className="flex items-center gap-1.5">
      <span className="text-stone-700 font-medium">{from}</span>
      <span className={`text-[10px] ${sentColor}`}>→ {typeLabel}{rel.label ? `(${rel.label})` : ''} →</span>
      <span className="text-stone-700 font-medium">{to}</span>
      {reverse && <span className="text-[9px] text-stone-300">[被动]</span>}
      {rel.note ? <span className="text-stone-400 text-[10px] ml-1 truncate">{rel.note}</span> : null}
    </div>
  )
}
