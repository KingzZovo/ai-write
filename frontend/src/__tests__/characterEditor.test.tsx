import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { CharacterProfileEditor } from '@/components/panels/CharacterProfileEditor'
import { SettingsPanel } from '@/components/panels/SettingsPanel'
import { apiFetch, ApiError } from '@/lib/api'
import CharactersPage from '@/app/characters/page'

vi.mock('@/lib/api', async importOriginal => ({ ...await importOriginal<typeof import('@/lib/api')>(), apiFetch: vi.fn() }))
vi.mock('@/lib/i18n/I18nProvider', () => ({ useT: () => (key: string) => key }))
const route = vi.hoisted(() => ({ project: 'p' }))
vi.mock('next/navigation', () => ({ useSearchParams: () => new URLSearchParams({ id: route.project }) }))
const api = vi.mocked(apiFetch)
const original = { id: 'c', name: '周砚', profile_json: { identity: '师父', personality: '沉稳', aliases: ['老周'], details: { craft: '修书' } } }
let stored: Record<string, unknown>
const post = vi.fn()
const remove = vi.fn()
const get = vi.fn()
beforeEach(() => {
  sessionStorage.clear()
  route.project = 'p'
  stored = original.profile_json
  post.mockImplementation(async (_path, options) => {
    stored = JSON.parse(options.body).profile_json
    return { status: 'accepted', entity: 'character', name: original.name }
  })
  get.mockImplementation(async () => ({ characters: [{ ...original, profile_json: stored }] }))
  api.mockImplementation((path, options) => options?.method === 'POST' ? post(path, options) : options?.method === 'DELETE' ? remove(path, options) : get(path, options))
})
afterEach(() => { cleanup(); vi.useRealTimers(); vi.resetAllMocks(); vi.restoreAllMocks() })

it('saves the latest edits together and preserves hidden structured fields', async () => {
  const saved = vi.fn()
  render(<CharacterProfileEditor projectId="p" character={original} onSaved={saved} />)
  fireEvent.change(screen.getByLabelText('身份'), { target: { value: '修书师父' } })
  fireEvent.blur(screen.getByLabelText('身份'))
  fireEvent.change(screen.getByLabelText('性格'), { target: { value: '寡言' } })
  fireEvent.blur(screen.getByLabelText('性格'))
  expect(post).not.toHaveBeenCalled()
  fireEvent.click(screen.getByText('保存人物'))
  await screen.findByText('已保存并核对。')
  expect(stored).toEqual({ ...original.profile_json, identity: '修书师父', personality: '寡言' })
  expect(saved).toHaveBeenCalledTimes(1)
  expect(JSON.parse(post.mock.calls[0][1].body).expected_profile_json).toEqual(original.profile_json)
})

it('guards duplicate saves and keeps typing performed while a save is pending', async () => {
  let resolve!: (value: unknown) => void
  post.mockImplementationOnce((_path, options) => {
    stored = JSON.parse(options.body).profile_json
    return new Promise(r => { resolve = r })
  })
  render(<CharacterProfileEditor projectId="p" character={original} onSaved={vi.fn()} />)
  fireEvent.change(screen.getByLabelText('身份'), { target: { value: '第一版身份' } })
  fireEvent.click(screen.getByText('保存人物'))
  fireEvent.click(screen.getByText('保存中…'))
  fireEvent.change(screen.getByLabelText('性格'), { target: { value: '正在编辑的新性格' } })
  await act(async () => { resolve({ status: 'accepted', entity: 'character', name: original.name }) })
  expect(post).toHaveBeenCalledTimes(1)
  expect(screen.getByLabelText('性格')).toHaveValue('正在编辑的新性格')
  expect(screen.getByRole('status')).toHaveTextContent('当前仍有未保存的修改')
  fireEvent.click(screen.getByText('保存人物'))
  await screen.findByText('已保存并核对。')
  expect(stored).toMatchObject({ identity: '第一版身份', personality: '正在编辑的新性格' })
  expect(JSON.parse(post.mock.calls[1][1].body).expected_profile_json).toMatchObject({ identity: '第一版身份', personality: '沉稳' })
})

