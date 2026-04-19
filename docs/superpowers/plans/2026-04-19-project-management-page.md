# 项目管理页 + 删除/重命名/软删除/批量 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把项目选择从下拉框升级为独立页面；全套增删改（软删 + 批量 + 回收站）+ 修复工作区"全书大纲"位置显示分卷大纲的 bug。

**Architecture:** 后端先加 `projects.deleted_at` 软删字段与 restore/purge 端点；前端把 `/` 改造成项目列表页，`/workspace?id=X` 为工作区，新增 `/trash` 回收站。工作区侧栏移除下拉。删除统一用模态，项目删除 type-to-confirm。

**Tech Stack:** FastAPI + SQLAlchemy + Alembic（后端）；Next.js 16 + React 19 + zustand + Tailwind（前端）；pytest（后端测试）。

---

## Task 1: Alembic 迁移 — projects.deleted_at

**Files:**
- Create: `backend/alembic/versions/2026_04_19_add_deleted_at_to_projects.py`

- [ ] **Step 1: 查当前 alembic head**

Run:
```bash
cd /root/ai-write/backend && docker compose -f ../docker-compose.yml exec backend alembic heads
```
Expected: 类似 `482b5e188065 (head)`，记下作 `down_revision`。

- [ ] **Step 2: 生成迁移骨架**

Run:
```bash
docker compose -f /root/ai-write/docker-compose.yml exec backend alembic revision -m "add deleted_at to projects"
```
输出文件名会是 `<revision>_add_deleted_at_to_projects.py`，位于 `backend/alembic/versions/`。

- [ ] **Step 3: 填写迁移内容**

编辑刚生成的文件，替换 `upgrade()` / `downgrade()`：

```python
def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_projects_deleted_at_null",
        "projects",
        ["deleted_at"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_projects_deleted_at_null", table_name="projects")
    op.drop_column("projects", "deleted_at")
```

部分索引让活跃项目查询命中 `ix_projects_deleted_at_null`；trash 查询全表扫描可接受。

- [ ] **Step 4: 执行迁移**

Run:
```bash
docker compose -f /root/ai-write/docker-compose.yml exec backend alembic upgrade head
```
Expected: `Running upgrade 482b5e188065 -> <new_rev>, add deleted_at to projects`

- [ ] **Step 5: 验证 schema**

Run:
```bash
docker exec ai-write-postgres-1 psql -U postgres -d aiwrite -c "\d projects" | grep deleted
```
Expected: `deleted_at | timestamp with time zone`

- [ ] **Step 6: commit**

```bash
git -C /root/ai-write add backend/alembic/versions/
git -C /root/ai-write commit -m "feat(db): add deleted_at to projects for soft delete"
```

---

## Task 2: Project model + list/get/delete 软删

**Files:**
- Modify: `backend/app/models/project.py:26-52` (Project 类)
- Modify: `backend/app/api/projects.py` (list + get + delete)

- [ ] **Step 1: 加 deleted_at 列到 Project 模型**

编辑 `backend/app/models/project.py`，在 `Project` 类的 `updated_at` 下面加：

```python
    deleted_at = Column(DateTime(timezone=True), nullable=True)
```

- [ ] **Step 2: list_projects 过滤 deleted + 支持 trashed**

编辑 `backend/app/api/projects.py` 的 `list_projects`：

```python
@router.get("")
async def list_projects(
    trashed: bool = False,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """List projects; by default active only. Pass ?trashed=true for trash."""
    query = select(Project)
    if trashed:
        query = query.where(Project.deleted_at.is_not(None))
    else:
        query = query.where(Project.deleted_at.is_(None))
    query = query.order_by(
        Project.deleted_at.desc() if trashed else Project.created_at.desc()
    )
    result = await db.execute(query)
    projects = result.scalars().all()
    return {
        "projects": [ProjectResponse.model_validate(p) for p in projects],
        "total": len(projects),
    }
```

- [ ] **Step 3: get_project 对软删 404**

同文件 `get_project`：

```python
@router.get("/{project_id}")
async def get_project(
    project_id: UUID,
    include_deleted: bool = False,
    db: AsyncSession = Depends(get_db),
) -> ProjectResponse:
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.deleted_at is not None and not include_deleted:
        raise HTTPException(status_code=404, detail="Project not found")
    return ProjectResponse.model_validate(project)
```

- [ ] **Step 4: delete_project 改软删，加 purge 分支**

同文件 `delete_project`：

```python
from datetime import datetime, timezone as _tz

@router.delete("/{project_id}", status_code=204)
async def delete_project(
    project_id: UUID,
    purge: bool = False,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Soft-delete by default; pass ?purge=true to hard-delete from trash."""
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    if purge:
        await db.delete(project)
    else:
        if project.deleted_at is None:
            project.deleted_at = datetime.now(_tz.utc)
    await db.flush()
```

- [ ] **Step 5: 本地启动 backend 并冒烟验证**

Run:
```bash
curl -s -X POST http://127.0.0.1:8000/api/auth/login -H 'Content-Type: application/json' \
  -d '{"username":"king","password":"Wt991125"}' | python3 -c "import json,sys;print(json.load(sys.stdin)['token'])"
```
把 token 存起来（`T=<token>`）。

Backend 容器会自动 reload（uvicorn --reload）。调：

```bash
curl -s -H "Authorization: Bearer $T" http://127.0.0.1:8000/api/projects | python3 -m json.tool | head -20
```
Expected：返回 JSON 含 `projects` 键。

- [ ] **Step 6: commit**

```bash
git -C /root/ai-write add backend/app/models/project.py backend/app/api/projects.py
git -C /root/ai-write commit -m "feat(api): soft-delete projects + filter deleted in list/get"
```

---

## Task 3: 后端新增 restore 端点 + 测试

**Files:**
- Modify: `backend/app/api/projects.py`（加 `restore_project`）
- Modify: `backend/tests/test_api_core.py`（加测试）

- [ ] **Step 1: 写 restore 测试（先失败）**

在 `backend/tests/test_api_core.py` 末尾追加：

```python
@pytest.mark.asyncio
async def test_soft_delete_and_restore_project(auth_client):
    # Create
    resp = await auth_client.post("/api/projects", json={"title": "软删测试", "genre": "测试"})
    assert resp.status_code == 201
    pid = resp.json()["id"]

    # Soft delete
    resp = await auth_client.delete(f"/api/projects/{pid}")
    assert resp.status_code == 204

    # Should be hidden from list
    resp = await auth_client.get("/api/projects")
    ids = [p["id"] for p in resp.json()["projects"]]
    assert pid not in ids

    # Should appear in trashed list
    resp = await auth_client.get("/api/projects?trashed=true")
    trashed_ids = [p["id"] for p in resp.json()["projects"]]
    assert pid in trashed_ids

    # Restore
    resp = await auth_client.post(f"/api/projects/{pid}/restore")
    assert resp.status_code == 200
    assert resp.json()["id"] == pid

    # Back in active list
    resp = await auth_client.get("/api/projects")
    ids = [p["id"] for p in resp.json()["projects"]]
    assert pid in ids

    # Purge
    resp = await auth_client.delete(f"/api/projects/{pid}?purge=true")
    assert resp.status_code == 204
    resp = await auth_client.get(f"/api/projects/{pid}")
    assert resp.status_code == 404
```

