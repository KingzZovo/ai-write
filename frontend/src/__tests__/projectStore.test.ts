import { describe, it, expect, beforeEach } from 'vitest'
import { useProjectStore, normalizeVolume, normalizeChapter } from '@/stores/projectStore'

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
    const projects = [
      { id: '1', title: 'Test Novel', genre: 'fantasy', premise: 'A hero rises' },
    ]
    useProjectStore.getState().setProjects(projects)
    expect(useProjectStore.getState().projects).toHaveLength(1)
    expect(useProjectStore.getState().projects[0].title).toBe('Test Novel')
  })

  it('setCurrentProject sets and clears currentProject', () => {
    const project = { id: '1', title: 'Test', genre: 'xianxia', premise: 'Cultivation' }
    useProjectStore.getState().setCurrentProject(project)
    expect(useProjectStore.getState().currentProject?.id).toBe('1')

    useProjectStore.getState().setCurrentProject(null)
    expect(useProjectStore.getState().currentProject).toBeNull()
  })

  it('normalizeVolume converts snake_case API response', () => {
    const raw = { id: 'v1', project_id: 'p1', title: 'Volume 1', volume_idx: 0, summary: null }
    const vol = normalizeVolume(raw)
    expect(vol.volumeIdx).toBe(0)
    expect(vol.projectId).toBe('p1')
    expect(vol.title).toBe('Volume 1')
  })

  it('normalizeChapter converts snake_case API response', () => {
    const raw = {
      id: 'c1', volume_id: 'v1', title: 'Ch 1', chapter_idx: 1,
      content_text: 'Hello', word_count: 100, status: 'draft',
    }
    const ch = normalizeChapter(raw)
    expect(ch.chapterIdx).toBe(1)
    expect(ch.contentText).toBe('Hello')
    expect(ch.wordCount).toBe(100)
  })

  it('updateChapterContent updates the right chapter', () => {
    useProjectStore.setState({
      chapters: [
        { id: 'c1', volumeId: 'v1', title: 'Ch1', chapterIdx: 1, contentText: 'old', wordCount: 3, status: 'draft' as const },
        { id: 'c2', volumeId: 'v1', title: 'Ch2', chapterIdx: 2, contentText: 'other', wordCount: 5, status: 'draft' as const },
      ],
    })
    useProjectStore.getState().updateChapterContent('c1', 'new content')
    const chapters = useProjectStore.getState().chapters
    expect(chapters[0].contentText).toBe('new content')
    expect(chapters[1].contentText).toBe('other')
  })

  it('selectChapter updates selectedChapterId', () => {
    useProjectStore.getState().selectChapter('c1')
    expect(useProjectStore.getState().selectedChapterId).toBe('c1')

    useProjectStore.getState().selectChapter(null)
    expect(useProjectStore.getState().selectedChapterId).toBeNull()
  })

  it('addChapters deduplicates by id', () => {
    useProjectStore.setState({
      chapters: [
        { id: 'c1', volumeId: 'v1', title: 'Ch1', chapterIdx: 1, contentText: '', wordCount: 0, status: 'draft' as const },
      ],
    })
    useProjectStore.getState().addChapters([
      { id: 'c1', volumeId: 'v1', title: 'Ch1', chapterIdx: 1, contentText: 'dup', wordCount: 0, status: 'draft' as const },
      { id: 'c2', volumeId: 'v1', title: 'Ch2', chapterIdx: 2, contentText: 'new', wordCount: 10, status: 'draft' as const },
    ])
    const chapters = useProjectStore.getState().chapters
    expect(chapters).toHaveLength(2)
    expect(chapters[0].contentText).toBe('')
    expect(chapters[1].id).toBe('c2')
  })
})