it('keeps the draft and reports an unconfirmed projection instead of success', async () => {
  get.mockResolvedValue({ characters: [original] })
  const saved = vi.fn()
  render(<CharacterProfileEditor projectId="p" character={original} onSaved={saved} />)
  fireEvent.change(screen.getByLabelText('身份'), { target: { value: '新身份' } })
  fireEvent.click(screen.getByText('保存人物'))
  expect(await screen.findByRole('alert')).toHaveTextContent('尚未同步确认')
  expect(screen.getByLabelText('身份')).toHaveValue('新身份')
  expect(saved).not.toHaveBeenCalled()
})

it('restores unsaved fields after closing and reopening without making a request', () => {
  const view = render(<CharacterProfileEditor projectId="p" character={original} onSaved={vi.fn()} />)
  fireEvent.change(screen.getByLabelText('小传'), { target: { value: '保留的小传草稿' } })
  view.unmount()
  render(<CharacterProfileEditor projectId="p" character={original} onSaved={vi.fn()} />)
  expect(screen.getByLabelText('小传')).toHaveValue('保留的小传草稿')
  expect(api).not.toHaveBeenCalled()
})

it('does not apply an old save or draft to another project', async () => {
  let resolve!: (value: unknown) => void
  post.mockImplementationOnce(() => new Promise(r => { resolve = r }))
  const saved = vi.fn()
  const view = render(<CharacterProfileEditor projectId="old" character={original} onSaved={saved} />)
  fireEvent.change(screen.getByLabelText('身份'), { target: { value: '旧项目的草稿' } })
  fireEvent.click(screen.getByText('保存人物'))
  view.rerender(<CharacterProfileEditor projectId="new" character={original} onSaved={saved} />)
  expect(post.mock.calls[0][1].signal.aborted).toBe(true)
  await act(async () => { resolve({ status: 'accepted', entity: 'character', name: original.name }) })
  expect(screen.getByLabelText('身份')).toHaveValue('师父')
  expect(saved).not.toHaveBeenCalled()
})

it.each(['[]', 'null', '"text"', '{'])('rejects an invalid profile object: %s', async value => {
  render(<CharacterProfileEditor projectId="p" character={original} json onSaved={vi.fn()} />)
  fireEvent.change(screen.getByLabelText('人物档案 JSON'), { target: { value } })
  fireEvent.click(screen.getByText('保存人物'))
  await screen.findByRole('alert')
  expect(post).not.toHaveBeenCalled()
})

it('prevents a recovered old draft from overwriting a changed profile', () => {
  const view = render(<CharacterProfileEditor projectId="p" character={original} onSaved={vi.fn()} />)
  fireEvent.change(screen.getByLabelText('身份'), { target: { value: '旧草稿' } })
  view.unmount()
  render(<CharacterProfileEditor projectId="p" character={{ ...original, profile_json: { identity: '服务器新稿' } }} onSaved={vi.fn()} />)
  expect(screen.getByRole('alert')).toHaveTextContent('版本不同')
  expect(screen.getByText('保存人物')).toBeDisabled()
  expect((screen.getByLabelText('人物档案 JSON') as HTMLTextAreaElement).value).toContain('旧草稿')
})

it('ignores a previous project list response after switching the settings panel', async () => {
  let resolve!: (value: unknown) => void
  get.mockImplementation(path => {
    if (path.includes('/old/characters')) return new Promise(r => { resolve = r })
    return Promise.resolve(path.endsWith('/characters') ? { characters: [{ ...original, name: '新作品人物' }] } : { world_rules: [] })
  })
  const view = render(<SettingsPanel projectId="old" />)
  view.rerender(<SettingsPanel projectId="new" />)
  await screen.findByText('新作品人物')
  await act(async () => { resolve({ characters: [original] }) })
  expect(screen.queryByText('周砚')).not.toBeInTheDocument()
  expect(screen.getByText('新作品人物')).toBeInTheDocument()
})

it('reports a list read error instead of treating it as an empty project', async () => {
  get.mockRejectedValueOnce(new Error('offline')).mockResolvedValue({ world_rules: [] })
  render(<SettingsPanel projectId="p" />)
  await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('人物列表读取失败'))
  expect(screen.queryByText('还没有人物。')).not.toBeInTheDocument()
})