- [ ] **Step 2: 运行测试确认失败**

Run:
```bash
docker compose -f /root/ai-write/docker-compose.yml exec backend pytest tests/test_api_core.py::test_soft_delete_and_restore_project -v
```
Expected: FAIL（restore 404 因为端点不存在）。

- [ ] **Step 3: 实现 restore 端点**

在 `backend/app/api/projects.py` 加：

```python
@router.post("/{project_id}/restore")
async def restore_project(
    project_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> ProjectResponse:
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.deleted_at is None:
        raise HTTPException(status_code=400, detail="Project is not deleted")
    project.deleted_at = None
    await db.flush()
    await db.refresh(project)
    return ProjectResponse.model_validate(project)
```

- [ ] **Step 4: 运行测试确认通过**

Run:
```bash
docker compose -f /root/ai-write/docker-compose.yml exec backend pytest tests/test_api_core.py::test_soft_delete_and_restore_project -v
```
Expected: PASS。

- [ ] **Step 5: 跑全量 backend 测试回归**

Run:
```bash
docker compose -f /root/ai-write/docker-compose.yml exec backend pytest tests/ -x
```
Expected: 原有测试全绿。

- [ ] **Step 6: commit**

```bash
git -C /root/ai-write add backend/app/api/projects.py backend/tests/test_api_core.py
git -C /root/ai-write commit -m "feat(api): add project restore endpoint + soft-delete tests"
```

---

## Task 4: 前端修复 loadProjectData 选错大纲 bug

**Files:**
- Modify: `frontend/src/components/workspace/DesktopWorkspace.tsx`（loadProjectData）

- [ ] **Step 1: 找 loadProjectData 位置**

Run:
```bash
grep -n "const loadProjectData\|const bookOutline\b" /root/ai-write/frontend/src/components/workspace/DesktopWorkspace.tsx
```
Expected: `const loadProjectData = useCallback(` 在 ~186 行，`const bookOutline` 在 ~214 行。

- [ ] **Step 2: 替换 bookOutline 选择逻辑**

把现有这段（约 212-220 行）：

```typescript
// Load book outline into preview (prefer confirmed)
const bookOutlines = outlines.filter((o) => o.level === 'book')
const bookOutline =
  bookOutlines.find((o) => o.is_confirmed) || bookOutlines[0] || null
```

替换为：

```typescript
// Load book outline into preview (prefer confirmed).
// Filter out legacy garbage: book-level records whose content is actually
// a volume outline (has volume_idx key). Prefer earliest valid outline.
const bookOutlines = outlines
  .filter((o) => o.level === 'book')
  .filter((o) => {
    const cj = (o.content_json as Record<string, unknown>) || {}
    return !('volume_idx' in cj)
  })
  .sort((a, b) => (a.id < b.id ? -1 : 1))
const bookOutline =
  bookOutlines.find((o) => o.is_confirmed) || bookOutlines[0] || null
```

注：按 id 排序为稳定回退（没有 created_at 字段在 DTO 里）。后端按 created_at DESC 返回，`.sort()` 反一次拿到最早的。

- [ ] **Step 3: typecheck**

Run:
```bash
cd /root/ai-write/frontend && npx tsc --noEmit
```
Expected: 无错误。

- [ ] **Step 4: commit**

```bash
git -C /root/ai-write add frontend/src/components/workspace/DesktopWorkspace.tsx
git -C /root/ai-write commit -m "fix(workspace): ignore stale book outlines that contain volume_idx"
```

---

## Task 5: 前端 API 客户端增强（restore / purge / trashed）

**Files:**
- 此任务不改 `api.ts`（已有通用 `apiFetch`）。验证现有 helpers 足够覆盖后续调用。
- 跳过此任务，直接进 Task 6。

---

## Task 6: 创建 ProjectListPage 与 ProjectCard 组件

**Files:**
- Create: `frontend/src/components/project/ProjectCard.tsx`
- Create: `frontend/src/components/project/ProjectListPage.tsx`

- [ ] **Step 1: 创建 ProjectCard.tsx**

```typescript
'use client'

import type { Project } from '@/stores/projectStore'

export interface ProjectStats {
  volumeCount: number
  chapterCount: number
  totalWords: number
}

interface Props {
  project: Project
  stats?: ProjectStats
  selected?: boolean
  selectable?: boolean
  onToggleSelect?: (id: string) => void
  onOpen: (id: string) => void
  onRename: (project: Project) => void
  onDelete: (project: Project) => void
}

function formatRelative(iso?: string): string {
  if (!iso) return ''
  const t = new Date(iso).getTime()
  if (Number.isNaN(t)) return ''
  const diff = (Date.now() - t) / 1000
  if (diff < 60) return '刚刚'
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`
  if (diff < 30 * 86400) return `${Math.floor(diff / 86400)} 天前`
  return new Date(iso).toLocaleDateString('zh-CN')
}

export function ProjectCard({
  project,
  stats,
  selected,
  selectable,
  onToggleSelect,
  onOpen,
  onRename,
  onDelete,
}: Props) {
  const handleBodyClick = () => {
    if (selectable) onToggleSelect?.(project.id)
    else onOpen(project.id)
  }
  const stop = (e: React.MouseEvent) => e.stopPropagation()

  return (
    <div
      onClick={handleBodyClick}
      className={`relative rounded-xl border bg-white p-4 cursor-pointer transition-shadow hover:shadow-md ${
        selected ? 'border-blue-500 ring-2 ring-blue-300' : 'border-gray-200'
      }`}
    >
      {selectable && (
        <input
          type="checkbox"
          checked={!!selected}
          onChange={() => onToggleSelect?.(project.id)}
          onClick={stop}
          className="absolute top-3 left-3"
        />
      )}
      <div className={`${selectable ? 'pl-7' : ''} pr-8`}>
        <h3 className="text-base font-semibold text-gray-900 truncate">
          {project.title}
        </h3>
        {project.genre && (
          <span className="inline-block mt-1 px-2 py-0.5 text-[10px] bg-gray-100 text-gray-600 rounded">
            {project.genre}
          </span>
        )}
        {project.premise && (
          <p className="mt-2 text-xs text-gray-500 line-clamp-2">
            {project.premise}
          </p>
        )}
        <div className="mt-3 text-[11px] text-gray-400 flex items-center gap-2 flex-wrap">
          <span>{formatRelative(project.created_at)}</span>
          {stats && (
            <>
              <span>·</span>
              <span>{stats.volumeCount} 卷</span>
              <span>·</span>
              <span>{stats.chapterCount} 章</span>
              {stats.totalWords > 0 && (
                <>
                  <span>·</span>
                  <span>{stats.totalWords.toLocaleString()} 字</span>
                </>
              )}
            </>
          )}
        </div>
      </div>
      {!selectable && (
        <ProjectCardMenu
          onRename={() => onRename(project)}
          onDelete={() => onDelete(project)}
        />
      )}
    </div>
  )
}

