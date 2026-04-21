'use client'

import React, { useCallback, useEffect, useState } from 'react'
import { apiFetch } from '@/lib/api'

// =========================================================================
// Types (mirror backend app/api/writing_engine.py)
// =========================================================================

interface WritingRule {
  id: string
  genre: string
  category: string
  title: string
  rule_text: string
  examples_json: Array<Record<string, unknown>> | null
  priority: number
  is_active: boolean
}
interface BeatPattern {
  id: string
  genre: string
  stage: string
  title: string
  description: string
  trigger_conditions_json: Record<string, unknown> | null
  reusable: boolean
  is_active: boolean
}
interface AntiAITrap {
  id: string
  locale: string
  pattern_type: 'keyword' | 'regex' | 'ngram'
  pattern: string
  severity: 'hard' | 'soft'
  replacement_hint: string
  is_active: boolean
}
interface GenreProfile {
  id: string
  code: string
  name: string
  description: string
  default_beat_pattern_ids: string[] | null
  default_writing_rule_ids: string[] | null
  is_active: boolean
}
interface ToolSpec {
  id: string
  name: string
  description: string
  input_schema_json: Record<string, unknown> | null
  output_schema_json: Record<string, unknown> | null
  handler: 'python_callable' | 'sql' | 'qdrant' | 'llm'
  config_json: Record<string, unknown> | null
  is_active: boolean
}

type Tab = 'rules' | 'beats' | 'traps' | 'genres' | 'tools'

const TABS: { key: Tab; label: string; path: string }[] = [
  { key: 'rules', label: '写作规则', path: '/api/writing-rules' },
  { key: 'beats', label: 'Beat 节奏', path: '/api/beat-patterns' },
  { key: 'traps', label: '反 AI 陷阱', path: '/api/anti-ai-traps' },
  { key: 'genres', label: '题材画像', path: '/api/genre-profiles' },
  { key: 'tools', label: 'Agent 工具', path: '/api/tool-specs' },
]

// =========================================================================
// Page
// =========================================================================