it('does not refetch on selection or let an old save select the previous character', async () => {
  const other = { id: 'second', name: '林禾', profile_json: { identity: '学徒' } }
  get.mockResolvedValue({ characters: [original, other] })
  let resolve!: (value: unknown) => void
  post.mockImplementationOnce(() => new Promise(r => { resolve = r }))
  render(<CharactersPage />)
  await screen.findByRole('heading', { name: '周砚' })
  fireEvent.change(screen.getByLabelText('人物档案 JSON'), { target: { value: '{"identity":"修改中的师父"}' } })
  fireEvent.click(screen.getByText('保存人物'))
  fireEvent.click(screen.getByRole('button', { name: '林禾' }))
  await act(async () => { resolve({ status: 'accepted', entity: 'character', name: original.name }) })
  expect(screen.getByRole('heading', { name: '林禾' })).toBeInTheDocument()
  expect((screen.getByLabelText('人物档案 JSON') as HTMLTextAreaElement).value).toContain('学徒')
  expect(get).toHaveBeenCalledTimes(1)
})

it('clears old selection and draft immediately when the character page changes project', async () => {
  get.mockImplementation(path => Promise.resolve({ characters: path.includes('/next/')
    ? [{ id: 'new', name: '新书人物', profile_json: {} }] : [original] }))
  const view = render(<CharactersPage />)
  await screen.findByRole('heading', { name: '周砚' })
  fireEvent.change(screen.getByLabelText('人物档案 JSON'), { target: { value: '{"identity":"旧作品草稿"}' } })
  route.project = 'next'
  view.rerender(<CharactersPage />)
  await screen.findByRole('heading', { name: '新书人物' })
  expect(screen.getByLabelText('人物档案 JSON')).toHaveValue('{}')
  expect(post).not.toHaveBeenCalled()
})

it('keeps the sidebar editor open and new typing when projection changes the row id', async () => {
  let resolve!: (value: unknown) => void
  get.mockImplementation(path => Promise.resolve(path.endsWith('/characters')
    ? { characters: [{ ...original, id: stored === original.profile_json ? 'c' : 'recreated', profile_json: stored }] }
    : { world_rules: [] }))
  post.mockImplementationOnce((_path, options) => {
    stored = JSON.parse(options.body).profile_json
    return new Promise(r => { resolve = r })
  })
  render(<SettingsPanel projectId="p" />)
  fireEvent.click(await screen.findByText('编辑'))
  fireEvent.change(screen.getByLabelText('身份'), { target: { value: '已提交的身份' } })
  fireEvent.click(screen.getByText('保存人物'))
  fireEvent.change(screen.getByLabelText('性格'), { target: { value: '继续写的性格' } })
  await act(async () => { resolve({ status: 'accepted', entity: 'character', name: original.name }) })
  expect(screen.getByLabelText('性格')).toHaveValue('继续写的性格')
  expect(screen.getByRole('status')).toHaveTextContent('当前仍有未保存的修改')
})

it('keeps a conflicted draft and does not advance its baseline on failure', async () => {
  post.mockRejectedValueOnce(new Error('人物档案已被修改，未覆盖新版本。'))
  render(<CharacterProfileEditor projectId="p" character={original} onSaved={vi.fn()} />)
  fireEvent.change(screen.getByLabelText('身份'), { target: { value: '保留的草稿' } })
  fireEvent.click(screen.getByText('保存人物'))
  await screen.findByRole('alert')
  expect(screen.getByLabelText('身份')).toHaveValue('保留的草稿')
  fireEvent.click(screen.getByText('保存人物'))
  await screen.findByText('已保存并核对。')
  expect(JSON.parse(post.mock.calls[1][1].body).expected_profile_json).toEqual(original.profile_json)
})

