import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { RegenerateVolumeModal } from '@/components/outline/RegenerateVolumeModal'
import { apiSSE } from '@/lib/api'
vi.mock('@/lib/api', () => ({ apiSSE: vi.fn() }))
afterEach(() => { cleanup(); vi.resetAllMocks() })

it.each(['error', 'eof', 'success'])('requires committed done before notifying parent: %s', mode => {
  const onDone = vi.fn()
  vi.mocked(apiSSE).mockReturnValue(new AbortController())
  render(<RegenerateVolumeModal projectId="p" volumeId="v" volumeTitle="第一卷" chapterCount={3} onClose={vi.fn()} onDone={onDone} />)
  fireEvent.click(screen.getByText('确认重生'))
  const args = vi.mocked(apiSSE).mock.calls[0]
  act(() => {
    if (mode === 'error') args[4]?.({ status: 'failed', error: '旧内容未替换' })
    if (mode === 'success') args[4]?.({ status: 'done', chapters_created: 3 })
    args[3]?.()
  })
  expect(onDone).toHaveBeenCalledTimes(mode === 'success' ? 1 : 0)
  if (mode === 'error') expect(screen.getByText(/错误：旧内容未替换/)).toBeTruthy()
  if (mode === 'eof') expect(screen.getByText(/尚未收到完成确认/)).toBeTruthy()
})