function ProjectCardMenu({
  onRename,
  onDelete,
}: {
  onRename: () => void
  onDelete: () => void
}) {
  const [open, setOpen] = React.useState(false)
  const ref = React.useRef<HTMLDivElement>(null)

  React.useEffect(() => {
    if (!open) return
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [open])

  return (
    <div ref={ref} className="absolute top-2 right-2" onClick={(e) => e.stopPropagation()}>
      <button
        onClick={() => setOpen((v) => !v)}
        className="w-7 h-7 flex items-center justify-center rounded hover:bg-gray-100 text-gray-500"
        aria-label="more"
      >
        ⋯
      </button>
      {open && (
        <div className="absolute right-0 mt-1 w-28 bg-white border border-gray-200 rounded-lg shadow-lg overflow-hidden z-10">
          <button
            onClick={() => { setOpen(false); onRename() }}
            className="block w-full text-left px-3 py-2 text-sm hover:bg-gray-50"
          >
            重命名
          </button>
          <button
            onClick={() => { setOpen(false); onDelete() }}
            className="block w-full text-left px-3 py-2 text-sm text-red-600 hover:bg-red-50"
          >
            删除
          </button>
        </div>
      )}
    </div>
  )
}

import React from 'react'
```

- [ ] **Step 2: 创建 ProjectListPage.tsx**

```typescript
'use client'

import React, { useCallback, useEffect, useState } from 'react'
import { useRouter } from 'next/navigation'
import { apiFetch } from '@/lib/api'
import type { Project } from '@/stores/projectStore'
import { useProjectStore } from '@/stores/projectStore'
import { ProjectCard, type ProjectStats } from './ProjectCard'
import { NewProjectModal } from './NewProjectModal'
import { RenameProjectModal } from './RenameProjectModal'
import { DeleteProjectModal } from './DeleteProjectModal'
import { BulkDeleteModal } from './BulkDeleteModal'

interface ListRes {
  projects: Project[]
  total: number
}

interface VolumeBrief { id: string; project_id: string }
interface ChapterBrief { id: string; volume_id: string; word_count: number }

export function ProjectListPage() {
  const router = useRouter()
  const { setProjects, setCurrentProject, projects } = useProjectStore()
  const [loading, setLoading] = useState(true)
  const [stats, setStats] = useState<Record<string, ProjectStats>>({})

  const [showNew, setShowNew] = useState(false)
  const [renameTarget, setRenameTarget] = useState<Project | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<Project | null>(null)

  const [selectMode, setSelectMode] = useState(false)
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set())
  const [showBulkDelete, setShowBulkDelete] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const data = await apiFetch<ListRes>('/api/projects')
      setProjects(data.projects)
    } finally {
      setLoading(false)
    }
  }, [setProjects])

  useEffect(() => { load() }, [load])

  // Lazy-compute stats per project (bounded concurrency: 3)
  useEffect(() => {
    let cancelled = false
    async function computeFor(p: Project) {
      try {
        const vols = await apiFetch<VolumeBrief[]>(`/api/projects/${p.id}/volumes`)
        const chs = await apiFetch<ChapterBrief[]>(`/api/projects/${p.id}/chapters`)
        if (cancelled) return
        const totalWords = chs.reduce((s, c) => s + (c.word_count || 0), 0)
        setStats((prev) => ({
          ...prev,
          [p.id]: { volumeCount: vols.length, chapterCount: chs.length, totalWords },
        }))
      } catch { /* ignore per-project failure */ }
    }
    const queue = [...projects]
    async function worker() {
      while (queue.length > 0 && !cancelled) {
        const p = queue.shift()!
        await computeFor(p)
      }
    }
    Promise.all([worker(), worker(), worker()])
    return () => { cancelled = true }
  }, [projects])

  const toggleSelect = useCallback((id: string) => {
    setSelectedIds((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id); else next.add(id)
      return next
    })
  }, [])

  const handleOpen = useCallback((id: string) => {
    const p = projects.find((x) => x.id === id)
    if (p) setCurrentProject(p)
    router.push(`/workspace?id=${id}`)
  }, [projects, router, setCurrentProject])

  const handleBulkDelete = useCallback(async () => {
    for (const id of selectedIds) {
      await apiFetch(`/api/projects/${id}`, { method: 'DELETE' })
    }
    setSelectedIds(new Set())
    setSelectMode(false)
    setShowBulkDelete(false)
    await load()
  }, [selectedIds, load])

  return (
    <div className="min-h-screen pt-12 bg-gray-50">
      <div className="max-w-6xl mx-auto px-6 py-8">
        <div className="flex items-center justify-between mb-6">
          <h1 className="text-2xl font-bold text-gray-900">我的项目</h1>
          <div className="flex items-center gap-2">
            <button
              onClick={() => router.push('/trash')}
              className="px-3 py-1.5 text-sm text-gray-600 hover:bg-gray-100 rounded-lg"
            >
              回收站
            </button>
            <button
              onClick={() => {
                if (selectMode) { setSelectMode(false); setSelectedIds(new Set()) }
                else setSelectMode(true)
              }}
              className="px-3 py-1.5 text-sm text-gray-600 hover:bg-gray-100 rounded-lg"
            >
              {selectMode ? '取消多选' : '多选'}
            </button>
            <button
              onClick={() => setShowNew(true)}
              className="px-4 py-1.5 text-sm bg-blue-600 text-white rounded-lg hover:bg-blue-700"
            >
              + 新建项目
            </button>
          </div>
        </div>

        {selectMode && (
          <div className="mb-4 flex items-center gap-3">
            <span className="text-sm text-gray-600">已选 {selectedIds.size} 项</span>
            <button
              onClick={() => setShowBulkDelete(true)}
              disabled={selectedIds.size === 0}
              className="px-3 py-1.5 text-sm bg-red-600 text-white rounded-lg hover:bg-red-700 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              删除选中
            </button>
          </div>
        )}

        {loading ? (
          <div className="py-20 text-center text-gray-400">加载中...</div>
        ) : projects.length === 0 ? (
          <div className="py-20 text-center">
            <p className="text-gray-500 mb-4">还没有项目，点击右上角"+ 新建项目"开始创作。</p>
          </div>
        ) : (
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-4">
            {projects.map((p) => (
              <ProjectCard
                key={p.id}
                project={p}
                stats={stats[p.id]}
                selectable={selectMode}
                selected={selectedIds.has(p.id)}
                onToggleSelect={toggleSelect}
                onOpen={handleOpen}
                onRename={setRenameTarget}
                onDelete={setDeleteTarget}
              />
            ))}
          </div>
        )}
      </div>

      {showNew && (
        <NewProjectModal
          onClose={() => setShowNew(false)}
          onCreated={async (created) => {
            setShowNew(false)
            await load()
            handleOpen(created.id)
          }}
        />
      )}
      {renameTarget && (
        <RenameProjectModal
          project={renameTarget}
          onClose={() => setRenameTarget(null)}
          onDone={async () => { setRenameTarget(null); await load() }}
        />
      )}
      {deleteTarget && (
        <DeleteProjectModal
          project={deleteTarget}
          onClose={() => setDeleteTarget(null)}
          onDone={async () => { setDeleteTarget(null); await load() }}
        />
      )}
      {showBulkDelete && (
        <BulkDeleteModal
          count={selectedIds.size}
          onClose={() => setShowBulkDelete(false)}
          onConfirm={handleBulkDelete}
        />
      )}
    </div>
  )
}
```

- [ ] **Step 3: typecheck（会报找不到 NewProjectModal 等，下一任务补齐）**

跳过单独 check；继续 Task 7。

---

## Task 7: 模态组件 — 新建 / 重命名 / 删除 / 批量删除

**Files:**
- Create: `frontend/src/components/project/NewProjectModal.tsx`
- Create: `frontend/src/components/project/RenameProjectModal.tsx`
- Create: `frontend/src/components/project/DeleteProjectModal.tsx`
- Create: `frontend/src/components/project/BulkDeleteModal.tsx`

- [ ] **Step 1: NewProjectModal.tsx**

```typescript
'use client'

