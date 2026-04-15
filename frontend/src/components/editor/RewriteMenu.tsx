'use client'

import React, { useState, useCallback, useRef, useEffect } from 'react'
import { apiSSE } from '@/lib/api'

type Operation = 'condense' | 'expand' | 'restructure' | 'continue' | 'custom'

interface RewriteMenuProps {
  selectedText: string
  contextBefore: string
  contextAfter: string
  position: { top: number; left: number }
  onAccept: (newText: string) => void
  onReject: () => void
  onClose: () => void
}

const OPERATIONS: { key: Operation; label: string; labelZh: string }[] = [
  { key: 'condense', label: 'Condense', labelZh: '缩写' },
  { key: 'expand', label: 'Expand', labelZh: '扩写' },
  { key: 'restructure', label: 'Restructure', labelZh: '重构' },
  { key: 'continue', label: 'Continue', labelZh: '续写' },
  { key: 'custom', label: 'Custom', labelZh: '自定义' },
]

export function RewriteMenu({
  selectedText,
  contextBefore,
  contextAfter,
  position,
  onAccept,
  onReject,
  onClose,
}: RewriteMenuProps) {
  const [rewrittenText, setRewrittenText] = useState('')
  const [isLoading, setIsLoading] = useState(false)
  const [showCustomInput, setShowCustomInput] = useState(false)
  const [customInstruction, setCustomInstruction] = useState('')
  const [showPreview, setShowPreview] = useState(false)
  const controllerRef = useRef<AbortController | null>(null)
  const menuRef = useRef<HTMLDivElement>(null)

  // Close on outside click
  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        if (!showPreview) {
          onClose()
        }
      }
    }
    document.addEventListener('mousedown', handleClickOutside)
    return () => document.removeEventListener('mousedown', handleClickOutside)
  }, [onClose, showPreview])

  const handleRewrite = useCallback(
    (operation: Operation, instruction?: string) => {
      if (isLoading) return
      setIsLoading(true)
      setRewrittenText('')
      setShowPreview(true)
      setShowCustomInput(false)

      const controller = apiSSE(
        '/api/rewrite',
        {
          selected_text: selectedText,
          operation,
          custom_instruction: instruction || '',
          context_before: contextBefore,
          context_after: contextAfter,
        },
        (text) => {
          setRewrittenText((prev) => prev + text)
        },
        () => {
          setIsLoading(false)
        }
      )
      controllerRef.current = controller
    },
    [isLoading, selectedText, contextBefore, contextAfter]
  )

  const handleOperationClick = useCallback(
    (op: Operation) => {
      if (op === 'custom') {
        setShowCustomInput(true)
        return
      }
      handleRewrite(op)
    },
    [handleRewrite]
  )

  const handleCustomSubmit = useCallback(() => {
    if (!customInstruction.trim()) return
    handleRewrite('custom', customInstruction)
  }, [customInstruction, handleRewrite])

  const handleAccept = useCallback(() => {
    onAccept(rewrittenText)
  }, [rewrittenText, onAccept])

  const handleReject = useCallback(() => {
    if (controllerRef.current) {
      controllerRef.current.abort()
    }
    setRewrittenText('')
    setShowPreview(false)
    setIsLoading(false)
    onReject()
  }, [onReject])

  return (
    <div
      ref={menuRef}
      className="fixed z-50"
      style={{ top: position.top, left: position.left }}
    >
      {/* Operation buttons */}
      {!showPreview && (
        <div className="bg-white rounded-lg shadow-lg border border-gray-200 p-1.5 flex gap-1 items-center">
          {OPERATIONS.map((op) => (
            <button
              key={op.key}
              onClick={() => handleOperationClick(op.key)}
              className="px-2.5 py-1.5 text-xs font-medium text-gray-700 rounded-md hover:bg-blue-50 hover:text-blue-700 transition-colors whitespace-nowrap"
              title={op.labelZh}
            >
              {op.label}
              <span className="ml-1 text-gray-400">{op.labelZh}</span>
            </button>
          ))}
        </div>
      )}

      {/* Custom instruction input */}
      {showCustomInput && !showPreview && (
        <div className="mt-1 bg-white rounded-lg shadow-lg border border-gray-200 p-2.5 w-72">
          <textarea
            value={customInstruction}
            onChange={(e) => setCustomInstruction(e.target.value)}
            placeholder="Describe how to rewrite..."
            className="w-full px-2 py-1.5 text-xs border border-gray-200 rounded resize-none h-16 focus:ring-2 focus:ring-blue-500 focus:border-transparent"
            autoFocus
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault()
                handleCustomSubmit()
              }
            }}
          />
          <div className="flex gap-1.5 mt-1.5">
            <button
              onClick={handleCustomSubmit}
              disabled={!customInstruction.trim()}
              className="flex-1 px-2 py-1 text-xs bg-blue-600 text-white rounded hover:bg-blue-700 disabled:opacity-50"
            >
              Rewrite
            </button>
            <button
              onClick={() => setShowCustomInput(false)}
              className="flex-1 px-2 py-1 text-xs bg-gray-100 text-gray-600 rounded hover:bg-gray-200"
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {/* Diff preview */}
      {showPreview && (
        <div className="bg-white rounded-lg shadow-lg border border-gray-200 p-3 w-96 max-h-80 overflow-y-auto">
          <h4 className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-2">
            Rewrite Preview
          </h4>

          {/* Original text with strikethrough */}
          <div className="mb-2">
            <span className="text-[10px] font-medium text-red-500 uppercase">Original</span>
            <div className="mt-0.5 px-2 py-1.5 bg-red-50 border border-red-100 rounded text-xs text-gray-600 line-through leading-relaxed">
              {selectedText}
            </div>
          </div>

          {/* Rewritten text highlighted green */}
          <div className="mb-3">
            <span className="text-[10px] font-medium text-green-600 uppercase">
              {isLoading ? 'Generating...' : 'New'}
            </span>
            <div className="mt-0.5 px-2 py-1.5 bg-green-50 border border-green-100 rounded text-xs text-gray-800 leading-relaxed min-h-[2rem]">
              {rewrittenText || (
                <span className="text-gray-400 animate-pulse">Generating rewrite...</span>
              )}
            </div>
          </div>

          {/* Accept / Reject buttons */}
          <div className="flex gap-1.5">
            <button
              onClick={handleAccept}
              disabled={isLoading || !rewrittenText}
              className="flex-1 px-3 py-1.5 text-xs bg-green-600 text-white rounded-lg hover:bg-green-700 disabled:opacity-50 disabled:cursor-not-allowed font-medium"
            >
              Accept
            </button>
            <button
              onClick={handleReject}
              className="flex-1 px-3 py-1.5 text-xs bg-gray-100 text-gray-700 rounded-lg hover:bg-gray-200 font-medium"
            >
              Reject
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
