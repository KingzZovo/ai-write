import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { SettingsPanel } from '@/components/panels/SettingsPanel'
import { ApiError, apiFetch } from '@/lib/api'

vi.mock('@/lib/api', async importOriginal => ({ ...await importOriginal<typeof import('@/lib/api')>(), apiFetch: vi.fn() }))
vi.mock('@/lib/i18n/I18nProvider', () => ({ useT: () => (key: string) => key }))

const original = { id: 'rule-one', category: '能力限制', rule_text: '铜铃损坏后不能复原。\n洪水期间也不例外。',
  version: 'a'.repeat(64), source_status: 'author', source_message: '作者当前登记的设定。' }
const draft = '作者改稿第一行。\n' + '水位未回落前不能通航；解除限制需要本段测量。'.repeat(130) + '\n保留末行例外。'
let rows: typeof original[]
const read = vi.fn()
const put = vi.fn()
const post = vi.fn()
const remove = vi.fn()

beforeEach(() => {
  rows = [{ ...original }]
  read.mockImplementation(async () => ({ world_rules: rows }))
  put.mockImplementation(async body => {
    rows = [{ ...original, category: body.new_category, rule_text: body.new_text, version: 'c'.repeat(64) }]
    return { status: 'accepted', entity: 'world_rule', category: body.new_category, text: body.new_text, deduplicated: false }
  })
  post.mockResolvedValue({ status: 'accepted', entity: 'world_rule', id: 'created-rule' })
  remove.mockResolvedValue({ status: 'accepted', entity: 'world_rule', deleted: 1 })
  vi.mocked(apiFetch).mockImplementation(async (path, options) => {
    const method = options?.method
    if (method === 'PUT') return put(JSON.parse(String(options?.body)))
    if (method === 'POST') return post(JSON.parse(String(options?.body)))
    if (method === 'DELETE') return remove(JSON.parse(String(options?.body)))
    if (path.endsWith('/characters')) return { characters: [] }
    return read()
  })
  vi.spyOn(window, 'confirm').mockReturnValue(true)
})
afterEach(() => { cleanup(); vi.resetAllMocks(); vi.restoreAllMocks() })

async function showWorld() {
  render(<SettingsPanel projectId="p" />)
  fireEvent.click(screen.getByRole('button', { name: /^世界规则/ }))
  await waitFor(() => expect(screen.queryByText('加载中...')).not.toBeInTheDocument())
}

async function edit() {
  await showWorld()
  fireEvent.click(screen.getByRole('button', { name: 'common.edit' }))
  fireEvent.change(screen.getByLabelText('规则全文'), { target: { value: draft } })
}

it('retains the entire draft and original version after a source conflict', async () => {
  put.mockRejectedValue(new ApiError('规则来源已变化，请保留输入并重新读取核对。', 409))
  await edit()
  fireEvent.click(screen.getByRole('button', { name: 'common.save' }))
  await screen.findByRole('alert')
  expect(screen.getByLabelText('规则全文')).toHaveValue(draft)
  expect(put.mock.calls[0][0]).toEqual({ category: original.category, old_text: original.rule_text,
    new_text: draft, new_category: original.category, expected_version: original.version })
  expect(screen.getByText('编辑世界规则')).toBeInTheDocument()
})

it('rereads full current text without silently changing the save basis; adoption is explicit', async () => {
  await edit()
  const changed = { ...original, rule_text: '服务器新版本。\n' + '其他例外也必须核对。'.repeat(200), version: 'b'.repeat(64) }
  rows = [changed]
  fireEvent.click(screen.getByRole('button', { name: '重新读取当前规则' }))
  await screen.findByText('采用读取版本，保留编辑稿')
  expect(document.querySelector('pre')?.textContent).toBe(changed.rule_text)
  expect(screen.getByLabelText('规则全文')).toHaveValue(draft)
  put.mockRejectedValueOnce(new ApiError('原版本已变化。', 409))
  fireEvent.click(screen.getByRole('button', { name: 'common.save' }))
  await screen.findByRole('alert')
  expect(put.mock.calls[0][0].expected_version).toBe(original.version)
  fireEvent.click(screen.getByRole('button', { name: '采用读取版本，保留编辑稿' }))
  expect(screen.getByLabelText('规则全文')).toHaveValue(draft)
  fireEvent.click(screen.getByRole('button', { name: 'common.save' }))
  await waitFor(() => expect(screen.queryByLabelText('规则全文')).not.toBeInTheDocument())
  expect(put.mock.calls[1][0]).toMatchObject({ expected_version: changed.version, old_text: changed.rule_text, new_text: draft })
})

