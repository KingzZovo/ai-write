import { afterEach, describe, expect, it, vi } from 'vitest'
import { apiSSE } from '@/lib/api'

afterEach(() => vi.restoreAllMocks())

async function receive(payload: string, options: { callbackError?: boolean; failRead?: boolean; bytewise?: boolean; omitEventHandler?: boolean } = {}) {
  vi.spyOn(console, 'error').mockImplementation(() => {})
  const bytes = new TextEncoder().encode(payload)
  let index = 0
  const cancel = vi.fn()
  const body = new ReadableStream<Uint8Array>({
    pull(controller) {
      if (index < bytes.length) {
        const end = options.bytewise ? index + 1 : bytes.length
        controller.enqueue(bytes.slice(index, end))
        index = end
      } else if (options.failRead) controller.error(new Error('connection lost'))
      else controller.close()
    }, cancel,
  })
  vi.spyOn(globalThis, 'fetch').mockResolvedValue({ ok: true, status: 200, body } as Response)
  const chunks: string[] = []
  const errors: Error[] = []
  const events: Record<string, unknown>[] = []
  let doneCount = 0
  await new Promise<void>((resolve) => {
    apiSSE('/api/generate/chapter', {}, text => {
      chunks.push(text)
      if (options.callbackError) throw new Error('callback failed')
    }, () => { doneCount++; resolve() }, options.omitEventHandler ? undefined : event => events.push(event), error => errors.push(error))
  })
  // Let the promise chain finish its reader cleanup as well.
  await new Promise(resolve => setTimeout(resolve, 0))
  expect(doneCount).toBe(1)
  expect(body.locked).toBe(false)
  return { chunks, errors, events, cancel }
}

describe('SSE response integrity', () => {
  it('decodes Chinese byte boundaries and CRLF framing through the sentinel', async () => {
    const r = await receive('data:{"text":"雨停了。"}\r\n\r\ndata: [DONE]\r\n\r\n', { bytewise: true })
    expect(r.chunks).toEqual(['雨停了。'])
    expect(r.errors).toEqual([])
  })

  it.each([false, true])('reports premature termination without discarding delivered text, read error=%s', async failRead => {
    const r = await receive('data: {"text":"雨停了。"}\n\n', { failRead })
    expect(r.chunks).toEqual(['雨停了。'])
    expect(r.errors).toHaveLength(1)
  })

  it('reports backend errors even without an application event handler', async () => {
    const r = await receive('data: {"text":"半句"}\n\ndata: {"error":"输出未完整结束"}\n\ndata: [DONE]\n\n', { omitEventHandler: true })
    expect(r.chunks).toEqual(['半句'])
    expect(r.errors[0].message).toBe('输出未完整结束')
  })

  it.each(['{bad json', 'null', '[]', '"not an event"'])('rejects malformed data without injecting it as prose: %s', async data => {
    const r = await receive(`data: ${data}\n\ndata: [DONE]\n\n`)
    expect(r.chunks).toEqual([])
    expect(r.errors).toHaveLength(1)
  })

  it('does not reinterpret callback failures as model text', async () => {
    const r = await receive('data: {"text":"正文"}\n\ndata: [DONE]\n\n', { callbackError: true })
    expect(r.chunks).toEqual(['正文'])
    expect(r.errors[0].message).toBe('callback failed')
  })
})

it('continues through named revision recovery errors to the final save', async () => {
  const r = await receive('data: {"event":"revise_error","error":"修订未完成"}\n\ndata: {"status":"saved"}\n\ndata: [DONE]\n\n')
  expect(r.events.map(e => e.event ?? e.status)).toEqual(['revise_error', 'saved'])
  expect(r.errors).toEqual([])
})

it('settles intentional cancellation once and releases the reader', async () => {
  let streamController!: ReadableStreamDefaultController<Uint8Array>
  const body = new ReadableStream<Uint8Array>({ start(controller) { streamController = controller } })
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (_input, init) => {
    init?.signal?.addEventListener('abort', () => {
      streamController.error(new DOMException('aborted', 'AbortError'))
    })
    return { ok: true, status: 200, body } as Response
  })
  const onError = vi.fn()
  const onDone = vi.fn()
  const controller = apiSSE('/api/rewrite', {}, () => {}, onDone, undefined, onError)
  await new Promise(resolve => setTimeout(resolve, 0))
  controller.abort()
  await new Promise(resolve => setTimeout(resolve, 0))
  expect(onDone).toHaveBeenCalledTimes(1)
  expect(onError).not.toHaveBeenCalled()
  expect(body.locked).toBe(false)
})
