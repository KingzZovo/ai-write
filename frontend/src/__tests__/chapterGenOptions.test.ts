import { describe, it, expect, beforeEach } from 'vitest'
import { getChapterGenOptions, saveChapterGenOptions } from '@/lib/chapterGenOptions'

describe('chapterGenOptions', () => {
  beforeEach(() => {
    localStorage.clear()
  })

  it('returns empty options for missing chapter / no stored value', () => {
    expect(getChapterGenOptions(null)).toEqual({ userInstruction: '', targetWords: null })
    expect(getChapterGenOptions('ch1')).toEqual({ userInstruction: '', targetWords: null })
  })

  it('round-trips saved options per chapter', () => {
    saveChapterGenOptions('ch1', { userInstruction: '多写对话', targetWords: 2500 })
    saveChapterGenOptions('ch2', { userInstruction: '', targetWords: 4000 })
    expect(getChapterGenOptions('ch1')).toEqual({ userInstruction: '多写对话', targetWords: 2500 })
    expect(getChapterGenOptions('ch2')).toEqual({ userInstruction: '', targetWords: 4000 })
  })

  it('removes the stored entry when both fields are empty', () => {
    saveChapterGenOptions('ch1', { userInstruction: '指令', targetWords: null })
    saveChapterGenOptions('ch1', { userInstruction: '  ', targetWords: null })
    expect(localStorage.getItem('chapter_gen_opts:ch1')).toBeNull()
  })

  it('sanitizes corrupted stored values', () => {
    localStorage.setItem('chapter_gen_opts:ch1', 'not json')
    expect(getChapterGenOptions('ch1')).toEqual({ userInstruction: '', targetWords: null })
    localStorage.setItem(
      'chapter_gen_opts:ch2',
      JSON.stringify({ userInstruction: 42, targetWords: -100 }),
    )
    expect(getChapterGenOptions('ch2')).toEqual({ userInstruction: '', targetWords: null })
  })
})
