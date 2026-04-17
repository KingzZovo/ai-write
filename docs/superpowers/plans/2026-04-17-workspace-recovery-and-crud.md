# Workspace Recovery And CRUD Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore a persistent desktop workspace flow so book/volume/chapter structure survives refresh, is visible on PC, and supports delete/edit management actions.

**Architecture:** Use persisted outlines plus volume/chapter records as the source of truth, not transient `outlinePreview` state. The desktop workspace should derive its current stage from a project snapshot, consume structured outline payloads instead of regex fallbacks, and expose existing backend delete endpoints through the UI.

**Tech Stack:** Next.js 16, React 19, TypeScript, Zustand, FastAPI, SQLAlchemy, PostgreSQL

---

## File Structure

**Create:**

- `backend/tests/test_workspace_flow.py`
- `frontend/src/lib/workspaceSnapshot.ts`

**Modify:**

- `frontend/src/components/workspace/DesktopWorkspace.tsx`
- `frontend/src/components/outline/OutlineTree.tsx`
- `frontend/src/stores/projectStore.ts`
- `backend/app/api/generate.py`
- `backend/app/api/chapters.py`

### Task 1: Lock Down Backend Ownership And Outline Save Semantics

**Files:**

- Create: `backend/tests/test_workspace_flow.py`
- Modify: `backend/app/api/generate.py`
- Modify: `backend/app/api/chapters.py`

- [ ] **Step 1: Write the failing tests**

```python
import pytest


@pytest.mark.asyncio
async def test_get_chapter_must_belong_to_project(auth_client):
    p1 = (await auth_client.post("/api/projects", json={"title": "P1", "genre": "玄幻"})).json()
    p2 = (await auth_client.post("/api/projects", json={"title": "P2", "genre": "玄幻"})).json()

    v1 = (await auth_client.post(f"/api/projects/{p1['id']}/volumes", json={"title": "第一卷", "volume_idx": 1})).json()
    ch1 = (await auth_client.post(
        f"/api/projects/{p1['id']}/chapters",
        json={"volume_id": v1["id"], "title": "第一章", "chapter_idx": 1, "outline_json": {}},
    )).json()

    resp = await auth_client.get(f"/api/projects/{p2['id']}/chapters/{ch1['id']}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_outline_sse_saved_payload_uses_structured_json_when_valid(auth_client, monkeypatch):
    from app.api import generate as generate_api

    async def fake_stream(*args, **kwargs):
        yield '{"volumes":[{"volume_idx":1,"title":"第一卷","chapter_summaries":[{"chapter_idx":1,"title":"第一章","summary":"开局","key_events":["出发"]}]}]}'

    monkeypatch.setattr(generate_api.OutlineGenerator, "generate_volume_outline", lambda *args, **kwargs: fake_stream())

    project = (await auth_client.post("/api/projects", json={"title": "P3", "genre": "玄幻"})).json()
    response = await auth_client.post(
        "/api/generate/outline",
        json={"project_id": project["id"], "level": "volume", "user_input": "test"},
    )
    text = response.text
    assert '"status": "saved"' in text or '"status":"saved"' in text
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
cd /root/ai-write
backend/.venv/bin/python -m pytest backend/tests/test_workspace_flow.py -q
```

Expected:

- `test_get_chapter_must_belong_to_project` fails because `get_chapter` only checks chapter existence
- `test_outline_sse_saved_payload_uses_structured_json_when_valid` fails because outline auto-save currently always stores `{"raw_text": ...}`

- [ ] **Step 3: Write the minimal backend fixes**

```python
# backend/app/api/chapters.py
chapter = await db.get(Chapter, chapter_id)
if not chapter:
    raise HTTPException(status_code=404, detail="Chapter not found")

volume = await db.get(Volume, chapter.volume_id)
if not volume or str(volume.project_id) != project_id:
    raise HTTPException(status_code=404, detail="Chapter not found")
```

```python
# backend/app/api/generate.py
full_text = "".join(collected_text)
content_json = {"raw_text": full_text}
try:
    parsed = json.loads(full_text)
    if isinstance(parsed, dict):
        content_json = parsed
except json.JSONDecodeError:
    pass

outline = Outline(
    project_id=req.project_id,
    level=req.level,
    parent_id=req.parent_outline_id,
    content_json=content_json,
)
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
cd /root/ai-write
backend/.venv/bin/python -m pytest backend/tests/test_workspace_flow.py -q
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/tests/test_workspace_flow.py backend/app/api/generate.py backend/app/api/chapters.py
git commit -m "fix: tighten workspace outline persistence boundaries"
```

### Task 2: Add A Persistent Project Snapshot Resolver For Desktop Workspace

**Files:**

- Create: `frontend/src/lib/workspaceSnapshot.ts`
- Modify: `frontend/src/stores/projectStore.ts`

- [ ] **Step 1: Write the failing test surrogate in code comments and fixture data**

