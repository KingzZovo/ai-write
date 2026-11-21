import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { RewriteMenu } from '@/components/editor/RewriteMenu'

vi.mock('@/lib/i18n/I18nProvider', () => ({ useT: () => (key: string) => key }))
afterEach(() => { cleanup(); vi.restoreAllMocks() })

it.each(['backend', 'eof'])('blocks partial rewrite acceptance after real SSE %s failure', async kind => {
  vi.spyOn(console, 'error').mockImplementation(() => {})
  const payload = 'data: {"text":"未完成的候选"}\n\n' + (kind === 'backend'
    ? 'data: {"error":"输出未完整结束","code":"rewrite_incomplete_response"}\n\ndata: [DONE]\n\n'
    : '')
  const body = new ReadableStream<Uint8Array>({ start(controller) {
    controller.enqueue(new TextEncoder().encode(payload)); controller.close()
  } })
  vi.spyOn(globalThis, 'fetch').mockResolvedValue({ ok: true, status: 200, body } as Response)
  const accept = vi.fn()
  render(<RewriteMenu selectedText="原稿" contextBefore="" contextAfter="" position={{top:0,left:0}} onAccept={accept} onReject={vi.fn()} onClose={vi.fn()} />)
  await act(async () => { fireEvent.click(screen.getByText('editor.rewrite.expand')) })
  await waitFor(() => expect(screen.getByText(/editor.rewrite.failed/)).toBeTruthy())
  const button = screen.getByText('editor.rewrite.accept')
  expect(button).toBeDisabled()
  fireEvent.click(button)
  expect(accept).not.toHaveBeenCalled()
})
