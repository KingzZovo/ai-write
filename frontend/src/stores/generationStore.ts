import { create } from 'zustand'

interface GenerationState {
  isGenerating: boolean
  streamContent: string
  selectedModel: string
  temperature: number
  maxTokens: number
  setIsGenerating: (val: boolean) => void
  appendStreamContent: (text: string) => void
  resetStreamContent: () => void
  setSelectedModel: (model: string) => void
  setTemperature: (temp: number) => void
  setMaxTokens: (tokens: number) => void
}

export const useGenerationStore = create<GenerationState>((set) => ({
  isGenerating: false,
  streamContent: '',
  selectedModel: 'claude-sonnet-4-20250514',
  temperature: 0.7,
  maxTokens: 4096,
  setIsGenerating: (val) => set({ isGenerating: val }),
  appendStreamContent: (text) => set((state) => ({ streamContent: state.streamContent + text })),
  resetStreamContent: () => set({ streamContent: '' }),
  setSelectedModel: (model) => set({ selectedModel: model }),
  setTemperature: (temp) => set({ temperature: temp }),
  setMaxTokens: (tokens) => set({ maxTokens: tokens }),
}))