it('uses create-only semantics and retains inputs when a duplicate name conflicts', async () => {
  get.mockImplementation(path => Promise.resolve(path.endsWith('/characters') ? { characters: [] } : { world_rules: [] }))
  post.mockRejectedValue(new Error('人物档案已被修改，未覆盖新版本。'))
  render(<SettingsPanel projectId="p" />)
  fireEvent.click(await screen.findByText('+ 添加人物'))
  fireEvent.change(screen.getByPlaceholderText('姓名'), { target: { value: '周砚' } })
  fireEvent.change(screen.getByPlaceholderText('身份 / 角色'), { target: { value: '作者输入' } })
  fireEvent.click(screen.getByText('添加'))
  await screen.findByRole('alert')
  expect(screen.getByPlaceholderText('身份 / 角色')).toHaveValue('作者输入')
  expect(JSON.parse(post.mock.calls[0][1].body).expected_profile_json).toBeNull()
})

it('polls durable recovery without another write and preserves typing during recovery', async () => {
  vi.useFakeTimers()
  post.mockRejectedValue(new ApiError('后台将自动重试', 503, 'character_projection_pending'))
  const target = { ...original.profile_json, identity: '待同步身份' }
  get.mockResolvedValueOnce({ profile_json: target, pending: true })
    .mockResolvedValueOnce({ profile_json: target, pending: false })
    .mockResolvedValue({ characters: [{ ...original, profile_json: target }] })
  const saved = vi.fn()
  render(<CharacterProfileEditor projectId="p" character={original} onSaved={saved} />)
  fireEvent.change(screen.getByLabelText('身份'), { target: { value: '待同步身份' } })
  await act(async () => { fireEvent.click(screen.getByText('保存人物')) })
  expect(screen.getByText('等待同步确认…')).toBeDisabled()
  fireEvent.change(screen.getByLabelText('性格'), { target: { value: '恢复期间继续写' } })
  await act(async () => { await vi.advanceTimersByTimeAsync(5000) })
  expect(screen.getByRole('status')).toHaveTextContent('后台同步已完成，当前仍有未保存')
  expect(screen.getByLabelText('性格')).toHaveValue('恢复期间继续写')
  expect(post).toHaveBeenCalledTimes(1)
  expect(saved).toHaveBeenCalledTimes(1)
})

it('restores a pending verification after reopening and does not regenerate or save', async () => {
  const target = { identity: '已在后台保存' }
  sessionStorage.setItem(`aiwrite:character-draft:${JSON.stringify(['p', original.name])}`, JSON.stringify({
    base: original.profile_json, text: JSON.stringify(target), submitted: JSON.stringify(target),
  }))
  get.mockResolvedValueOnce({ profile_json: target, pending: false })
    .mockResolvedValue({ characters: [{ ...original, profile_json: target }] })
  render(<CharacterProfileEditor projectId="p" character={{ ...original, profile_json: target }} onSaved={vi.fn()} />)
  await screen.findByText('后台同步已完成并核对。')
  expect(post).not.toHaveBeenCalled()
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
})

it('stops recovery on a superseding author edit and can load the actual current profile', async () => {
  post.mockRejectedValue(new ApiError('自动重试', 503, 'character_projection_pending'))
  get.mockResolvedValue({ profile_json: { identity: '另一个页面的新稿' }, pending: true })
  render(<CharacterProfileEditor projectId="p" character={original} onSaved={vi.fn()} />)
  fireEvent.change(screen.getByLabelText('身份'), { target: { value: '本地旧草稿' } })
  fireEvent.click(screen.getByText('保存人物'))
  await screen.findByText('载入当前档案')
  expect(screen.getByText('保存人物')).toBeDisabled()
  expect((screen.getByLabelText('人物档案 JSON') as HTMLTextAreaElement).value).toContain('本地旧草稿')
  fireEvent.click(screen.getByText('载入当前档案'))
  expect(screen.getByLabelText('身份')).toHaveValue('另一个页面的新稿')
})

it('does not start background confirmation for a version conflict', async () => {
  post.mockRejectedValue(new ApiError('版本冲突', 409))
  render(<CharacterProfileEditor projectId="p" character={original} onSaved={vi.fn()} />)
  fireEvent.click(screen.getByText('保存人物'))
  await screen.findByRole('alert')
  expect(get).not.toHaveBeenCalled()
  expect(screen.queryByText('等待同步确认…')).not.toBeInTheDocument()
})

