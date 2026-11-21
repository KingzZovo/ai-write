import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { apiFetch } from '@/lib/api'
import { OutlineTree } from '@/components/outline/OutlineTree'
import { chapterPositionLabel, moveAdjacentChapter } from '@/lib/chapterOrder'
import { normalizeChapter, normalizeVolume, useProjectStore } from '@/stores/projectStore'

vi.mock('@/lib/api', () => ({ apiFetch: vi.fn() }))
vi.mock('@/components/outline/DeleteVolumeModal', () => ({ DeleteVolumeModal: () => null }))
vi.mock('@/components/outline/DeleteChapterModal', () => ({ DeleteChapterModal: () => null }))
vi.mock('@/components/outline/RegenerateVolumeModal', () => ({ RegenerateVolumeModal: () => null }))
afterEach(() => { cleanup(); vi.resetAllMocks(); useProjectStore.setState({ volumes: [], chapters: [] }) })

function book(projectId = 'book') {
  const chapters = [normalizeChapter({ id:'first',volume_id:'volume',chapter_idx:4,global_idx:1,
    title:'河岸',order_version:'a'.repeat(64) }), normalizeChapter({ id:'second',volume_id:'volume',chapter_idx:9,
    global_idx:2,title:'渡口',order_version:'b'.repeat(64) })]
  useProjectStore.setState({ chapters, volumes:[normalizeVolume({id:'volume',project_id:projectId,volume_idx:2,title:'返乡'})] })
  return chapters
}
function menu() { fireEvent.click(screen.getAllByRole('button', {name:'more'})[1]) }
function down() { menu(); fireEvent.click(screen.getByRole('button', {name:'下移'})) }
function ack() { return {chapter_id:'first',neighbor_id:'second',chapter_idx:9,neighbor_chapter_idx:4} }
function deferred() {
  let resolve!: (result: unknown) => void
  const promise = new Promise<unknown>(yes => { resolve = yes })
  return {promise,resolve}
}

it('sends one versioned swap request and displays saved global positions', async () => {
  book(); vi.mocked(apiFetch).mockResolvedValue(ack())
  const refresh = vi.fn()
  render(<OutlineTree projectId="book" onChanged={refresh} />)
  fireEvent.click(screen.getByText('返乡'))
  expect(screen.getByText('第1章 河岸')).toBeTruthy()
  expect(screen.getByText('第2章 渡口')).toBeTruthy()
  down()
  await waitFor(() => expect(refresh).toHaveBeenCalledOnce())
  expect(apiFetch).toHaveBeenCalledTimes(1)
  const [path,options] = vi.mocked(apiFetch).mock.calls[0]
  expect(path).toBe('/api/projects/book/chapters/first/move')
  expect(options?.method).toBe('POST')
  expect(JSON.parse(options?.body as string)).toEqual({neighbor_id:'second',
    expected_version:'a'.repeat(64),expected_neighbor_version:'b'.repeat(64)})
})

it('blocks duplicate moves until a new chapter list arrives, including uncertain saves', async () => {
  const chapters = book(), pending = deferred(), refresh = vi.fn()
  vi.mocked(apiFetch).mockImplementation(() => pending.promise)
  render(<OutlineTree projectId="book" onChanged={refresh} />)
  fireEvent.click(screen.getByText('返乡')); down(); menu()
  expect(screen.getByRole('button',{name:'下移'})).toHaveProperty('disabled',true)
  fireEvent.click(screen.getByRole('button',{name:'下移'}))
  expect(apiFetch).toHaveBeenCalledTimes(1)
  await act(async () => pending.resolve({}))
  expect(await screen.findByRole('alert')).toHaveProperty('textContent',expect.stringContaining('尚未确认'))
  expect(refresh).toHaveBeenCalledOnce()
  expect(screen.getByRole('button',{name:'下移'})).toHaveProperty('disabled',true)
  fireEvent.click(screen.getByText('重新读取章节'))
  expect(refresh).toHaveBeenCalledTimes(2)
  expect(apiFetch).toHaveBeenCalledTimes(1)
  await act(async () => useProjectStore.setState({chapters:[...chapters]}))
  expect(screen.getByRole('button',{name:'下移'})).toHaveProperty('disabled',false)
})

it('shows conflicts and refreshes without attempting a second write', async () => {
  book(); vi.mocked(apiFetch).mockRejectedValue(new Error('章节或顺序已变化'))
  const refresh = vi.fn()
  render(<OutlineTree projectId="book" onChanged={refresh} />)
  fireEvent.click(screen.getByText('返乡')); down()
  expect(await screen.findByRole('alert')).toHaveProperty('textContent','章节或顺序已变化')
  expect(refresh).toHaveBeenCalledOnce()
  expect(apiFetch).toHaveBeenCalledTimes(1)
})

it('does not refresh or lock a different project when the previous save returns late', async () => {
  book(); const pending = deferred(), oldRefresh = vi.fn(), newRefresh = vi.fn()
  vi.mocked(apiFetch).mockImplementation(() => pending.promise)
  const view = render(<OutlineTree projectId="book" onChanged={oldRefresh} />)
  fireEvent.click(screen.getByText('返乡')); down()
  view.rerender(<OutlineTree projectId="other" onChanged={newRefresh} />)
  fireEvent.click(screen.getByText('返乡'))
  await act(async () => pending.resolve(ack()))
  expect(oldRefresh).not.toHaveBeenCalled()
  expect(newRefresh).not.toHaveBeenCalled()
  expect(screen.queryByRole('alert')).toBeNull()
  menu()
  expect(screen.getByRole('button',{name:'下移'})).toHaveProperty('disabled',false)
})

it('requires current order versions and never invents a global position for old metadata', async () => {
  const [first,second] = book()
  first.order_version = null; first.global_idx = null
  expect(chapterPositionLabel(first)).toBe('卷内第4章')
  await expect(moveAdjacentChapter('book',first,second)).rejects.toThrow('重新读取')
  expect(apiFetch).not.toHaveBeenCalled()
})
