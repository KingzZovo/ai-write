'use client'

import { useState, useEffect, useCallback } from 'react'
import { apiFetch } from '@/lib/api'

interface FilterWord {
  id: string
  word: string
  category: string
  severity: string
  replacement: string
  source: string
  enabled: number
  hit_count: number
}

const CATEGORY_LABELS: Record<string, string> = {
  ai_trace: 'AI 痕迹',
  cliche: '陈词滥调',
  banned: '禁用词',
  custom: '自定义',
}

const SEVERITY_COLORS: Record<string, string> = {
  high: 'bg-red-100 text-red-700',
  medium: 'bg-yellow-100 text-yellow-700',
  low: 'bg-gray-100 text-gray-600',
}

const SOURCE_LABELS: Record<string, string> = {
  builtin: '内置',
  user: '手动添加',
  ai_detected: 'AI 发现',
}

export default function FilterWordsPage() {
  const [words, setWords] = useState<FilterWord[]>([])
  const [categories, setCategories] = useState<Record<string, number>>({})
  const [loading, setLoading] = useState(true)
  const [filterCategory, setFilterCategory] = useState('')
  const [newWord, setNewWord] = useState('')
  const [newCategory, setNewCategory] = useState('custom')
  const [newSeverity, setNewSeverity] = useState('medium')
  const [newReplacement, setNewReplacement] = useState('')
  const [showAdd, setShowAdd] = useState(false)
  const [analyzeText, setAnalyzeText] = useState('')
  const [analyzeResult, setAnalyzeResult] = useState<any>(null)
  const [analyzing, setAnalyzing] = useState(false)
  const [detecting, setDetecting] = useState(false)
  const [detectResult, setDetectResult] = useState<any>(null)

  const fetchWords = useCallback(async () => {
    try {
      const url = filterCategory
        ? `/api/filter-words?category=${filterCategory}`
        : '/api/filter-words'
      const data = await apiFetch<{ words: FilterWord[]; total: number; categories: Record<string, number> }>(url)
      setWords(data.words)
      setCategories(data.categories)
    } catch { /* */ }
    finally { setLoading(false) }
  }, [filterCategory])

  useEffect(() => { fetchWords() }, [fetchWords])

  const handleAdd = async () => {
    if (!newWord.trim()) return
    try {
      await apiFetch('/api/filter-words', {
        method: 'POST',
        body: JSON.stringify({ word: newWord, category: newCategory, severity: newSeverity, replacement: newReplacement }),
      })
      setNewWord('')
      setNewReplacement('')
      setShowAdd(false)
      fetchWords()
    } catch (e) {
      alert(e instanceof Error ? e.message : '添加失败')
    }
  }

  const handleDelete = async (id: string) => {
    await apiFetch(`/api/filter-words/${id}`, { method: 'DELETE' })
    fetchWords()
  }

  const handleToggle = async (w: FilterWord) => {
    await apiFetch(`/api/filter-words/${w.id}`, {
      method: 'PUT',
      body: JSON.stringify({ enabled: !w.enabled }),
    })
    fetchWords()
  }

  const handleAnalyze = async () => {
    if (!analyzeText.trim()) return
    setAnalyzing(true)
    try {
      const data = await apiFetch<any>('/api/filter-words/analyze', {
        method: 'POST',
        body: JSON.stringify({ text: analyzeText }),
      })
      setAnalyzeResult(data)
    } catch { /* */ }
    finally { setAnalyzing(false) }
  }

  const handleAIDetect = async () => {
    if (!analyzeText.trim()) return
    setDetecting(true)
    try {
      const data = await apiFetch<any>('/api/filter-words/ai-detect', {
        method: 'POST',
        body: JSON.stringify({ text: analyzeText }),
      })
      setDetectResult(data)
      fetchWords()
    } catch { /* */ }
    finally { setDetecting(false) }
  }

  return (
    <div className="pt-14 px-4 md:px-8 max-w-4xl mx-auto pb-8">
      <h1 className="text-xl font-bold text-gray-900 mb-1">过滤词管理</h1>
      <p className="text-sm text-gray-500 mb-6">配置 Anti-AI 检测词库，支持手动添加和 AI 自动发现</p>

      {/* Category filter tabs */}
      <div className="flex gap-2 mb-4 overflow-x-auto">
        <button onClick={() => setFilterCategory('')}
          className={`px-3 py-1.5 text-xs rounded-full whitespace-nowrap ${!filterCategory ? 'bg-blue-600 text-white' : 'bg-gray-200 text-gray-600'}`}>
          全部 ({Object.values(categories).reduce((a, b) => a + b, 0) || words.length})
        </button>
        {Object.entries(categories).map(([cat, count]) => (
          <button key={cat} onClick={() => setFilterCategory(cat)}
            className={`px-3 py-1.5 text-xs rounded-full whitespace-nowrap ${filterCategory === cat ? 'bg-blue-600 text-white' : 'bg-gray-200 text-gray-600'}`}>
            {CATEGORY_LABELS[cat] || cat} ({count})
          </button>
        ))}
      </div>

      {/* Action buttons */}
      <div className="flex gap-2 mb-4">
        <button onClick={() => setShowAdd(!showAdd)}
          className="px-4 py-2 text-sm bg-blue-600 text-white rounded-lg">
          + 添加过滤词
        </button>
      </div>

      {/* Add form */}
      {showAdd && (
        <div className="bg-white rounded-lg border border-gray-200 p-4 mb-4 space-y-3">
          <div className="flex gap-2">
            <input value={newWord} onChange={e => setNewWord(e.target.value)}
              placeholder="输入过滤词" className="flex-1 px-3 py-2 text-sm border border-gray-200 rounded-lg" />
            <select value={newCategory} onChange={e => setNewCategory(e.target.value)}
              className="px-3 py-2 text-sm border border-gray-200 rounded-lg">
              <option value="ai_trace">AI 痕迹</option>
              <option value="cliche">陈词滥调</option>
              <option value="banned">禁用词</option>
              <option value="custom">自定义</option>
            </select>
            <select value={newSeverity} onChange={e => setNewSeverity(e.target.value)}
              className="px-3 py-2 text-sm border border-gray-200 rounded-lg">
              <option value="high">高</option>
              <option value="medium">中</option>
              <option value="low">低</option>
            </select>
          </div>
          <input value={newReplacement} onChange={e => setNewReplacement(e.target.value)}
            placeholder="替代建议（可选）" className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg" />
          <div className="flex gap-2">
            <button onClick={handleAdd} className="px-4 py-2 text-sm bg-green-600 text-white rounded-lg">确认添加</button>
            <button onClick={() => setShowAdd(false)} className="px-4 py-2 text-sm bg-gray-200 text-gray-700 rounded-lg">取消</button>
          </div>
        </div>
      )}

      {/* Word list */}
      {loading ? (
        <p className="text-sm text-gray-400">加载中...</p>
      ) : (
        <div className="flex flex-wrap gap-2 mb-8">
          {words.map(w => (
            <div key={w.id}
              className={`inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full border text-sm ${
                w.enabled ? 'bg-white border-gray-200' : 'bg-gray-50 border-gray-100 opacity-50'
              }`}>
              <span className={`w-2 h-2 rounded-full ${
                w.severity === 'high' ? 'bg-red-500' : w.severity === 'medium' ? 'bg-yellow-500' : 'bg-gray-400'
              }`} />
              <span className="text-gray-800">{w.word}</span>
              {w.replacement && <span className="text-gray-400 text-xs">→{w.replacement}</span>}
              {w.hit_count > 0 && <span className="text-[10px] text-gray-400">({w.hit_count})</span>}
              <span className="text-[10px] text-gray-300">{SOURCE_LABELS[w.source] || w.source}</span>
              <button onClick={() => handleToggle(w)}
                className="text-[10px] text-blue-500 hover:text-blue-700">
                {w.enabled ? '禁用' : '启用'}
              </button>
              <button onClick={() => handleDelete(w.id)}
                className="text-[10px] text-red-400 hover:text-red-600">×</button>
            </div>
          ))}
        </div>
      )}

      {/* AI Analysis section */}
      <div className="bg-white rounded-lg border border-gray-200 p-4 space-y-3">
        <h2 className="text-base font-semibold text-gray-900">AI 分析文本</h2>
        <p className="text-xs text-gray-500">粘贴小说文本，扫描已有过滤词或让 AI 发现新的 AI 痕迹</p>
        <textarea value={analyzeText} onChange={e => setAnalyzeText(e.target.value)}
          placeholder="粘贴需要分析的小说文本..."
          className="w-full h-32 px-3 py-2 text-sm border border-gray-200 rounded-lg resize-none" />
        <div className="flex gap-2">
          <button onClick={handleAnalyze} disabled={analyzing || !analyzeText.trim()}
            className="px-4 py-2 text-sm bg-blue-600 text-white rounded-lg disabled:opacity-50">
            {analyzing ? '扫描中...' : '扫描已有过滤词'}
          </button>
          <button onClick={handleAIDetect} disabled={detecting || !analyzeText.trim()}
            className="px-4 py-2 text-sm bg-purple-600 text-white rounded-lg disabled:opacity-50">
            {detecting ? 'AI 分析中...' : 'AI 自动发现新词'}
          </button>
        </div>

        {analyzeResult && (
          <div className="bg-gray-50 rounded-lg p-3">
            <h3 className="text-sm font-medium text-gray-700 mb-2">扫描结果：{analyzeResult.total_hits} 处命中</h3>
            <div className="flex flex-wrap gap-2">
              {analyzeResult.hits?.map((h: any, i: number) => (
                <span key={i} className={`px-2 py-1 rounded text-xs ${SEVERITY_COLORS[h.severity] || 'bg-gray-100'}`}>
                  {h.word} ×{h.count}
                </span>
              ))}
            </div>
          </div>
        )}

        {detectResult && (
          <div className="bg-purple-50 rounded-lg p-3">
            <h3 className="text-sm font-medium text-purple-700 mb-2">{detectResult.message}</h3>
            <div className="space-y-1">
              {detectResult.detected?.map((d: any, i: number) => (
                <div key={i} className="text-xs text-gray-700">
                  <span className="font-medium">{d.word}</span>
                  <span className="text-gray-400 ml-2">{d.reason}</span>
                  {d.replacement && <span className="text-green-600 ml-2">→ {d.replacement}</span>}
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
