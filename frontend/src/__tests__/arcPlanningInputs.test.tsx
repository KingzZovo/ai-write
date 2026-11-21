import { fireEvent, render, screen } from '@testing-library/react'
import { expect, it } from 'vitest'
import { PlanningAttemptInputs, PlanningOriginal } from '@/components/arc/ArcPlanningInputs'

function open(label: string) {
  const element = screen.getByText(label).closest('details')!
  element.open = true
  fireEvent(element, new Event('toggle'))
}

it('mounts the complete retained cognition only when the author opens it', () => {
  const original = `开头\n${'人物当时误信，原句仍完整保留。'.repeat(800)}\n末尾`
  const { container } = render(<PlanningOriginal label="认知原件" text={original} />)
  expect(container.querySelector('pre')).toBeNull()
  open('认知原件')
  expect(container.querySelector('pre')?.textContent).toBe(original)
})

it('lets the author inspect every accepted, rejected and interrupted attempt input', () => {
  const inputs = ['首轮采用的未知记录与完整原句', '修复轮采用的后章获知与完整失败稿', '下一批已预占的完整材料']
  const journal = { version: 2, stages: [
    { attempts: [{ state: 'rejected', input: inputs[0] }, { state: 'accepted', input: inputs[1] }] },
    { attempts: [{ state: 'reserved', input: inputs[2] }] },
  ] }
  render(<PlanningAttemptInputs journal={journal} />)
  expect(screen.queryByText(inputs[0])).toBeNull()
  open('查看各轮实际创作材料')
  for (const label of ['第 1 阶段 · 第 1 次实际输入', '第 1 阶段 · 第 2 次实际输入', '第 2 阶段 · 第 1 次实际输入']) open(label)
  for (const input of inputs) expect(screen.getByText(input)).toBeTruthy()
})

it('shows the selected request input after switching records while an input is open', () => {
  const { rerender } = render(<PlanningAttemptInputs journal={{ stages: [{ attempts: [{ input: '旧请求材料' }] }] }} />)
  open('查看各轮实际创作材料'); open('第 1 阶段 · 第 1 次实际输入')
  rerender(<PlanningAttemptInputs journal={{ stages: [{ attempts: [{ input: '当前请求材料' }] }] }} />)
  expect(screen.getByText('当前请求材料')).toBeTruthy()
  expect(screen.queryByText('旧请求材料')).toBeNull()
})
