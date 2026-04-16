import { create } from 'zustand'
import { apiFetch } from '@/lib/api'

export interface BookSource {
  id: string
  name: string
  sourceUrl: string
  sourceGroup: string | null
  enabled: number
  lastTestOk: number
}

export interface ReferenceBook {
  id: string
  title: string
  author: string | null
  source: string
  total_chapters: number
  total_words: number
  status: string
  metadata_json: Record<string, unknown>
}

export interface CrawlTask {
  id: string
  book_id: string
  book_url: string
  total_chapters: number
  completed_chapters: number
  status: string
}

interface KnowledgeState {
  sources: BookSource[]
  books: ReferenceBook[]
  crawlTasks: CrawlTask[]
  loading: boolean
  error: string | null

  setSources: (sources: BookSource[]) => void
  setBooks: (books: ReferenceBook[]) => void
  setCrawlTasks: (tasks: CrawlTask[]) => void
  setLoading: (loading: boolean) => void
  setError: (error: string | null) => void

  fetchSources: () => Promise<void>
  fetchBooks: () => Promise<void>
  fetchCrawlTasks: () => Promise<void>

  importSources: (sourcesJson: unknown[]) => Promise<void>
  deleteSource: (id: string) => Promise<void>
  deleteBook: (id: string) => Promise<void>
  scoreBook: (id: string) => Promise<void>
}

export const useKnowledgeStore = create<KnowledgeState>((set, get) => ({
  sources: [],
  books: [],
  crawlTasks: [],
  loading: false,
  error: null,

  setSources: (sources) => set({ sources }),
  setBooks: (books) => set({ books }),
  setCrawlTasks: (tasks) => set({ crawlTasks: tasks }),
  setLoading: (loading) => set({ loading }),
  setError: (error) => set({ error }),

  fetchSources: async () => {
    try {
      set({ loading: true, error: null })
      const data = await apiFetch<BookSource[]>('/api/knowledge/sources')
      set({ sources: data, loading: false })
    } catch (err) {
      set({ error: err instanceof Error ? err.message : 'Failed to fetch sources', loading: false })
    }
  },

  fetchBooks: async () => {
    try {
      set({ loading: true, error: null })
      const data = await apiFetch<ReferenceBook[]>('/api/knowledge/books')
      set({ books: data, loading: false })
    } catch (err) {
      set({ error: err instanceof Error ? err.message : 'Failed to fetch books', loading: false })
    }
  },

  fetchCrawlTasks: async () => {
    try {
      set({ loading: true, error: null })
      const data = await apiFetch<CrawlTask[]>('/api/knowledge/crawl-tasks')
      set({ crawlTasks: data, loading: false })
    } catch (err) {
      set({ error: err instanceof Error ? err.message : 'Failed to fetch crawl tasks', loading: false })
    }
  },

  importSources: async (sourcesJson) => {
    try {
      set({ loading: true, error: null })
      await apiFetch('/api/knowledge/sources/import', {
        method: 'POST',
        body: JSON.stringify({ sources_json: sourcesJson }),
      })
      await get().fetchSources()
    } catch (err) {
      set({ error: err instanceof Error ? err.message : 'Failed to import sources', loading: false })
    }
  },

  deleteSource: async (id) => {
    try {
      set({ error: null })
      await apiFetch(`/api/knowledge/sources/${id}`, { method: 'DELETE' })
      set((state) => ({ sources: state.sources.filter((s) => s.id !== id) }))
    } catch (err) {
      set({ error: err instanceof Error ? err.message : 'Failed to delete source' })
    }
  },

  deleteBook: async (id) => {
    try {
      set({ error: null })
      await apiFetch(`/api/knowledge/books/${id}`, { method: 'DELETE' })
      set((state) => ({ books: state.books.filter((b) => b.id !== id) }))
    } catch (err) {
      set({ error: err instanceof Error ? err.message : 'Failed to delete book' })
    }
  },

  scoreBook: async (id) => {
    try {
      set({ error: null })
      await apiFetch(`/api/knowledge/books/${id}/score`, { method: 'POST' })
      await get().fetchBooks()
    } catch (err) {
      set({ error: err instanceof Error ? err.message : 'Failed to score book' })
    }
  },
}))
