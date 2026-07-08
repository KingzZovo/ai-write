import { describe, it, expect, beforeEach } from 'vitest'
import { useProjectStore } from '@/stores/projectStore'

describe('projectStore', () => {
  beforeEach(() => {
    useProjectStore.setState({
      projects: [],
      projectsLoaded: false,
      currentProject: null,
      volumes: [],
      chapters: [],
      selectedChapterId: null,
    })
  })

  it('setProjects updates project list', () => {
    const projects = [{ id: '1', title: 'Test Novel', genre: 'xianxia', premise: '' }]
    useProjectStore.getState().setProjects(projects)
    expect(useProjectStore.getState().projects).toHaveLength(1)
    expect(useProjectStore.getState().projects[0].title).toBe('Test Novel')
  })

  it('setCurrentProject sets currentProject', () => {
    const project = { id: '1', title: 'Test', genre: 'xianxia', premise: '' }
    useProjectStore.getState().setCurrentProject(project)
    expect(useProjectStore.getState().currentProject?.id).toBe('1')
  })

  it('selectChapter updates selectedChapterId', () => {
    useProjectStore.getState().selectChapter('ch-1')
    expect(useProjectStore.getState().selectedChapterId).toBe('ch-1')
  })

  it('updateChapterContent updates the correct chapter', () => {
    useProjectStore.setState({
      chapters: [
        { id: 'ch-1', volumeId: 'v1', title: '第一章', chapterIdx: 1, contentText: 'old', wordCount: 3, status: 'draft' },
        { id: 'ch-2', volumeId: 'v1', title: '第二章', chapterIdx: 2, contentText: 'other', wordCount: 5, status: 'draft' },
      ],
    })
    useProjectStore.getState().updateChapterContent('ch-1', '新内容')
    const ch = useProjectStore.getState().chapters.find(c => c.id === 'ch-1')
    expect(ch?.contentText).toBe('新内容')
  })
})
