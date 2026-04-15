import { create } from 'zustand'

interface Project {
  id: string
  title: string
  genre: string
  premise: string
}

interface Volume {
  id: string
  projectId: string
  title: string
  volumeIdx: number
}

interface Chapter {
  id: string
  volumeId: string
  title: string
  chapterIdx: number
  contentText: string
  wordCount: number
  status: 'draft' | 'generating' | 'completed'
}

interface ProjectState {
  currentProject: Project | null
  volumes: Volume[]
  chapters: Chapter[]
  selectedChapterId: string | null
  setCurrentProject: (project: Project | null) => void
  setVolumes: (volumes: Volume[]) => void
  setChapters: (chapters: Chapter[]) => void
  selectChapter: (id: string | null) => void
}

export const useProjectStore = create<ProjectState>((set) => ({
  currentProject: null,
  volumes: [],
  chapters: [],
  selectedChapterId: null,
  setCurrentProject: (project) => set({ currentProject: project }),
  setVolumes: (volumes) => set({ volumes }),
  setChapters: (chapters) => set({ chapters }),
  selectChapter: (id) => set({ selectedChapterId: id }),
}))