export default function WritingEnginePage() {
  const [activeTab, setActiveTab] = useState<Tab>('rules')

  return (
    <div className="max-w-5xl mx-auto">
      <div className="mb-4">
        <h2 className="text-xl font-semibold text-gray-900">写作引擎 (v0.8)</h2>
        <p className="text-sm text-gray-500 mt-1">
          管理写作规则、beat 节奏模板、反-AI-味陷阱、题材画像与 Agent 工具。
        </p>
      </div>

      <div className="flex gap-1 border-b border-gray-200 mb-4 overflow-x-auto">
        {TABS.map((t) => (
          <button
            key={t.key}
            onClick={() => setActiveTab(t.key)}
            className={`px-4 py-2.5 text-sm font-medium border-b-2 transition-colors whitespace-nowrap ${
              activeTab === t.key
                ? 'border-blue-600 text-blue-600'
                : 'border-transparent text-gray-500 hover:text-gray-700 hover:border-gray-300'
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {activeTab === 'rules' && <RulesTab />}
      {activeTab === 'beats' && <BeatsTab />}
      {activeTab === 'traps' && <TrapsTab />}
      {activeTab === 'genres' && <GenresTab />}
      {activeTab === 'tools' && <ToolsTab />}
    </div>
  )
}

// =========================================================================
// Shared helpers
// =========================================================================

function useResource<T extends { id: string; is_active: boolean }>(path: string) {
  const [items, setItems] = useState<T[]>([])
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const reload = useCallback(async () => {
    setLoading(true)
    setErr(null)
    try {
      const data = await apiFetch<T[]>(path)
      setItems(data)
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [path])

  useEffect(() => {
    reload()
  }, [reload])

  const toggle = useCallback(
    async (id: string) => {
      await apiFetch(`${path}/${id}/toggle`, { method: 'POST' })
      reload()
    },
    [path, reload],
  )

  const remove = useCallback(
    async (id: string) => {
      if (!confirm('确定删除？此操作不可撤销')) return
      await apiFetch(`${path}/${id}`, { method: 'DELETE' })
      reload()
    },
    [path, reload],
  )

  const create = useCallback(
    async (body: Record<string, unknown>) => {
      await apiFetch(path, { method: 'POST', body: JSON.stringify(body) })
      reload()
    },
    [path, reload],
  )

  const update = useCallback(
    async (id: string, body: Record<string, unknown>) => {
      await apiFetch(`${path}/${id}`, { method: 'PUT', body: JSON.stringify(body) })
      reload()
    },
    [path, reload],
  )

  return { items, loading, err, reload, toggle, remove, create, update }
}

function Row(props: {
  title: string
  subtitle?: string
  active: boolean
  onToggle: () => void
  onEdit: () => void
  onDelete: () => void
}) {
  return (
    <div className="flex items-center justify-between bg-white border border-gray-200 rounded px-4 py-3">
      <div className="min-w-0 flex-1">
        <div className="text-sm font-medium text-gray-900 truncate">{props.title}</div>
        {props.subtitle && (
          <div className="text-xs text-gray-500 mt-0.5 line-clamp-2">{props.subtitle}</div>
        )}
      </div>
      <div className="flex items-center gap-2 ml-4 shrink-0">
        <button
          onClick={props.onToggle}
          className={`text-xs px-2 py-1 rounded border ${
            props.active
              ? 'bg-green-50 text-green-700 border-green-200'
              : 'bg-gray-50 text-gray-500 border-gray-200'
          }`}
        >
          {props.active ? '启用中' : '已停用'}
        </button>
        <button
          onClick={props.onEdit}
          className="text-xs px-2 py-1 rounded border border-gray-200 text-gray-600 hover:bg-gray-50"
        >
          编辑
        </button>
        <button
          onClick={props.onDelete}
          className="text-xs px-2 py-1 rounded border border-red-200 text-red-600 hover:bg-red-50"
        >
          删除
        </button>
      </div>
    </div>
  )
}

function Drawer(props: {
  open: boolean
  title: string
  onClose: () => void
  onSave: () => void
  children: React.ReactNode
}) {
  if (!props.open) return null
  return (
    <div className="fixed inset-0 z-40 flex">
      <div className="flex-1 bg-black/30" onClick={props.onClose} />
      <div className="w-full max-w-md bg-white shadow-xl p-5 overflow-y-auto">
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-base font-semibold">{props.title}</h3>
          <button onClick={props.onClose} className="text-gray-400 hover:text-gray-600">
            ✕
          </button>
        </div>
        <div className="space-y-3">{props.children}</div>
        <div className="mt-6 flex justify-end gap-2">
          <button
            onClick={props.onClose}
            className="text-sm px-4 py-2 rounded border border-gray-200 text-gray-700 hover:bg-gray-50"
          >
            取消
          </button>
          <button
            onClick={props.onSave}
            className="text-sm px-4 py-2 rounded bg-blue-600 text-white hover:bg-blue-700"
          >
            保存
          </button>
        </div>
      </div>
    </div>
  )
}

function Field(props: {
  label: string
  value: string
  onChange: (v: string) => void
  placeholder?: string
  multiline?: boolean
}) {
  return (
    <label className="block">
      <span className="text-xs text-gray-600">{props.label}</span>
      {props.multiline ? (
        <textarea
          className="mt-1 w-full border border-gray-300 rounded px-2 py-1.5 text-sm min-h-24"
          value={props.value}
          placeholder={props.placeholder}
          onChange={(e) => props.onChange(e.target.value)}
        />
      ) : (
        <input
          className="mt-1 w-full border border-gray-300 rounded px-2 py-1.5 text-sm"
          value={props.value}
          placeholder={props.placeholder}
          onChange={(e) => props.onChange(e.target.value)}
        />
      )}
    </label>
  )
}

// =========================================================================
// Tabs
// =========================================================================

function RulesTab() {
  const { items, loading, err, toggle, remove, create, update } =
    useResource<WritingRule>('/api/writing-rules')
  const [drawer, setDrawer] = useState<WritingRule | null>(null)
  const [creating, setCreating] = useState<Partial<WritingRule> | null>(null)

  return (
    <div>
      <div className="flex justify-end mb-3">
        <button
          onClick={() =>
            setCreating({ genre: '', category: 'pacing', title: '', rule_text: '', priority: 50, is_active: true })
          }
          className="text-sm px-3 py-1.5 rounded bg-blue-600 text-white hover:bg-blue-700"
        >
          新建规则
        </button>
      </div>
      {err && <div className="text-sm text-red-600 mb-2">{err}</div>}
      {loading ? (
        <div className="text-sm text-gray-500">加载中…</div>
      ) : (
        <div className="space-y-2">
          {items.map((r) => (
            <Row
              key={r.id}
              title={`[${r.category}] ${r.title}${r.genre ? ` (${r.genre})` : ''}`}
              subtitle={`优先级 ${r.priority} · ${r.rule_text}`}
              active={r.is_active}
              onToggle={() => toggle(r.id)}
              onEdit={() => setDrawer(r)}
              onDelete={() => remove(r.id)}
            />
          ))}
          {items.length === 0 && <div className="text-sm text-gray-500">暂无规则</div>}
        </div>
      )}

      <Drawer
        open={!!drawer}
        title="编辑规则"
        onClose={() => setDrawer(null)}
        onSave={async () => {
          if (!drawer) return
          await update(drawer.id, {
            genre: drawer.genre,
            category: drawer.category,
            title: drawer.title,
            rule_text: drawer.rule_text,
            priority: drawer.priority,
            is_active: drawer.is_active,
          })
          setDrawer(null)
        }}
      >
        {drawer && (
          <>
            <Field label="题材 (空字符串表示通用)" value={drawer.genre} onChange={(v) => setDrawer({ ...drawer, genre: v })} />
            <Field label="类别 (pacing/dialogue/hook/description)" value={drawer.category} onChange={(v) => setDrawer({ ...drawer, category: v })} />
            <Field label="标题" value={drawer.title} onChange={(v) => setDrawer({ ...drawer, title: v })} />
            <Field label="规则文本" value={drawer.rule_text} onChange={(v) => setDrawer({ ...drawer, rule_text: v })} multiline />
            <Field
              label="优先级 (0-100)"
              value={String(drawer.priority)}
              onChange={(v) => setDrawer({ ...drawer, priority: Number(v) || 0 })}
            />
          </>
        )}
      </Drawer>

      <Drawer
        open={!!creating}
        title="新建规则"
        onClose={() => setCreating(null)}
        onSave={async () => {
          if (!creating) return
          await create(creating as Record<string, unknown>)
          setCreating(null)
        }}
      >
        {creating && (
          <>
            <Field label="题材 (空字符串表示通用)" value={creating.genre || ''} onChange={(v) => setCreating({ ...creating, genre: v })} />
            <Field label="类别" value={creating.category || ''} onChange={(v) => setCreating({ ...creating, category: v })} />
            <Field label="标题" value={creating.title || ''} onChange={(v) => setCreating({ ...creating, title: v })} />
            <Field label="规则文本" value={creating.rule_text || ''} onChange={(v) => setCreating({ ...creating, rule_text: v })} multiline />
            <Field
              label="优先级 (0-100)"
              value={String(creating.priority ?? 50)}
              onChange={(v) => setCreating({ ...creating, priority: Number(v) || 0 })}
            />
          </>
        )}
      </Drawer>
    </div>
  )
}

function BeatsTab() {
  const { items, loading, err, toggle, remove, create, update } =
    useResource<BeatPattern>('/api/beat-patterns')
  const [drawer, setDrawer] = useState<BeatPattern | null>(null)
  const [creating, setCreating] = useState<Partial<BeatPattern> | null>(null)
  return (
    <div>
      <div className="flex justify-end mb-3">
        <button
          onClick={() =>
            setCreating({ genre: '', stage: 'opening', title: '', description: '', reusable: true, is_active: true })
          }
          className="text-sm px-3 py-1.5 rounded bg-blue-600 text-white hover:bg-blue-700"
        >
          新建 beat
        </button>
      </div>
      {err && <div className="text-sm text-red-600 mb-2">{err}</div>}
      {loading ? (
        <div className="text-sm text-gray-500">加载中…</div>
      ) : (
        <div className="space-y-2">
          {items.map((b) => (
            <Row
              key={b.id}
              title={`[${b.stage}] ${b.title}${b.genre ? ` (${b.genre})` : ''}`}
              subtitle={b.description}
              active={b.is_active}
              onToggle={() => toggle(b.id)}
              onEdit={() => setDrawer(b)}
              onDelete={() => remove(b.id)}
            />
          ))}
          {items.length === 0 && <div className="text-sm text-gray-500">暂无 beat</div>}
        </div>
      )}
      <Drawer
        open={!!drawer}
        title="编辑 beat"
        onClose={() => setDrawer(null)}
        onSave={async () => {
          if (!drawer) return
          await update(drawer.id, {
            genre: drawer.genre,
            stage: drawer.stage,
            title: drawer.title,
            description: drawer.description,
            reusable: drawer.reusable,
            is_active: drawer.is_active,
          })
          setDrawer(null)
        }}
      >
        {drawer && (
          <>
            <Field label="题材" value={drawer.genre} onChange={(v) => setDrawer({ ...drawer, genre: v })} />
            <Field label="阶段 (opening/turning/climax/volume_end/closure)" value={drawer.stage} onChange={(v) => setDrawer({ ...drawer, stage: v })} />
            <Field label="标题" value={drawer.title} onChange={(v) => setDrawer({ ...drawer, title: v })} />
            <Field label="描述" value={drawer.description} onChange={(v) => setDrawer({ ...drawer, description: v })} multiline />
          </>
        )}
      </Drawer>
      <Drawer
        open={!!creating}
        title="新建 beat"
        onClose={() => setCreating(null)}
        onSave={async () => {
          if (!creating) return
          await create(creating as Record<string, unknown>)
          setCreating(null)
        }}
      >
        {creating && (
          <>
            <Field label="题材" value={creating.genre || ''} onChange={(v) => setCreating({ ...creating, genre: v })} />
            <Field label="阶段" value={creating.stage || ''} onChange={(v) => setCreating({ ...creating, stage: v })} />
            <Field label="标题" value={creating.title || ''} onChange={(v) => setCreating({ ...creating, title: v })} />
            <Field label="描述" value={creating.description || ''} onChange={(v) => setCreating({ ...creating, description: v })} multiline />
          </>
        )}
      </Drawer>
    </div>
  )
}

function TrapsTab() {
  const { items, loading, err, toggle, remove, create, update } =
    useResource<AntiAITrap>('/api/anti-ai-traps')
  const [drawer, setDrawer] = useState<AntiAITrap | null>(null)
  const [creating, setCreating] = useState<Partial<AntiAITrap> | null>(null)
  return (
    <div>
      <div className="flex justify-end mb-3">
        <button
          onClick={() =>
            setCreating({ locale: 'zh-CN', pattern_type: 'keyword', pattern: '', severity: 'soft', replacement_hint: '', is_active: true })
          }
          className="text-sm px-3 py-1.5 rounded bg-blue-600 text-white hover:bg-blue-700"
        >
          新建陷阱
        </button>
      </div>
      {err && <div className="text-sm text-red-600 mb-2">{err}</div>}
      {loading ? (
        <div className="text-sm text-gray-500">加载中…</div>
      ) : (
        <div className="space-y-2">
          {items.map((t) => (
            <Row
              key={t.id}
              title={`[${t.pattern_type}][${t.severity}] ${t.pattern}`}
              subtitle={t.replacement_hint}
              active={t.is_active}
              onToggle={() => toggle(t.id)}
              onEdit={() => setDrawer(t)}
              onDelete={() => remove(t.id)}
            />
          ))}
          {items.length === 0 && <div className="text-sm text-gray-500">暂无陷阱</div>}
        </div>
      )}
      <Drawer
        open={!!drawer}
        title="编辑陷阱"
        onClose={() => setDrawer(null)}
        onSave={async () => {
          if (!drawer) return
          await update(drawer.id, {
            locale: drawer.locale,
            pattern_type: drawer.pattern_type,
            pattern: drawer.pattern,
            severity: drawer.severity,
            replacement_hint: drawer.replacement_hint,
            is_active: drawer.is_active,
          })
          setDrawer(null)
        }}
      >
        {drawer && (
          <>
            <Field label="Locale" value={drawer.locale} onChange={(v) => setDrawer({ ...drawer, locale: v })} />
            <Field label="类型 (keyword/regex/ngram)" value={drawer.pattern_type} onChange={(v) => setDrawer({ ...drawer, pattern_type: v as AntiAITrap['pattern_type'] })} />
            <Field label="模式" value={drawer.pattern} onChange={(v) => setDrawer({ ...drawer, pattern: v })} multiline />
            <Field label="严重程度 (hard/soft)" value={drawer.severity} onChange={(v) => setDrawer({ ...drawer, severity: v as AntiAITrap['severity'] })} />
            <Field label="修改建议" value={drawer.replacement_hint} onChange={(v) => setDrawer({ ...drawer, replacement_hint: v })} multiline />
          </>
        )}
      </Drawer>
      <Drawer
        open={!!creating}
        title="新建陷阱"
        onClose={() => setCreating(null)}
        onSave={async () => {
          if (!creating) return
          await create(creating as Record<string, unknown>)
          setCreating(null)
        }}
      >
        {creating && (
          <>
            <Field label="Locale" value={creating.locale || 'zh-CN'} onChange={(v) => setCreating({ ...creating, locale: v })} />
            <Field label="类型" value={creating.pattern_type || 'keyword'} onChange={(v) => setCreating({ ...creating, pattern_type: v as AntiAITrap['pattern_type'] })} />
            <Field label="模式" value={creating.pattern || ''} onChange={(v) => setCreating({ ...creating, pattern: v })} multiline />
            <Field label="严重程度" value={creating.severity || 'soft'} onChange={(v) => setCreating({ ...creating, severity: v as AntiAITrap['severity'] })} />
            <Field label="修改建议" value={creating.replacement_hint || ''} onChange={(v) => setCreating({ ...creating, replacement_hint: v })} multiline />
          </>
        )}
      </Drawer>
    </div>
  )
}

function GenresTab() {
  const { items, loading, err, toggle, remove, create, update } =
    useResource<GenreProfile>('/api/genre-profiles')
  const [drawer, setDrawer] = useState<GenreProfile | null>(null)
  const [creating, setCreating] = useState<Partial<GenreProfile> | null>(null)
  return (
    <div>
      <div className="flex justify-end mb-3">
        <button
          onClick={() => setCreating({ code: '', name: '', description: '', is_active: true })}
          className="text-sm px-3 py-1.5 rounded bg-blue-600 text-white hover:bg-blue-700"
        >
          新建题材画像
        </button>
      </div>
      {err && <div className="text-sm text-red-600 mb-2">{err}</div>}
      {loading ? (
        <div className="text-sm text-gray-500">加载中…</div>
      ) : (
        <div className="space-y-2">
          {items.map((g) => (
            <Row
              key={g.id}
              title={`${g.name} (${g.code})`}
              subtitle={g.description}
              active={g.is_active}
              onToggle={() => toggle(g.id)}
              onEdit={() => setDrawer(g)}
              onDelete={() => remove(g.id)}
            />
          ))}
          {items.length === 0 && <div className="text-sm text-gray-500">暂无题材画像</div>}
        </div>
      )}
      <Drawer
        open={!!drawer}
        title="编辑题材画像"
        onClose={() => setDrawer(null)}
        onSave={async () => {
          if (!drawer) return
          await update(drawer.id, {
            code: drawer.code,
            name: drawer.name,
            description: drawer.description,
            is_active: drawer.is_active,
          })
          setDrawer(null)
        }}
      >
        {drawer && (
          <>
            <Field label="代码 (唯一)" value={drawer.code} onChange={(v) => setDrawer({ ...drawer, code: v })} />
            <Field label="名称" value={drawer.name} onChange={(v) => setDrawer({ ...drawer, name: v })} />
            <Field label="描述" value={drawer.description} onChange={(v) => setDrawer({ ...drawer, description: v })} multiline />
          </>
        )}
      </Drawer>
      <Drawer
        open={!!creating}
        title="新建题材画像"
        onClose={() => setCreating(null)}
        onSave={async () => {
          if (!creating) return
          await create(creating as Record<string, unknown>)
          setCreating(null)
        }}
      >
        {creating && (
          <>
            <Field label="代码 (唯一)" value={creating.code || ''} onChange={(v) => setCreating({ ...creating, code: v })} />
            <Field label="名称" value={creating.name || ''} onChange={(v) => setCreating({ ...creating, name: v })} />
            <Field label="描述" value={creating.description || ''} onChange={(v) => setCreating({ ...creating, description: v })} multiline />
          </>
        )}
      </Drawer>
    </div>
  )
}

function ToolsTab() {
  const { items, loading, err, toggle, remove } = useResource<ToolSpec>('/api/tool-specs')
  return (
    <div>
      {err && <div className="text-sm text-red-600 mb-2">{err}</div>}
      {loading ? (
        <div className="text-sm text-gray-500">加载中…</div>
      ) : (
        <div className="space-y-2">
          {items.map((t) => (
            <Row
              key={t.id}
              title={`${t.name} (${t.handler})`}
              subtitle={t.description}
              active={t.is_active}
              onToggle={() => toggle(t.id)}
              onEdit={() => alert('工具规格目前仅支持启用 / 停用和删除。')}
              onDelete={() => remove(t.id)}
            />
          ))}
          {items.length === 0 && <div className="text-sm text-gray-500">暂无工具</div>}
        </div>
      )}
      <p className="mt-4 text-xs text-gray-500">
        新增工具需要后端同时注册 handler，当前仅支持启用/停用内置 5 个工具。
      </p>
    </div>
  )
}
