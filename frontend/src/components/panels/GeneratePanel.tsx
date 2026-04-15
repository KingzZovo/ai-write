'use client'

import React from 'react'
import { useGenerationStore } from '@/stores/generationStore'

interface GeneratePanelProps {
  onGenerate?: () => void
  onGenerateOutline?: (level: string) => void
}

export function GeneratePanel({ onGenerate, onGenerateOutline }: GeneratePanelProps) {
  const {
    isGenerating,
    selectedModel,
    temperature,
    maxTokens,
    setSelectedModel,
    setTemperature,
    setMaxTokens,
  } = useGenerationStore()

  return (
    <div className="p-4 space-y-6">
      <div>
        <h3 className="text-sm font-semibold text-gray-900 mb-3">Generation Settings</h3>

        <div className="space-y-4">
          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">Model</label>
            <select
              value={selectedModel}
              onChange={(e) => setSelectedModel(e.target.value)}
              className="w-full px-3 py-1.5 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent"
              disabled={isGenerating}
            >
              <optgroup label="Anthropic">
                <option value="claude-sonnet-4-20250514">Claude Sonnet 4</option>
                <option value="claude-opus-4-20250514">Claude Opus 4</option>
                <option value="claude-haiku-4-5-20251001">Claude Haiku 4.5</option>
              </optgroup>
              <optgroup label="OpenAI">
                <option value="gpt-4o">GPT-4o</option>
                <option value="gpt-4o-mini">GPT-4o Mini</option>
              </optgroup>
            </select>
          </div>

          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">
              Temperature: {temperature}
            </label>
            <input
              type="range"
              min="0"
              max="1"
              step="0.1"
              value={temperature}
              onChange={(e) => setTemperature(parseFloat(e.target.value))}
              className="w-full"
              disabled={isGenerating}
            />
          </div>

          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">Max Tokens</label>
            <input
              type="number"
              value={maxTokens}
              onChange={(e) => setMaxTokens(parseInt(e.target.value) || 4096)}
              className="w-full px-3 py-1.5 text-sm border border-gray-200 rounded-lg"
              min={256}
              max={16384}
              step={256}
              disabled={isGenerating}
            />
          </div>
        </div>
      </div>

      <div className="border-t pt-4">
        <h3 className="text-sm font-semibold text-gray-900 mb-3">Generate Content</h3>
        <div className="space-y-2">
          <button
            onClick={() => onGenerateOutline?.('book')}
            disabled={isGenerating}
            className="w-full px-4 py-2 text-sm bg-purple-600 text-white rounded-lg hover:bg-purple-700 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            Generate Book Outline
          </button>
          <button
            onClick={() => onGenerateOutline?.('volume')}
            disabled={isGenerating}
            className="w-full px-4 py-2 text-sm bg-indigo-600 text-white rounded-lg hover:bg-indigo-700 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            Generate Volume Outline
          </button>
          <button
            onClick={() => onGenerateOutline?.('chapter')}
            disabled={isGenerating}
            className="w-full px-4 py-2 text-sm bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            Generate Chapter Outline
          </button>
          <button
            onClick={onGenerate}
            disabled={isGenerating}
            className="w-full px-4 py-2 text-sm bg-green-600 text-white rounded-lg hover:bg-green-700 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {isGenerating ? 'Generating...' : 'Generate Chapter Content'}
          </button>
        </div>
      </div>

      <div className="border-t pt-4">
        <h3 className="text-sm font-semibold text-gray-900 mb-3">Style</h3>
        <textarea
          placeholder="Describe the target writing style... (e.g., 金庸武侠风格、余华现实主义风格)"
          className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg resize-none h-20 focus:ring-2 focus:ring-blue-500 focus:border-transparent"
          disabled={isGenerating}
        />
      </div>
    </div>
  )
}