it.each(['deleted', 'recreated'])('retains an editor when the original rule is %s, without adopting another node', async change => {
  await edit()
  rows = change === 'deleted' ? [] : [{ ...original, id: 'replacement-rule', version: 'b'.repeat(64) }]
  fireEvent.click(screen.getByRole('button', { name: '重新读取当前规则' }))
  await screen.findByText(/当前列表已没有原规则/)
  expect(screen.getByLabelText('规则全文')).toHaveValue(draft)
  expect(screen.queryByRole('button', { name: '采用读取版本，保留编辑稿' })).not.toBeInTheDocument()
  put.mockRejectedValueOnce(new ApiError('原规则已变化或不存在。', 404))
  fireEvent.click(screen.getByRole('button', { name: 'common.save' }))
  await screen.findByRole('alert')
  expect(put.mock.calls[0][0].expected_version).toBe(original.version)
  expect(screen.getByLabelText('规则全文')).toHaveValue(draft)
})

it('keeps failed additions and allows only one in-flight submission', async () => {
  let reject!: (reason: Error) => void
  post.mockImplementationOnce(() => new Promise((_resolve, fail) => { reject = fail }))
  await showWorld()
  fireEvent.click(screen.getByRole('button', { name: '+ 添加规则' }))
  fireEvent.change(screen.getByLabelText('规则分类'), { target: { value: '航运' } })
  fireEvent.change(screen.getByLabelText('规则全文'), { target: { value: draft } })
  const button = screen.getByRole('button', { name: '添加' })
  fireEvent.click(button); fireEvent.click(button)
  expect(post).toHaveBeenCalledTimes(1)
  expect(screen.getByRole('button', { name: /^人物/ })).toBeDisabled()
  await act(async () => { reject(new ApiError('写入结果尚未确认。', 503)) })
  await screen.findByRole('alert')
  expect(screen.getByLabelText('规则全文')).toHaveValue(draft)
  expect(screen.getByLabelText('规则分类')).toHaveValue('航运')
  expect(screen.getByRole('button', { name: '添加' })).toBeEnabled()
})

it.each(['edit', 'add'])('does not close the %s draft on an incomplete success response', async action => {
  if (action === 'edit') {
    put.mockResolvedValueOnce({ status: 'accepted', entity: 'world_rule' })
    await edit()
    fireEvent.click(screen.getByRole('button', { name: 'common.save' }))
  } else {
    post.mockResolvedValueOnce({ status: 'accepted', entity: 'world_rule' })
    await showWorld()
    fireEvent.click(screen.getByRole('button', { name: '+ 添加规则' }))
    fireEvent.change(screen.getByLabelText('规则分类'), { target: { value: '渡口' } })
    fireEvent.change(screen.getByLabelText('规则全文'), { target: { value: draft } })
    fireEvent.click(screen.getByRole('button', { name: '添加' }))
  }
  expect(await screen.findByRole('alert')).toHaveTextContent('回执不完整')
  expect(screen.getByLabelText('规则全文')).toHaveValue(draft)
})

it('keeps the rule visible until deletion is confirmed by a fresh canonical read', async () => {
  await showWorld()
  fireEvent.click(screen.getByRole('button', { name: 'common.delete' }))
  await screen.findByText(/当前仍能读取原规则，删除尚未确认/)
  expect(remove.mock.calls[0][0]).toEqual({ category: original.category, text: original.rule_text, expected_version: original.version })
  expect(screen.getByText(/铜铃损坏后不能复原/)).toBeInTheDocument()
  expect(screen.queryByText(/正在删除/)).not.toBeInTheDocument()
})

it('retains the last visible list when both deletion and its verification read fail', async () => {
  await showWorld()
  remove.mockRejectedValueOnce(new ApiError('删除结果尚未确认。', 503))
  read.mockRejectedValueOnce(new ApiError('当前规则无法读取。', 503))
  fireEvent.click(screen.getByRole('button', { name: 'common.delete' }))
  await screen.findByText(/删除结果未确认，请重新读取核对/)
  expect(screen.getByText(/铜铃损坏后不能复原/)).toBeInTheDocument()
  expect(screen.getByRole('alert')).toHaveTextContent('世界规则读取失败')
})

it('can confirm absence after an uncertain delete response by reading the actual graph', async () => {
  await showWorld()
  remove.mockImplementationOnce(async () => { rows = []; throw new ApiError('回执丢失。', 503) })
  fireEvent.click(screen.getByRole('button', { name: 'common.delete' }))
  await screen.findByText('已核对，原规则已删除。')
  expect(screen.queryByText(/铜铃损坏后不能复原/)).not.toBeInTheDocument()
})

it('shows the source problem and permits explicit author adoption with the original version', async () => {
  rows = [{ ...original, source_status: 'needs_review', source_message: '原提取来源已变化，请核对完整规则。' }]
  await showWorld()
  expect(screen.getByText(rows[0].source_message)).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '核对并编辑' }))
  expect(screen.getByLabelText('规则全文')).toHaveValue(original.rule_text)
  fireEvent.click(screen.getByRole('button', { name: 'common.save' }))
  await waitFor(() => expect(screen.queryByLabelText('规则全文')).not.toBeInTheDocument())
  expect(put.mock.calls[0][0]).toMatchObject({ expected_version: original.version, new_text: original.rule_text })
})