import { useState } from 'react'
import { apiFetch } from '@/lib/api'
import type { Project } from '@/stores/projectStore'

const GENRES = ['玄幻', '仙侠', '都市', '言情', '悬疑', '科幻', '历史', '其他'] as const

export function NewProjectModal({
  onClose,
  onCreated,
}: {
  onClose: () => void
  onCreated: (p: Project) => void
}) {
  const [title, setTitle] = useState('')
  const [genre, setGenre] = useState<string>(GENRES[0])
  const [premise, setPremise] = useState('')
  const [busy, setBusy] = useState(false)

  const submit = async () => {
    if (!title.trim() || busy) return
    setBusy(true)
    try {
      const project = await apiFetch<Project>('/api/projects', {
        method: 'POST',
        body: JSON.stringify({ title: title.trim(), genre, premise: premise.trim() || null }),
      })
      onCreated(project)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div className="bg-white rounded-2xl shadow-xl w-full max-w-md mx-4 p-6">
        <h3 className="text-lg font-bold text-gray-900 mb-4">新建项目</h3>
        <div className="space-y-4">
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">
              书名 <span className="text-red-500">*</span>
            </label>
            <input
              type="text"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="输入小说名称"
              className="w-full px-3 py-2 text-sm border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500"
              autoFocus
            />
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">类型</label>
            <select
              value={genre}
              onChange={(e) => setGenre(e.target.value)}
              className="w-full px-3 py-2 text-sm border border-gray-300 rounded-lg"
            >
              {GENRES.map((g) => <option key={g} value={g}>{g}</option>)}
            </select>
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">故事梗概</label>
            <textarea
              value={premise}
              onChange={(e) => setPremise(e.target.value)}
              placeholder="简要描述你的小说设定和核心创意..."
              className="w-full px-3 py-2 text-sm border border-gray-300 rounded-lg resize-none h-24"
            />
          </div>
        </div>
        <div className="flex gap-3 mt-6">
          <button onClick={onClose} className="flex-1 px-4 py-2 text-sm border border-gray-300 rounded-lg hover:bg-gray-50">
            取消
          </button>
          <button
            onClick={submit}
            disabled={!title.trim() || busy}
            className="flex-1 px-4 py-2 text-sm bg-blue-600 text-white rounded-lg disabled:opacity-50"
          >
            {busy ? '创建中...' : '创建项目'}
          </button>
        </div>
      </div>
    </div>
  )
}
```

- [ ] **Step 2: RenameProjectModal.tsx**

```typescript
'use client'

import { useState } from 'react'
import { apiFetch } from '@/lib/api'
import type { Project } from '@/stores/projectStore'

export function RenameProjectModal({
  project,
  onClose,
  onDone,
}: {
  project: Project
  onClose: () => void
  onDone: () => void
}) {
  const [title, setTitle] = useState(project.title)
  const [busy, setBusy] = useState(false)

  const submit = async () => {
    const trimmed = title.trim()
    if (!trimmed || trimmed === project.title || busy) return
    setBusy(true)
    try {
      await apiFetch(`/api/projects/${project.id}`, {
        method: 'PUT',
        body: JSON.stringify({ title: trimmed }),
      })
      onDone()
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div className="bg-white rounded-2xl shadow-xl w-full max-w-md mx-4 p-6">
        <h3 className="text-lg font-bold text-gray-900 mb-4">重命名项目</h3>
        <input
          type="text"
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          className="w-full px-3 py-2 text-sm border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500"
          autoFocus
          onKeyDown={(e) => e.key === 'Enter' && submit()}
        />
        <div className="flex gap-3 mt-6">
          <button onClick={onClose} className="flex-1 px-4 py-2 text-sm border border-gray-300 rounded-lg hover:bg-gray-50">
            取消
          </button>
          <button
            onClick={submit}
            disabled={!title.trim() || title.trim() === project.title || busy}
            className="flex-1 px-4 py-2 text-sm bg-blue-600 text-white rounded-lg disabled:opacity-50"
          >
            {busy ? '保存中...' : '保存'}
          </button>
        </div>
      </div>
    </div>
  )
}
```

- [ ] **Step 3: DeleteProjectModal.tsx（type-to-confirm）**

```typescript
'use client'

import { useState } from 'react'
import { apiFetch } from '@/lib/api'
import type { Project } from '@/stores/projectStore'

export function DeleteProjectModal({
  project,
  onClose,
  onDone,
}: {
  project: Project
  onClose: () => void
  onDone: () => void
}) {
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const matches = input.trim() === project.title.trim()

  const submit = async () => {
    if (!matches || busy) return
    setBusy(true)
    try {
      await apiFetch(`/api/projects/${project.id}`, { method: 'DELETE' })
      onDone()
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div className="bg-white rounded-2xl shadow-xl w-full max-w-md mx-4 p-6">
        <h3 className="text-lg font-bold text-red-600 mb-2">⚠ 删除项目</h3>
        <p className="text-sm text-gray-700">
          项目「{project.title}」将被移入回收站，可随时从回收站恢复。要永久删除，请进入回收站操作。
        </p>
        <p className="text-sm text-gray-700 mt-3">
          为确认删除，请输入书名：<span className="font-semibold">{project.title}</span>
        </p>
        <input
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          className="w-full px-3 py-2 mt-2 text-sm border border-gray-300 rounded-lg focus:ring-2 focus:ring-red-500"
          autoFocus
        />
        <div className="flex gap-3 mt-6">
          <button onClick={onClose} className="flex-1 px-4 py-2 text-sm border border-gray-300 rounded-lg hover:bg-gray-50">
            取消
          </button>
          <button
            onClick={submit}
            disabled={!matches || busy}
            className="flex-1 px-4 py-2 text-sm bg-red-600 text-white rounded-lg disabled:opacity-40 disabled:cursor-not-allowed"
          >
            {busy ? '删除中...' : '删除'}
          </button>
        </div>
      </div>
    </div>
  )
}
```

- [ ] **Step 4: BulkDeleteModal.tsx**

```typescript
'use client'

import { useState } from 'react'

export function BulkDeleteModal({
  count,
  onClose,
  onConfirm,
}: {
  count: number
  onClose: () => void
  onConfirm: () => Promise<void> | void
}) {
  const [busy, setBusy] = useState(false)
  const go = async () => {
    if (busy) return
    setBusy(true)
    try { await onConfirm() } finally { setBusy(false) }
  }
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div className="bg-white rounded-2xl shadow-xl w-full max-w-md mx-4 p-6">
        <h3 className="text-lg font-bold text-red-600 mb-2">⚠ 批量删除</h3>
        <p className="text-sm text-gray-700">
          选中的 {count} 个项目将被移入回收站，可随时恢复。
        </p>
        <div className="flex gap-3 mt-6">
          <button onClick={onClose} className="flex-1 px-4 py-2 text-sm border border-gray-300 rounded-lg hover:bg-gray-50">
            取消
          </button>
          <button
            onClick={go}
            disabled={busy}
            className="flex-1 px-4 py-2 text-sm bg-red-600 text-white rounded-lg disabled:opacity-50"
          >
            {busy ? '删除中...' : '删除'}
          </button>
        </div>
      </div>
    </div>
  )
}
```

- [ ] **Step 5: typecheck**

Run:
```bash
cd /root/ai-write/frontend && npx tsc --noEmit 2>&1 | head -20
```
Expected: 无错误。

- [ ] **Step 6: commit**

```bash
git -C /root/ai-write add frontend/src/components/project/
git -C /root/ai-write commit -m "feat(frontend): project list page + new/rename/delete/bulk modals"
```

---

## Task 8: 改造 `/` 路由到 ProjectListPage

**Files:**
- Modify: `frontend/src/app/page.tsx`

- [ ] **Step 1: 替换 page.tsx**

```typescript
'use client'

import { useEffect, useState } from 'react'
import dynamic from 'next/dynamic'
import { getToken } from '@/lib/api'

const ProjectListPage = dynamic(
  () => import('@/components/project/ProjectListPage').then((m) => m.ProjectListPage),
  { ssr: false, loading: () => (
    <div className="flex items-center justify-center h-screen pt-12 bg-gray-50">
      <p className="text-gray-400">加载中...</p>
    </div>
  )}
)

export default function Home() {
  const [checked, setChecked] = useState(false)
  useEffect(() => {
    if (!getToken()) {
      window.location.href = '/login'
      return
    }
    setChecked(true)
  }, [])
  if (!checked) {
    return (
      <div className="flex items-center justify-center h-screen bg-gray-50">
        <div className="text-center">
          <h1 className="text-2xl font-bold text-gray-900 mb-2">AI Write</h1>
          <p className="text-gray-400 text-sm">加载中...</p>
        </div>
      </div>
    )
  }
  return <ProjectListPage />
}
```

- [ ] **Step 2: typecheck + build**

```bash
cd /root/ai-write/frontend && npx tsc --noEmit && npm run build 2>&1 | tail -10
```
Expected: 编译成功，`/` 路由 prerendered。

- [ ] **Step 3: commit**

```bash
git -C /root/ai-write add frontend/src/app/page.tsx
git -C /root/ai-write commit -m "feat(frontend): swap / to project list page"
```

---

## Task 9: Workspace URL 驱动 + 返回按钮

**Files:**
- Modify: `frontend/src/components/workspace/DesktopWorkspace.tsx`
- Modify: `frontend/src/components/workspace/MobileWorkspace.tsx`
- Modify: `frontend/src/app/workspace/page.tsx`（确认 Suspense 包裹 useSearchParams）

- [ ] **Step 1: page.tsx 包裹 Suspense**

编辑 `frontend/src/app/workspace/page.tsx`，在 return 的 `<ErrorBoundary>` 外层加 `<Suspense>`:

```typescript
import { Component, Suspense, useEffect, useState } from 'react'
```

render 改为：
```typescript
return (
  <Suspense fallback={<div className="flex items-center justify-center h-screen pt-12 bg-gray-50"><p className="text-gray-400">加载工作区...</p></div>}>
    <ErrorBoundary>
      {mobile ? <MobileWorkspace /> : <DesktopWorkspace />}
    </ErrorBoundary>
  </Suspense>
)
```

- [ ] **Step 2: DesktopWorkspace 读 URL**

在 DesktopWorkspace 顶部 imports 加：

```typescript
import { useRouter, useSearchParams } from 'next/navigation'
```

在 component 里（靠近 useProjectStore 调用处）：

```typescript
const router = useRouter()
const searchParams = useSearchParams()
const urlProjectId = searchParams.get('id')
```

增加 useEffect 同步 URL → currentProject：

```typescript
useEffect(() => {
  if (!urlProjectId) {
    router.replace('/')
    return
  }
  if (currentProject?.id === urlProjectId) return
  apiFetch<Project>(`/api/projects/${urlProjectId}`)
    .then((p) => setCurrentProject(p))
    .catch(() => router.replace('/'))
}, [urlProjectId, currentProject?.id, router, setCurrentProject])
```

- [ ] **Step 3: 移除下拉 UI + 加返回按钮**

在 DesktopWorkspace 的 sidebar 顶部（现"Project selector"部分），把整个 dropdown 区域替换为：

```tsx
<div className="p-4 border-b border-gray-200">
  <button
    onClick={() => router.push('/')}
    className="flex items-center gap-1 text-sm text-gray-600 hover:text-gray-900 mb-2"
  >
    <span>←</span>
    <span>返回项目列表</span>
  </button>
  <h2 className="text-lg font-semibold text-gray-900 truncate">
    {currentProject?.title || 'AI Write'}
  </h2>
</div>
```

- [ ] **Step 4: 删除下拉相关本地状态**

在 DesktopWorkspace 里搜并删除：
- `const [selectorOpen, setSelectorOpen] = useState(false)`
- `const handleSelectProject = ...`（整个 useCallback）
- 顶部的 "新建项目" 按钮 + showNewProject modal 区块（现在这些归 `/`）

Run:
```bash
grep -n "selectorOpen\|showNewProject\|handleSelectProject" /root/ai-write/frontend/src/components/workspace/DesktopWorkspace.tsx
```
Expected: 命中的行全部删除（相关 JSX 一并清理）。

- [ ] **Step 5: creative 视图的"选现有项目"列表也清掉**

DesktopWorkspace 原有 `activeView === 'creative'` 分支列出前 5 个项目 — 现在 URL 永远带 id，所以这个视图不会再出现。把该分支整段删除（或保留一个"加载中"兜底文案）。

- [ ] **Step 6: MobileWorkspace 同步改造**

Run:
```bash
grep -n "selectorOpen\|handleSelectProject\|showNewProject\|useSearchParams\|useRouter" /root/ai-write/frontend/src/components/workspace/MobileWorkspace.tsx
```

若命中 `selectorOpen` 或 `handleSelectProject` —— 应用和 Steps 2-5 相同的改造：
- 加 `import { useRouter, useSearchParams } from 'next/navigation'`
- 顶部 `const urlProjectId = searchParams.get('id')`
- useEffect 监听 URL → `apiFetch<Project>(...)` → setCurrentProject / router.replace('/')
- 侧栏/顶栏的项目下拉区域替换为 `← 返回项目列表` 按钮 + 项目标题展示
- 删除 selectorOpen/handleSelectProject/showNewProject 相关本地状态与 JSX

若 MobileWorkspace 内没有 selectorOpen 相关代码，则仅加 URL 读取和返回按钮（保证移动端也能按 URL 进入）。

- [ ] **Step 7: typecheck**

```bash
cd /root/ai-write/frontend && npx tsc --noEmit 2>&1 | head -30
```
Expected: 无错误。

- [ ] **Step 8: commit**

```bash
git -C /root/ai-write add frontend/src/app/workspace frontend/src/components/workspace/
git -C /root/ai-write commit -m "feat(workspace): URL-driven project + back-to-list button"
```

---

## Task 10: 侧栏三点菜单 — OutlineTree rename/delete

**Files:**
- Create: `frontend/src/components/outline/RowMenu.tsx`（通用 hover 三点组件）
- Create: `frontend/src/components/outline/DeleteVolumeModal.tsx`
- Create: `frontend/src/components/outline/DeleteChapterModal.tsx`
- Modify: `frontend/src/components/outline/OutlineTree.tsx`

- [ ] **Step 1: 创建 RowMenu.tsx**

```typescript
'use client'

import React, { useEffect, useRef, useState } from 'react'

export interface MenuItem {
  label: string
  onClick: () => void
  danger?: boolean
}

export function RowMenu({ items }: { items: MenuItem[] }) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [open])

  return (
    <div ref={ref} className="relative" onClick={(e) => e.stopPropagation()}>
      <button
        onClick={() => setOpen((v) => !v)}
        className="w-6 h-6 flex items-center justify-center rounded hover:bg-gray-200 text-gray-500"
        aria-label="more"
      >
        ⋯
      </button>
      {open && (
        <div className="absolute right-0 mt-1 w-24 bg-white border border-gray-200 rounded-lg shadow-lg overflow-hidden z-20">
          {items.map((it, i) => (
            <button
              key={i}
              onClick={() => { setOpen(false); it.onClick() }}
              className={`block w-full text-left px-3 py-1.5 text-xs hover:bg-gray-50 ${it.danger ? 'text-red-600 hover:bg-red-50' : ''}`}
            >
              {it.label}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
```

- [ ] **Step 2: DeleteVolumeModal.tsx**

```typescript
'use client'

import { useState } from 'react'
import { apiFetch } from '@/lib/api'

export function DeleteVolumeModal({
  projectId,
  volumeId,
  volumeTitle,
  chapterCount,
  onClose,
  onDone,
}: {
  projectId: string
  volumeId: string
  volumeTitle: string
  chapterCount: number
  onClose: () => void
  onDone: () => void
}) {
  const [busy, setBusy] = useState(false)
  const go = async () => {
    if (busy) return
    setBusy(true)
    try {
      await apiFetch(`/api/projects/${projectId}/volumes/${volumeId}`, { method: 'DELETE' })
      onDone()
    } finally { setBusy(false) }
  }
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div className="bg-white rounded-2xl shadow-xl w-full max-w-md mx-4 p-6">
        <h3 className="text-lg font-bold text-red-600 mb-2">⚠ 删除卷</h3>
        <p className="text-sm text-gray-700">
          卷「{volumeTitle}」及其下 {chapterCount} 章内容将被彻底删除，不可恢复。
        </p>
        <div className="flex gap-3 mt-6">
          <button onClick={onClose} className="flex-1 px-4 py-2 text-sm border border-gray-300 rounded-lg hover:bg-gray-50">取消</button>
          <button onClick={go} disabled={busy} className="flex-1 px-4 py-2 text-sm bg-red-600 text-white rounded-lg disabled:opacity-50">
            {busy ? '删除中...' : '删除'}
          </button>
        </div>
      </div>
    </div>
  )
}
```

- [ ] **Step 3: DeleteChapterModal.tsx**

```typescript
'use client'

import { useState } from 'react'
import { apiFetch } from '@/lib/api'

export function DeleteChapterModal({
  projectId,
  chapterId,
  chapterTitle,
  onClose,
  onDone,
}: {
  projectId: string
  chapterId: string
  chapterTitle: string
  onClose: () => void
  onDone: () => void
}) {
  const [busy, setBusy] = useState(false)
  const go = async () => {
    if (busy) return
    setBusy(true)
    try {
      await apiFetch(`/api/projects/${projectId}/chapters/${chapterId}`, { method: 'DELETE' })
      onDone()
    } finally { setBusy(false) }
  }
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div className="bg-white rounded-2xl shadow-xl w-full max-w-md mx-4 p-6">
        <h3 className="text-lg font-bold text-red-600 mb-2">⚠ 删除章节</h3>
        <p className="text-sm text-gray-700">
          章节「{chapterTitle}」的内容将被彻底删除，不可恢复。
        </p>
        <div className="flex gap-3 mt-6">
          <button onClick={onClose} className="flex-1 px-4 py-2 text-sm border border-gray-300 rounded-lg hover:bg-gray-50">取消</button>
          <button onClick={go} disabled={busy} className="flex-1 px-4 py-2 text-sm bg-red-600 text-white rounded-lg disabled:opacity-50">
            {busy ? '删除中...' : '删除'}
          </button>
        </div>
      </div>
    </div>
  )
}
```

- [ ] **Step 4: 改造 OutlineTree.tsx 加 hover 菜单 + rename inline**

在 OutlineTree 组件内：

1. 引入：`import { RowMenu } from './RowMenu'`、`import { DeleteVolumeModal } from './DeleteVolumeModal'`、`import { DeleteChapterModal } from './DeleteChapterModal'`、`import { apiFetch } from '@/lib/api'`

2. 新增 props：`projectId: string`、`onChanged: () => void`（用来触发父组件重载 volumes/chapters）

3. 新增 state：
```typescript
const [renamingVolumeId, setRenamingVolumeId] = useState<string | null>(null)
const [renameVolumeValue, setRenameVolumeValue] = useState('')
const [renamingChapterId, setRenamingChapterId] = useState<string | null>(null)
const [renameChapterValue, setRenameChapterValue] = useState('')
const [deleteVolume, setDeleteVolume] = useState<{id: string; title: string; chapterCount: number} | null>(null)
const [deleteChapter, setDeleteChapter] = useState<{id: string; title: string} | null>(null)
```

4. volume 行：把 `<button onClick={toggleNode}` 外包一层 `group` class（已有），并在按钮右端加 RowMenu：
```tsx
<div className="flex items-center w-full px-3 py-1.5 hover:bg-gray-100 rounded group">
  <button onClick={() => toggleNode(volume.id)} className="flex-1 flex items-center text-left">
    <span className="mr-1 text-gray-400 text-xs">{expandedNodes.has(volume.id) ? '▼' : '▶'}</span>
    {renamingVolumeId === volume.id ? (
      <input
        autoFocus
        value={renameVolumeValue}
        onClick={(e) => e.stopPropagation()}
        onChange={(e) => setRenameVolumeValue(e.target.value)}
        onKeyDown={async (e) => {
          if (e.key === 'Enter') {
            await apiFetch(`/api/projects/${projectId}/volumes/${volume.id}`, {
              method: 'PUT', body: JSON.stringify({ title: renameVolumeValue.trim() || volume.title })
            })
            setRenamingVolumeId(null); onChanged()
          } else if (e.key === 'Escape') setRenamingVolumeId(null)
        }}
        onBlur={() => setRenamingVolumeId(null)}
        className="text-sm flex-1 px-1 border border-blue-300 rounded"
      />
    ) : (
      <span className="font-medium text-gray-700 flex-1 truncate">{volume.title}</span>
    )}
    <span className="text-[10px] text-gray-400 ml-1 opacity-0 group-hover:opacity-100 transition-opacity">
      {volChapters.length}章
    </span>
  </button>
  <div className="opacity-0 group-hover:opacity-100 transition-opacity">
    <RowMenu items={[
      { label: '重命名', onClick: () => { setRenameVolumeValue(volume.title); setRenamingVolumeId(volume.id) }},
      { label: '删除', danger: true, onClick: () => setDeleteVolume({ id: volume.id, title: volume.title, chapterCount: volChapters.length }) },
    ]} />
  </div>
</div>
```

5. chapter 行改造（和 volume 行结构一致）。找到 `volChapters.map((chapter) => { ... })` 返回的 `<button>` JSX，替换为：

```tsx
<div
  key={chapter.id}
  className={`flex items-center w-full px-3 py-1 rounded group ${
    selectedChapterId === chapter.id ? 'bg-blue-50' : 'hover:bg-gray-50'
  }`}
>
  <button
    onClick={() => handleSelectChapter(chapter.id)}
    className="flex-1 flex items-center text-left"
  >
    <span className="mr-1.5 text-gray-300">-</span>
    {renamingChapterId === chapter.id ? (
      <input
        autoFocus
        value={renameChapterValue}
        onClick={(e) => e.stopPropagation()}
        onChange={(e) => setRenameChapterValue(e.target.value)}
        onKeyDown={async (e) => {
          if (e.key === 'Enter') {
            await apiFetch(`/api/projects/${projectId}/chapters/${chapter.id}`, {
              method: 'PUT',
              body: JSON.stringify({ title: renameChapterValue.trim() || chapter.title }),
            })
            setRenamingChapterId(null); onChanged()
          } else if (e.key === 'Escape') setRenamingChapterId(null)
        }}
        onBlur={() => setRenamingChapterId(null)}
        className="text-sm flex-1 px-1 border border-blue-300 rounded"
      />
    ) : (
      <span className={`flex-1 truncate ${selectedChapterId === chapter.id ? 'text-blue-700' : 'text-gray-600'}`}>
        {chapter.title}
      </span>
    )}
    <span className="flex items-center gap-1 ml-1">
      {(chapter.word_count ?? chapter.wordCount ?? 0) > 0 && (
        <span className="text-[10px] text-gray-400">
          {((chapter.word_count ?? chapter.wordCount ?? 0) > 1000)
            ? `${(((chapter.word_count ?? chapter.wordCount ?? 0) / 1000)).toFixed(1)}k`
            : (chapter.word_count ?? chapter.wordCount ?? 0)}
        </span>
      )}
      <span className={`text-[9px] px-1 py-0.5 rounded ${statusColors[chapter.status || 'draft'] || statusColors.draft}`}>
        {statusLabels[chapter.status || 'draft'] || chapter.status}
      </span>
    </span>
  </button>
  <div className="opacity-0 group-hover:opacity-100 transition-opacity ml-1">
    <RowMenu items={[
      { label: '重命名', onClick: () => { setRenameChapterValue(chapter.title); setRenamingChapterId(chapter.id) }},
      { label: '删除', danger: true, onClick: () => setDeleteChapter({ id: chapter.id, title: chapter.title }) },
    ]} />
  </div>
</div>
```

6. component 末尾加 modals 渲染：

```tsx
{deleteVolume && (
  <DeleteVolumeModal
    projectId={projectId}
    volumeId={deleteVolume.id}
    volumeTitle={deleteVolume.title}
    chapterCount={deleteVolume.chapterCount}
    onClose={() => setDeleteVolume(null)}
    onDone={() => { setDeleteVolume(null); onChanged() }}
  />
)}
{deleteChapter && (
  <DeleteChapterModal
    projectId={projectId}
    chapterId={deleteChapter.id}
    chapterTitle={deleteChapter.title}
    onClose={() => setDeleteChapter(null)}
    onDone={() => { setDeleteChapter(null); onChanged() }}
  />
)}
```

- [ ] **Step 5: DesktopWorkspace 调 OutlineTree 时传新 props**

Modify 调用处（原 `<OutlineTree volumeOutlines=... onSelectChapter=... />`）：

```tsx
<OutlineTree
  projectId={currentProject!.id}
  volumeOutlines={volumeOutlines}
  onChanged={() => loadProjectData(currentProject!.id)}
  onSelectChapter={(chapterId) => { selectChapter(chapterId); setActiveView('editor') }}
/>
```

`loadProjectData` 需要能从 store 拿到 currentProject；已经是 useCallback，确认下 dep。

- [ ] **Step 6: typecheck**

```bash
cd /root/ai-write/frontend && npx tsc --noEmit 2>&1 | head -20
```
Expected: 无错误。

- [ ] **Step 7: commit**

```bash
git -C /root/ai-write add frontend/src/components/outline/ frontend/src/components/workspace/DesktopWorkspace.tsx
git -C /root/ai-write commit -m "feat(outline): row three-dot menu + rename + delete modals"
```

---

## Task 11: 回收站 `/trash` 页面

**Files:**
- Create: `frontend/src/app/trash/page.tsx`
- Create: `frontend/src/components/project/TrashListPage.tsx`
- Create: `frontend/src/components/project/PurgeProjectModal.tsx`

- [ ] **Step 1: PurgeProjectModal.tsx（type-to-confirm，彻底删）**

```typescript
'use client'

import { useState } from 'react'
import { apiFetch } from '@/lib/api'

export function PurgeProjectModal({
  projectId,
  projectTitle,
  onClose,
  onDone,
}: {
  projectId: string
  projectTitle: string
  onClose: () => void
  onDone: () => void
}) {
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const matches = input.trim() === projectTitle.trim()
  const go = async () => {
    if (!matches || busy) return
    setBusy(true)
    try {
      await apiFetch(`/api/projects/${projectId}?purge=true`, { method: 'DELETE' })
      onDone()
    } finally { setBusy(false) }
  }
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div className="bg-white rounded-2xl shadow-xl w-full max-w-md mx-4 p-6">
        <h3 className="text-lg font-bold text-red-600 mb-2">⚠ 永久删除</h3>
        <p className="text-sm text-gray-700">
          项目「{projectTitle}」将被<strong>永久删除</strong>，所有卷、章节、大纲、版本都不可恢复。
        </p>
        <p className="text-sm text-gray-700 mt-3">
          为确认，请输入书名：<span className="font-semibold">{projectTitle}</span>
        </p>
        <input
          value={input} onChange={(e) => setInput(e.target.value)} autoFocus
          className="w-full px-3 py-2 mt-2 text-sm border border-gray-300 rounded-lg focus:ring-2 focus:ring-red-500"
        />
        <div className="flex gap-3 mt-6">
          <button onClick={onClose} className="flex-1 px-4 py-2 text-sm border border-gray-300 rounded-lg hover:bg-gray-50">取消</button>
          <button onClick={go} disabled={!matches || busy} className="flex-1 px-4 py-2 text-sm bg-red-600 text-white rounded-lg disabled:opacity-40">
            {busy ? '删除中...' : '永久删除'}
          </button>
        </div>
      </div>
    </div>
  )
}
```

- [ ] **Step 2: TrashListPage.tsx**

```typescript
'use client'

import { useCallback, useEffect, useState } from 'react'
import { useRouter } from 'next/navigation'
import { apiFetch } from '@/lib/api'
import type { Project } from '@/stores/projectStore'
import { PurgeProjectModal } from './PurgeProjectModal'

interface TrashedProject extends Project {
  deleted_at?: string | null
}

export function TrashListPage() {
  const router = useRouter()
  const [items, setItems] = useState<TrashedProject[]>([])
  const [loading, setLoading] = useState(true)
  const [purgeTarget, setPurgeTarget] = useState<TrashedProject | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const data = await apiFetch<{ projects: TrashedProject[] }>('/api/projects?trashed=true')
      setItems(data.projects)
    } finally { setLoading(false) }
  }, [])

  useEffect(() => { load() }, [load])

  const restore = async (id: string) => {
    await apiFetch(`/api/projects/${id}/restore`, { method: 'POST' })
    await load()
  }

  return (
    <div className="min-h-screen pt-12 bg-gray-50">
      <div className="max-w-4xl mx-auto px-6 py-8">
        <div className="flex items-center gap-3 mb-6">
          <button onClick={() => router.push('/')} className="text-sm text-gray-600 hover:text-gray-900">← 返回</button>
          <h1 className="text-2xl font-bold text-gray-900">回收站</h1>
        </div>

        {loading ? (
          <div className="py-20 text-center text-gray-400">加载中...</div>
        ) : items.length === 0 ? (
          <div className="py-20 text-center text-gray-500">回收站为空</div>
        ) : (
          <table className="w-full bg-white rounded-xl overflow-hidden">
            <thead className="bg-gray-50 text-left text-xs text-gray-500 uppercase">
              <tr>
                <th className="px-4 py-2">书名</th>
                <th className="px-4 py-2">类型</th>
                <th className="px-4 py-2">删除时间</th>
                <th className="px-4 py-2 text-right">操作</th>
              </tr>
            </thead>
            <tbody className="text-sm">
              {items.map((p) => (
                <tr key={p.id} className="border-t border-gray-100">
                  <td className="px-4 py-3 font-medium text-gray-800">{p.title}</td>
                  <td className="px-4 py-3 text-gray-600">{p.genre || '—'}</td>
                  <td className="px-4 py-3 text-gray-500">
                    {p.deleted_at ? new Date(p.deleted_at).toLocaleString('zh-CN') : '—'}
                  </td>
                  <td className="px-4 py-3 text-right space-x-2">
                    <button onClick={() => restore(p.id)} className="text-blue-600 hover:underline">恢复</button>
                    <button onClick={() => setPurgeTarget(p)} className="text-red-600 hover:underline">永久删除</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      {purgeTarget && (
        <PurgeProjectModal
          projectId={purgeTarget.id}
          projectTitle={purgeTarget.title}
          onClose={() => setPurgeTarget(null)}
          onDone={async () => { setPurgeTarget(null); await load() }}
        />
      )}
    </div>
  )
}
```

- [ ] **Step 3: app/trash/page.tsx**

```typescript
'use client'

import { useEffect, useState } from 'react'
import dynamic from 'next/dynamic'
import { getToken } from '@/lib/api'

const TrashListPage = dynamic(
  () => import('@/components/project/TrashListPage').then((m) => m.TrashListPage),
  { ssr: false }
)

export default function TrashPage() {
  const [ok, setOk] = useState(false)
  useEffect(() => {
    if (!getToken()) { window.location.href = '/login'; return }
    setOk(true)
  }, [])
  if (!ok) return <div className="flex items-center justify-center h-screen pt-12 bg-gray-50"><p className="text-gray-400">加载中...</p></div>
  return <TrashListPage />
}
```

- [ ] **Step 4: Project 接口加 deleted_at 字段**

编辑 `frontend/src/stores/projectStore.ts` 的 `Project` interface：

```typescript
export interface Project {
  id: string
  title: string
  genre: string
  premise: string
  created_at?: string
  updated_at?: string
  deleted_at?: string | null
}
```

- [ ] **Step 5: 后端 ProjectResponse 暴露 deleted_at**

编辑 `backend/app/schemas/project.py`，在 `ProjectResponse` 类的 `updated_at` 后加一行：

```python
    deleted_at: datetime | None = None
```

`model_config = ConfigDict(from_attributes=True)` 已经在——会自动从 ORM 读到新列。

- [ ] **Step 6: typecheck + build**

```bash
cd /root/ai-write/frontend && npx tsc --noEmit && npm run build 2>&1 | tail -5
```
Expected: 无错误。

- [ ] **Step 7: commit**

```bash
git -C /root/ai-write add backend/app/schemas/project.py frontend/src/app/trash frontend/src/components/project/TrashListPage.tsx frontend/src/components/project/PurgeProjectModal.tsx frontend/src/stores/projectStore.ts
git -C /root/ai-write commit -m "feat(trash): /trash page with restore + permanent purge"
```

---

## Task 12: 部署 + 端到端验证

**Files:**
- 纯运行时。

- [ ] **Step 1: 重建 frontend 容器**

Run:
```bash
cd /root/ai-write && docker compose up -d --build frontend 2>&1 | tail -10
```
Expected: `Container ai-write-frontend-1 Started`。

- [ ] **Step 2: backend 重启并确认 migration 已应用**

如 Task 1 Step 4 还没执行到 live backend：
```bash
docker compose -f /root/ai-write/docker-compose.yml restart backend
docker compose -f /root/ai-write/docker-compose.yml exec backend alembic current
```
Expected: 显示新的 revision id。

- [ ] **Step 3: 冒烟路径 1 — 项目列表**

浏览器打开 `http://<host>:8080/` ；已登录应直接看到卡片网格。

- [ ] **Step 4: 冒烟路径 2 — 大纲 bug**

点「测试」项目进工作区；左侧侧栏顶部显示「测试」；工作区主区展示的「全书大纲」应为**原始书籍大纲文本**（不含 volume_idx JSON）。

- [ ] **Step 5: 冒烟路径 3 — 新建 → 生成 → 删除**

新建项目 → 进入工作区 → 生成 book outline → 生成 volume outline（2卷）→ 返回列表 → 点该项目卡片三点菜单 → 删除 → 输入书名 → 确认 → 项目消失；进 `/trash` → 看到它 → 恢复 → 回到列表。

- [ ] **Step 6: 冒烟路径 4 — 卷/章节删除 + 重命名**

进已有项目 → 侧栏 hover 一个卷 → 三点菜单 → 重命名成功 → 刷新仍在 → 删除该卷 → 卷消失，其下章节也消失。

- [ ] **Step 7: 冒烟路径 5 — 批量**

列表页 → 多选 → 选 2 个项目 → 删除选中 → 确认 → 都进 trash。

- [ ] **Step 8: 发现回归 issue 就修，直到 Steps 3-7 全通**

此步不独立 commit —— 修什么 commit 什么。

- [ ] **Step 9: 结项 commit（可选）**

若 Steps 3-7 全通无修改，不需要额外 commit。否则每个修复独立 commit。

---

## 非目标（再次声明）

- 卷/章节级的软删除与回收站条目
- 自动过期清理（N 天后硬删）
- 拖拽排序
- 项目归档（和删除区分的第三状态）
- `/trash` 里的批量操作（目前单条恢复/清除足够）