it('ignores a late recovery status response after project change', async () => {
  let resolve!: (value: unknown) => void
  post.mockRejectedValue(new ApiError('自动重试', 503, 'character_projection_pending'))
  get.mockImplementationOnce(() => new Promise(r => { resolve = r }))
  const saved = vi.fn()
  const view = render(<CharacterProfileEditor projectId="old" character={original} onSaved={saved} />)
  await act(async () => { fireEvent.click(screen.getByText('保存人物')) })
  view.rerender(<CharacterProfileEditor projectId="new" character={original} onSaved={saved} />)
  await act(async () => { resolve({ profile_json: original.profile_json, pending: false }) })
  expect(saved).not.toHaveBeenCalled()
  expect(get).toHaveBeenCalledTimes(1)
  expect(screen.queryByText('等待同步确认…')).not.toBeInTheDocument()
})

it('stops recovery reads on a missing character rather than polling forever', async () => {
  vi.useFakeTimers()
  post.mockRejectedValue(new ApiError('自动重试', 503, 'character_projection_pending'))
  get.mockRejectedValue(new ApiError('人物已删除', 404))
  render(<CharacterProfileEditor projectId="p" character={original} onSaved={vi.fn()} />)
  await act(async () => { fireEvent.click(screen.getByText('保存人物')) })
  await act(async () => { await vi.advanceTimersByTimeAsync(20000) })
  expect(screen.getByRole('alert')).toHaveTextContent('人物已删除')
  expect(screen.queryByText('已提交，后台正在恢复同步。当前草稿会保留。')).not.toBeInTheDocument()
  expect(get).toHaveBeenCalledTimes(1)
  expect(post).toHaveBeenCalledTimes(1)
})

it('keeps the character visible until deletion is confirmed and sends its baseline and request token', async () => {
  vi.spyOn(window, 'confirm').mockReturnValue(true)
  let deleted = false
  let resolve!: (value: unknown) => void
  get.mockImplementation(path => Promise.resolve(path.endsWith('/characters')
    ? { characters: deleted ? [] : [original] } : { world_rules: [] }))
  remove.mockImplementationOnce(() => new Promise(r => { resolve = r }))
  render(<SettingsPanel projectId="p" />)
  fireEvent.click(await screen.findByText('common.delete'))
  expect(screen.getByText('周砚')).toBeInTheDocument()
  const body = JSON.parse(remove.mock.calls[0][1].body)
  expect(body.expected_profile_json).toEqual(original.profile_json)
  expect(body.deletion_token).toMatch(/^[0-9a-f-]{36}$/)
  await act(async () => { deleted = true; resolve({ status: 'accepted' }) })
  expect(screen.queryByText('周砚')).not.toBeInTheDocument()
  expect(screen.getByRole('status')).toHaveTextContent('删除已完成并核对')
})

it('retains the list after a deletion conflict', async () => {
  vi.spyOn(window, 'confirm').mockReturnValue(true)
  get.mockImplementation(path => Promise.resolve(path.endsWith('/characters') ? { characters: [original] } : { world_rules: [] }))
  remove.mockRejectedValue(new ApiError('人物档案已变化', 409))
  render(<SettingsPanel projectId="p" />)
  fireEvent.click(await screen.findByText('common.delete'))
  await screen.findByRole('alert')
  expect(screen.getByText('周砚')).toBeInTheDocument()
})

it('checks background deletion without deleting a recreated same-name character', async () => {
  vi.useFakeTimers()
  vi.spyOn(window, 'confirm').mockReturnValue(true)
  let recreated = false
  get.mockImplementation(path => Promise.resolve(path.endsWith('/characters')
    ? { characters: [{ ...original, id: recreated ? 'new-id' : original.id }] } : { world_rules: [] }))
  remove.mockRejectedValue(new ApiError('后台清理中', 503, 'character_deletion_pending'))
  render(<SettingsPanel projectId="p" />)
  await act(async () => {})
  await act(async () => { fireEvent.click(screen.getByText('common.delete')) })
  recreated = true
  await act(async () => { await vi.advanceTimersByTimeAsync(5000) })
  expect(screen.getByText('周砚')).toBeInTheDocument()
  expect(screen.getByRole('status')).toHaveTextContent('删除已完成并核对')
  expect(remove).toHaveBeenCalledTimes(1)
})