Because the repo does not yet have a frontend test harness, create pure functions and validate them manually with fixed fixtures during implementation.

Use these fixtures as the acceptance baseline:

```ts
const outlines = [
  { id: 'book-1', level: 'book', parent_id: null, content_json: { raw_text: '全书大纲' } },
  { id: 'vol-1', level: 'volume', parent_id: 'book-1', content_json: { volume_idx: 1, title: '第一卷', chapter_summaries: [] } },
]

const volumes = [{ id: 'v1', title: '第一卷', volume_idx: 1 }]
const chapters = []
```

Expected:

- resolver returns `stage = 'volume-ready'`
- resolver returns `bookOutlineId = 'book-1'`
- resolver returns `bookOutlineText = '全书大纲'`

- [ ] **Step 2: Add the snapshot helper**

```ts
// frontend/src/lib/workspaceSnapshot.ts
export type WorkspaceStage =
  | 'book-missing'
  | 'volume-ready'
  | 'chapter-ready'
  | 'editor-ready'

export function getOutlineText(content: Record<string, unknown>): string {
  return String(content.raw_text || JSON.stringify(content, null, 2) || '')
}

export function resolveWorkspaceStage(input: {
  outlines: Array<{ id: string; level: string; parent_id: string | null; content_json: Record<string, unknown> }>
  volumes: Array<{ id: string }>
  chapters: Array<{ id: string }>
}) {
  const bookOutline = input.outlines.find((o) => o.level === 'book') || null
  const volumeOutlines = input.outlines.filter((o) => o.level === 'volume')

  if (!bookOutline) return { stage: 'book-missing' as const, bookOutline: null, volumeOutlines }
  if (input.volumes.length === 0) return { stage: 'volume-ready' as const, bookOutline, volumeOutlines }
  if (input.chapters.length === 0) return { stage: 'chapter-ready' as const, bookOutline, volumeOutlines }
  return { stage: 'editor-ready' as const, bookOutline, volumeOutlines }
}
```

- [ ] **Step 3: Extend project store for persisted outline metadata**

```ts
// frontend/src/stores/projectStore.ts
export interface StoredOutline {
  id: string
  level: 'book' | 'volume' | 'chapter'
  parentId: string | null
  contentJson: Record<string, unknown>
}

interface ProjectState {
  outlines: StoredOutline[]
  setOutlines: (outlines: StoredOutline[]) => void
}
```

- [ ] **Step 4: Verify with type-check and lint**

Run:

```bash
cd /root/ai-write/frontend
npm run lint
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/workspaceSnapshot.ts frontend/src/stores/projectStore.ts
git commit -m "refactor: add persistent workspace snapshot resolver"
```

### Task 3: Make DesktopWorkspace Consume Persisted Outlines Instead Of Regex Fallbacks

**Files:**

- Modify: `frontend/src/components/workspace/DesktopWorkspace.tsx`

- [ ] **Step 1: Remove duplicate book-outline save flow**

Replace the current book confirm behavior so it confirms the outline already auto-saved by SSE instead of POSTing a second copy.

```ts
const [latestGeneratedOutlineId, setLatestGeneratedOutlineId] = useState<string | null>(null)

// in SSE handling
if (parsed.status === 'saved' && parsed.outline_id) {
  setLatestGeneratedOutlineId(parsed.outline_id)
}
```

```ts
const handleConfirmOutline = useCallback(async () => {
  if (!currentProject || !latestGeneratedOutlineId) return
  await apiFetch(`/api/projects/${currentProject.id}/outlines/${latestGeneratedOutlineId}/confirm`, {
    method: 'POST',
  })
  setConfirmedOutlineId(latestGeneratedOutlineId)
  setWizardStep(2)
}, [currentProject, latestGeneratedOutlineId])
```

- [ ] **Step 2: Replace single-volume assumption with structured volume outline parsing**

Use a parsed payload shape like:

```ts
type VolumeOutlinePayload = {
  volumes: Array<{
    volume_idx: number
    title: string
    core_conflict?: string
    chapter_summaries?: Array<{
      chapter_idx: number
      title: string
      summary?: string
      key_events?: string[]
    }>
  }>
}
```

Implementation target:

```ts
const payload = JSON.parse(volumeOutlineText) as VolumeOutlinePayload
for (const item of payload.volumes) {
  const outline = await apiFetch(`/api/projects/${currentProject.id}/outlines`, {
    method: 'POST',
    body: JSON.stringify({
      level: 'volume',
      parent_id: confirmedOutlineId,
      content_json: item,
    }),
  })

  const volume = await apiFetch(`/api/projects/${currentProject.id}/volumes`, {
    method: 'POST',
    body: JSON.stringify({
      title: item.title,
      volume_idx: item.volume_idx,
      summary: item.core_conflict || null,
    }),
  })
}
```

- [ ] **Step 3: Build chapters from persisted volume outlines, not chapter-title regex extraction**

For each saved volume outline:

