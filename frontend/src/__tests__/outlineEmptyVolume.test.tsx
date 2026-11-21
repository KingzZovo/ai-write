import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { OutlineTree } from '@/components/outline/OutlineTree'
import { normalizeVolume, useProjectStore } from '@/stores/projectStore'

afterEach(() => { cleanup(); useProjectStore.setState({ volumes: [], chapters: [], selectedChapterId: null }) })

it('opens a newly created volume for its first author outline using the stable volume ID', async () => {
  useProjectStore.setState({ volumes: [normalizeVolume({ id: 'empty-volume', project_id: 'p', volume_idx: 7, title: '新增卷' })],
    chapters: [], selectedChapterId: null })
  const open = vi.fn()
  render(<OutlineTree projectId="p" volumeOutlines={{}} volumeOutlineIds={{}} onEditVolume={open} />)
  const label = screen.getByText('新增卷')
  if (!screen.queryByRole('button', { name: /编写本卷大纲/ })) fireEvent.click(label)
  fireEvent.click(await screen.findByRole('button', { name: /编写本卷大纲/ }))
  expect(open).toHaveBeenCalledWith('empty-volume')
})
