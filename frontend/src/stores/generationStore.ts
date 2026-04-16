import { create } from 'zustand'

interface GenerationState {
  isGenerating: boolean
  streamContent: string
  setIsGenerating: (val: boolean) => void
  appendStreamContent: (text: string) => void
  resetStreamContent: () => void
}

export const useGenerationStore = create<GenerationState>((set) => ({
  isGenerating: false,
  streamContent: '',
  setIsGenerating: (val) => set({ isGenerating: val }),
  appendStreamContent: (text) => set((state) => ({ streamContent: state.streamContent + text })),
  resetStreamContent: () => set({ streamContent: '' }),
}))
