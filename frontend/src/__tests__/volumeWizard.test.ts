import { beforeEach, expect, it, vi } from 'vitest'
import { apiFetch, apiSSE } from '@/lib/api'
import { generateSavedVolume, runVolumeWizard, type WizardState } from '@/lib/volumeWizard'
vi.mock('@/lib/api', () => ({ apiFetch: vi.fn(), apiSSE: vi.fn() }))
beforeEach(() => vi.resetAllMocks())

it.each(['eof', 'save_failed', 'error', 'saved_without_completed'])('rejects unconfirmed streams: %s', mode => {
  vi.mocked(apiSSE).mockReturnValue(new AbortController())
  const result = generateSavedVolume({}, vi.fn(), vi.fn())
  const args = vi.mocked(apiSSE).mock.calls[0]
  if (mode === 'save_failed') args[4]?.({ status: 'save_failed', error: '保存失败' })
  if (mode === 'error') args[5]?.(new Error('连接断开'))
  if (mode === 'saved_without_completed') args[4]?.({ status: 'saved', outline_id: 'o' })
  if (mode === 'save_failed') args[4]?.({ status: 'completed' })
  args[3]()
  return expect(result).rejects.toThrow()
})

it('fills an empty planned volume and reads server rows without duplicate writes', async () => {
  const state: WizardState = { volumes: [{ id: 'v', volume_idx: 1 }], chapters: [], outlines: [] }
  vi.mocked(apiFetch).mockImplementation(async (path) => {
    if (path.endsWith('/volumes')) return structuredClone(state.volumes)
    if (path.endsWith('/chapters')) return structuredClone(state.chapters)
    return structuredClone(state.outlines)
  })
  vi.mocked(apiSSE).mockImplementation((_path, body, _chunk, done, event) => {
    expect(body.planned_volume_count).toBe(1)
    state.chapters = [1, 2, 3].map(i => ({ id: `c${i}`, volume_id: 'v', chapter_idx: i, target_word_count: 1800 }))
    state.outlines = [{ id: 'o', level: 'volume', version: 1, is_confirmed: 0,
      content_json: { volume_id: 'v', volume_idx: 1, chapter_count: 3 } }]
    queueMicrotask(() => { event?.({ status: 'saved', outline_id: 'o' }); event?.({ status: 'completed' }); done() })
    return new AbortController()
  })
  const options = { projectId: 'p', bookId: 'b', count: 1, userInput: '', stopped: () => false,
    onController: vi.fn(), onProgress: vi.fn(), onState: vi.fn() }
  const saved = await runVolumeWizard(options)
  expect(saved?.chapters).toHaveLength(3)
  expect(apiSSE).toHaveBeenCalledTimes(1)
  expect(vi.mocked(apiFetch).mock.calls.every(call => call[1] === undefined)).toBe(true)
  await runVolumeWizard(options)
  expect(apiSSE).toHaveBeenCalledTimes(1) // Retry uses the already-saved result.
})

it('stops after one failed request and preserves previously loaded state', async () => {
  vi.mocked(apiFetch).mockResolvedValue([])
  vi.mocked(apiSSE).mockImplementation((_path, _body, _chunk, done, event) => {
    queueMicrotask(() => { event?.({ status: 'save_failed' }); done() })
    return new AbortController()
  })
  const onState = vi.fn()
  await expect(runVolumeWizard({ projectId: 'p', bookId: 'b', count: 3, userInput: '', stopped: () => false,
    onController: vi.fn(), onProgress: vi.fn(), onState })).rejects.toThrow()
  expect(apiSSE).toHaveBeenCalledTimes(1)
  expect(onState).toHaveBeenCalledWith({ volumes: [], chapters: [], outlines: [] })
})

it('does not trust a saved event when persisted chapter rows are absent', async () => {
  vi.mocked(apiFetch).mockResolvedValue([])
  vi.mocked(apiSSE).mockImplementation((_path, _body, _chunk, done, event) => {
    queueMicrotask(() => { event?.({ status: 'saved', outline_id: 'o' }); event?.({ status: 'completed' }); done() })
    return new AbortController()
  })
  await expect(runVolumeWizard({ projectId: 'p', bookId: 'b', count: 1, userInput: '', stopped: () => false,
    onController: vi.fn(), onProgress: vi.fn(), onState: vi.fn() })).rejects.toThrow('保存结果尚未核实')
})

it('does not start another volume or update state after leaving the wizard', async () => {
  let stopped = false
  vi.mocked(apiFetch).mockResolvedValue([])
  vi.mocked(apiSSE).mockImplementation((_path, _body, _chunk, done, event) => {
    queueMicrotask(() => {
      stopped = true
      event?.({ status: 'saved', outline_id: 'o' })
      event?.({ status: 'completed' })
      done()
    })
    return new AbortController()
  })
  const onState = vi.fn()
  const result = await runVolumeWizard({ projectId: 'p', bookId: 'b', count: 3, userInput: '',
    stopped: () => stopped, onController: vi.fn(), onProgress: vi.fn(), onState })
  expect(result).toBeNull()
  expect(apiSSE).toHaveBeenCalledTimes(1)
  expect(apiFetch).toHaveBeenCalledTimes(3)
  expect(onState).toHaveBeenCalledTimes(1)
})
