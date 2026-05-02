'use client'

import React, { useEffect, useRef, useCallback } from 'react'
import { EditorState } from 'prosemirror-state'
import { EditorView } from 'prosemirror-view'
import { Schema, DOMParser as ProseDOMParser } from 'prosemirror-model'
import { schema as basicSchema } from 'prosemirror-schema-basic'
import { addListNodes } from 'prosemirror-schema-list'
import { keymap } from 'prosemirror-keymap'
import { baseKeymap } from 'prosemirror-commands'
import { history, undo, redo } from 'prosemirror-history'
import { dropCursor } from 'prosemirror-dropcursor'
import { gapCursor } from 'prosemirror-gapcursor'

// Extended schema with AI-generated content marker
const aiWriteSchema = new Schema({
  nodes: addListNodes(basicSchema.spec.nodes, 'paragraph block*', 'block'),
  marks: basicSchema.spec.marks,
})

interface ChapterEditorProps {
  content: string
  onChange?: (text: string) => void
  isStreaming?: boolean
  streamingContent?: string
  className?: string
}

export function ChapterEditor({
  content,
  onChange,
  isStreaming = false,
  streamingContent = '',
  className = '',
}: ChapterEditorProps) {
  const editorRef = useRef<HTMLDivElement>(null)
  const viewRef = useRef<EditorView | null>(null)

  // Initialize ProseMirror
  useEffect(() => {
    if (!editorRef.current || viewRef.current) return

    const doc = createDocFromText(content)
    const state = EditorState.create({
      doc,
      plugins: [
        history(),
        keymap({ 'Mod-z': undo, 'Mod-Shift-z': redo }),
        keymap(baseKeymap),
        dropCursor(),
        gapCursor(),
      ],
    })

    const view = new EditorView(editorRef.current, {
      state,
      dispatchTransaction(transaction) {
        const newState = view.state.apply(transaction)
        view.updateState(newState)
        if (transaction.docChanged && onChange) {
          onChange(getTextContent(newState))
        }
      },
    })

    viewRef.current = view

    return () => {
      view.destroy()
      viewRef.current = null
    }
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  // Handle streaming content insertion
  useEffect(() => {
    if (!viewRef.current || !isStreaming || !streamingContent) return

    const view = viewRef.current
    const { state } = view
    const endPos = state.doc.content.size
    const tr = state.tr.insertText(streamingContent, endPos)
    view.dispatch(tr)
  }, [streamingContent, isStreaming])

  return (
    <div className={`chapter-editor ${className}`}>
      <div
        ref={editorRef}
        className="prose prose-lg max-w-none min-h-[500px] p-6 focus:outline-none"
      />
      {isStreaming && (
        <div className="fixed bottom-4 right-4 bg-blue-500 text-white px-4 py-2 rounded-full text-sm animate-pulse">
          AI generating...
        </div>
      )}
      <style jsx global>{`
        .ProseMirror {
          outline: none;
          min-height: 500px;
          font-family: 'Noto Serif SC', 'Source Han Serif SC', serif;
          font-size: 16px;
          line-height: 1.8;
          color: var(--text);
        }
        .ProseMirror p {
          margin-bottom: 0.8em;
          text-indent: 2em;
        }
        .ProseMirror:focus {
          outline: none;
        }
        .ai-generated {
          background-color: var(--color-info-50);
          border-left: 2px solid var(--color-info-500);
          padding-left: 0.5em;
        }
      `}</style>
    </div>
  )
}

function createDocFromText(text: string) {
  const element = document.createElement('div')
  if (text) {
    const paragraphs = text.split('\n').filter(Boolean)
    element.innerHTML = paragraphs.map((p) => `<p>${p}</p>`).join('')
  } else {
    element.innerHTML = '<p></p>'
  }
  return ProseDOMParser.fromSchema(aiWriteSchema).parse(element)
}

function getTextContent(state: EditorState): string {
  const paragraphs: string[] = []
  state.doc.forEach((node) => {
    paragraphs.push(node.textContent)
  })
  return paragraphs.join('\n')
}
