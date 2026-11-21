import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { NewProjectModal } from '@/components/project/NewProjectModal'
import { apiFetch } from '@/lib/api'

vi.mock('@/lib/api', () => ({ apiFetch: vi.fn() }))
afterEach(() => { cleanup(); vi.resetAllMocks() })

it('keeps an active library profile unselected until the author chooses it', async () => {
  vi.mocked(apiFetch).mockImplementation(async path => path === '/api/styles'
    ? [{ id: 'xianxia', name: '仙侠资料画像', is_active: 1, bind_level: 'global' }] : [])
  render(<NewProjectModal onClose={vi.fn()} onCreated={vi.fn()} />)
  const option = await screen.findByRole('option', { name: '仙侠资料画像' })
  const select = option.closest('select')!
  expect(select).toHaveValue('')
  fireEvent.change(select, { target: { value: 'xianxia' } })
  expect(select).toHaveValue('xianxia')
})
