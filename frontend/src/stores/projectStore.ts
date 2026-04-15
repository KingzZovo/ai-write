import { create } from 'zustand'

export interface Project {
  id: string
  title: string
  genre: string
  premise: string
  created_at?: string
  updated_at?: string
}

export interface Volume {
  id: string
  projectId: string
  project_id?: string
  title: string
  volumeIdx: number
  volume_idx?: number
  summary?: string | null
}

export interface Chapter {
  id: string
  volumeId: string
  volume_id?: string
  title: string
  chapterIdx: number
  chapter_idx?: number
  contentText: string
  content_text?: string
  wordCount: number
  word_count?: number
  status: 'draft' | 'generating' | 'completed'
  summary?: string | null
  outline_json?: Record<string, unknown>
}

interface ProjectState {
  // Project list
  projects: Project[]
  projectsLoaded: boolean
  setProjects: (projects: Project[]) => void
  setProjectsLoaded: (loaded: boolean) => void

  // Current project
  currentProject: Project | null
  volumes: Volume[]
  chapters: Chapter[]
  selectedChapterId: string | null
  setCurrentProject: (project: Project | null) => void
  setVolumes: (volumes: Volume[]) => void
  setChapters: (chapters: Chapter[]) => void
  addChapters: (chapters: Chapter[]) => void
  selectChapter: (id: string | null) => void
  updateChapterContent: (id: string, content: string) => void
  updateChapterStatus: (id: string, status: 'draft' | 'generating' | 'completed') => void
}

/** Normalize a volume from API (snake_case) to store format */
function normalizeVolume(v: Record<string, unknown>): Volume {
  return {
    id: String(v.id),
    projectId: String(v.project_id ?? v.projectId ?? ''),
    project_id: String(v.project_id ?? v.projectId ?? ''),
    title: String(v.title ?? ''),
    volumeIdx: Number(v.volume_idx ?? v.volumeIdx ?? 0),
    volume_idx: Number(v.volume_idx ?? v.volumeIdx ?? 0),
    summary: (v.summary as string | null) ?? null,
  }
}

/** Normalize a chapter from API (snake_case) to store format */
function normalizeChapter(c: Record<string, unknown>): Chapter {
  return {
    id: String(c.id),
    volumeId: String(c.volume_id ?? c.volumeId ?? ''),
    volume_id: String(c.volume_id ?? c.volumeId ?? ''),
    title: String(c.title ?? ''),
    chapterIdx: Number(c.chapter_idx ?? c.chapterIdx ?? 0),
    chapter_idx: Number(c.chapter_idx ?? c.chapterIdx ?? 0),
    contentText: String(c.content_text ?? c.contentText ?? ''),
    content_text: String(c.content_text ?? c.contentText ?? ''),
    wordCount: Number(c.word_count ?? c.wordCount ?? 0),
    word_count: Number(c.word_count ?? c.wordCount ?? 0),
    status: (c.status as 'draft' | 'generating' | 'completed') ?? 'draft',
    summary: (c.summary as string | null) ?? null,
    outline_json: (c.outline_json as Record<string, unknown>) ?? undefined,
  }
}

export { normalizeVolume, normalizeChapter }

export const useProjectStore = create<ProjectState>((set) => ({
  // Project list
  projects: [],
  projectsLoaded: false,
  setProjects: (projects) => set({ projects }),
  setProjectsLoaded: (loaded) => set({ projectsLoaded: loaded }),

  // Current project
  currentProject: null,
  volumes: [],
  chapters: [],
  selectedChapterId: null,
  setCurrentProject: (project) => set({ currentProject: project }),
  setVolumes: (volumes) => set({ volumes }),
  setChapters: (chapters) => set({ chapters }),
  addChapters: (newChapters) =>
    set((state) => {
      const existingIds = new Set(state.chapters.map((c) => c.id))
      const filtered = newChapters.filter((c) => !existingIds.has(c.id))
      return { chapters: [...state.chapters, ...filtered] }
    }),
  selectChapter: (id) => set({ selectedChapterId: id }),
  updateChapterContent: (id, content) =>
    set((state) => ({
      chapters: state.chapters.map((c) =>
        c.id === id
          ? {
              ...c,
              contentText: content,
              content_text: content,
              wordCount: content.length,
              word_count: content.length,
            }
          : c
      ),
    })),
  updateChapterStatus: (id, status) =>
    set((state) => ({
      chapters: state.chapters.map((c) =>
        c.id === id ? { ...c, status } : c
      ),
    })),
}))
