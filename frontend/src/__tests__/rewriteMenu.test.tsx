import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { RewriteMenu } from '@/components/editor/RewriteMenu'
import { apiSSE } from '@/lib/api'
vi.mock('@/lib/api', () => ({ apiSSE: vi.fn() }))
vi.mock('@/lib/i18n/I18nProvider', () => ({ useT: () => (key: string) => key }))
afterEach(() => { cleanup(); vi.resetAllMocks() })
it('carries continuation operation to acceptance only after stream completion', () => {
  vi.mocked(apiSSE).mockReturnValue(new AbortController())
  const accept = vi.fn()
  render(<RewriteMenu selectedText="原选段。" contextBefore="前文" contextAfter="后文" position={{top:0,left:0}} onAccept={accept} onReject={vi.fn()} onClose={vi.fn()} />)
  fireEvent.click(screen.getByText('editor.rewrite.continue'))
  const call = vi.mocked(apiSSE).mock.calls[0]
  act(() => { call[2]('续写。') })
  fireEvent.click(screen.getByText('editor.rewrite.accept'))
  expect(accept).not.toHaveBeenCalled()
  act(() => { call[3]?.() })
  fireEvent.click(screen.getByText('editor.rewrite.accept'))
  expect(accept).toHaveBeenCalledWith('续写。','continue')
})
it('does not accept partial output after stream failure', () => {
  vi.mocked(apiSSE).mockReturnValue(new AbortController())
  const accept = vi.fn()
  render(<RewriteMenu selectedText="原选段。" contextBefore="" contextAfter="" position={{top:0,left:0}} onAccept={accept} onReject={vi.fn()} onClose={vi.fn()} />)
  fireEvent.click(screen.getByText('editor.rewrite.expand'))
  const call = vi.mocked(apiSSE).mock.calls[0]
  act(() => { call[2]('半稿。'); call[5]?.(new Error('截断')); call[3]?.() })
  fireEvent.click(screen.getByText('editor.rewrite.accept'))
  expect(accept).not.toHaveBeenCalled()
})