```ts
for (const summary of item.chapter_summaries || []) {
  await apiFetch(`/api/projects/${currentProject.id}/chapters`, {
    method: 'POST',
    body: JSON.stringify({
      volume_id: volume.id,
      title: summary.title,
      chapter_idx: summary.chapter_idx,
      outline_json: {
        summary: summary.summary || '',
        key_events: summary.key_events || [],
      },
    }),
  })
}
```

This replaces the current `extractChapterTitles()` fallback path for initial scaffold generation.

- [ ] **Step 4: Hydrate desktop stage from persisted snapshot on load**

On project load:

```ts
const snapshot = resolveWorkspaceStage({ outlines, volumes: normalized, chapters: normChs })
setOutlines(normalizedOutlines)
setOutlinePreview(snapshot.bookOutline ? getOutlineText(snapshot.bookOutline.contentJson) : '')

if (snapshot.stage === 'book-missing') {
  setActiveView('wizard')
  setWizardStep(1)
} else if (snapshot.stage === 'volume-ready') {
  setActiveView('wizard')
  setWizardStep(2)
} else if (snapshot.stage === 'chapter-ready') {
  setActiveView('wizard')
  setWizardStep(3)
} else {
  setActiveView('editor')
}
```

- [ ] **Step 5: Run frontend lint**

Run:

```bash
cd /root/ai-write/frontend
npm run lint
```

Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add frontend/src/components/workspace/DesktopWorkspace.tsx
git commit -m "fix: restore desktop workspace from persisted outlines"
```

### Task 4: Expose Desktop CRUD And Outline Management Actions

**Files:**

- Modify: `frontend/src/components/outline/OutlineTree.tsx`
- Modify: `frontend/src/components/workspace/DesktopWorkspace.tsx`

- [ ] **Step 1: Add callbacks to the outline tree**

```ts
interface OutlineTreeProps {
  onSelectChapter?: (chapterId: string) => void
  onDeleteVolume?: (volumeId: string) => void
  onDeleteChapter?: (chapterId: string) => void
}
```

- [ ] **Step 2: Add delete controls with explicit stopPropagation**

```tsx
<button
  onClick={(event) => {
    event.stopPropagation()
    onDeleteVolume?.(volume.id)
  }}
  className="opacity-0 group-hover:opacity-100 text-[10px] text-red-500"
>
  删除
</button>
```

- [ ] **Step 3: Wire project / volume / chapter delete actions in DesktopWorkspace**

```ts
const handleDeleteProject = async () => {
  if (!currentProject) return
  await apiFetch(`/api/projects/${currentProject.id}`, { method: 'DELETE' })
  setCurrentProject(null)
  setVolumes([])
  setChapters([])
  setOutlines([])
  setActiveView('creative')
}
```

```ts
const handleDeleteVolume = async (volumeId: string) => {
  await apiFetch(`/api/projects/${currentProject.id}/volumes/${volumeId}`, { method: 'DELETE' })
  await loadProjectData(currentProject.id)
}

const handleDeleteChapter = async (chapterId: string) => {
  await apiFetch(`/api/projects/${currentProject.id}/chapters/${chapterId}`, { method: 'DELETE' })
  await loadProjectData(currentProject.id)
}
```

- [ ] **Step 4: Add visible PC actions for “查看整体大纲” and “删除项目”**

```tsx
<div className="flex items-center gap-2">
  <button onClick={() => setActiveView('outline')} className="px-3 py-1.5 text-xs border rounded-lg">
    查看整体大纲
  </button>
  <button onClick={handleDeleteProject} className="px-3 py-1.5 text-xs text-red-600 border border-red-200 rounded-lg">
    删除项目
  </button>
</div>
```

- [ ] **Step 5: Verify manually**

Manual checklist:

- 在 PC 端创建项目并生成全书/分卷/章节结构
- 刷新页面后仍可看到整体大纲和卷章
- 删除一个章节后目录立即刷新
- 删除一个卷后其下章节一起消失
- 删除项目后回到空白工作区

- [ ] **Step 6: Commit**

```bash
git add frontend/src/components/outline/OutlineTree.tsx frontend/src/components/workspace/DesktopWorkspace.tsx
git commit -m "feat: add desktop workspace management actions"
```

### Task 5: Final Verification

**Files:**

- Test: `backend/tests/test_workspace_flow.py`

- [ ] **Step 1: Run backend verification**

```bash
cd /root/ai-write
backend/.venv/bin/python -m pytest backend/tests/test_workspace_flow.py -q
```

Expected: PASS

- [ ] **Step 2: Run frontend verification**

```bash
cd /root/ai-write/frontend
npm run lint
```

Expected: PASS

- [ ] **Step 3: Record manual smoke results in the PR / task note**

```text
PC refresh recovery: pass
Book outline visible on desktop: pass
Delete project: pass
Delete volume: pass
Delete chapter: pass
```

- [ ] **Step 4: Commit final verification note**

```bash
git add .
git commit -m "test: verify workspace recovery and desktop crud flow"
```
