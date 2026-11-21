import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { RelationshipEditor } from '@/components/panels/RelationshipEditor'
import { apiFetch, ApiError } from '@/lib/api'
import { readRelationshipDraft, relationshipDraftKey, storeRelationshipDraft, type RelationshipSnapshot } from '@/lib/relationshipWrites'

vi.mock('@/lib/api', async original => ({ ...await original<typeof import('@/lib/api')>(), apiFetch: vi.fn() }))
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.mocked(apiFetch).mockReset(); sessionStorage.clear() })
let count = 0
function example() {
  const props = { projectId: `relationship-${++count}`, id: 'relation', from: '陈川', to: '沈青',
    relType: '同伴', mode: 'edit' as const, onDone: vi.fn(), onCancel: vi.fn() }
  const snapshot: RelationshipSnapshot = { version: 'a'.repeat(64), mixed_fields: [],
    fields: { label: '旧标签', note: '完整旧备注', sentiment: 'neutral' },
    entries: [{ edge_id: 'edge', properties: { type: '同伴', raw_type: '暂时同伴（尚未互相信任）',
      chapter_start: 900, source_chapter_id: 'chapter', label: '旧标签', note: '完整旧备注' } }] }
  return { props, snapshot, key: relationshipDraftKey(props.projectId, props.id) }
}
function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (error: Error) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
function saved(options?: RequestInit) {
  return { status: 'saved', request_id: (options?.headers as Record<string, string>)['Idempotency-Key'] }
}
async function editable(label: string) {
  const field = await screen.findByLabelText(label)
  await waitFor(() => expect(field).not.toBeDisabled())
  return field
}

describe('relationship author editor', () => {
  it('shows the full source wording and sends only the field the author changed', async () => {
    const { props, snapshot } = example()
    let sent: RequestInit | undefined
    vi.mocked(apiFetch).mockImplementation(async (_path, options) => {
      if (options?.method) { sent = options; return saved(options) }
      return snapshot
    })
    render(<RelationshipEditor {...props} />)
    fireEvent.change(await editable('关系备注'), { target: { value: '合作仅限本案。' } })
    expect(screen.getByText(/尚未互相信任/).textContent).toContain('正文来源待核对')
    fireEvent.click(screen.getByText('保存关系'))
    await waitFor(() => expect(props.onDone).toHaveBeenCalledOnce())
    expect(JSON.parse(sent!.body as string)).toEqual({ source: '陈川', target: '沈青', rel_type: '同伴', note: '合作仅限本案。' })
    expect((sent!.headers as Record<string, string>)['If-Match']).toBe(snapshot.version)
  })

  it('does not flatten untouched metadata from a mixed source group', async () => {
    const { props, snapshot } = example()
    snapshot.mixed_fields = ['note']
    snapshot.fields.note = ''
    let body: Record<string, string> | undefined
    vi.mocked(apiFetch).mockImplementation(async (_path, options) => {
      if (options?.method) { body = JSON.parse(options.body as string); return saved(options) }
      return snapshot
    })
    render(<RelationshipEditor {...props} />)
    fireEvent.change(await editable('关系标签'), { target: { value: '新标签' } })
    expect(screen.getByText(/原记录内容不一致/)).toBeTruthy()
    fireEvent.click(screen.getByText('保存关系'))
    await waitFor(() => expect(props.onDone).toHaveBeenCalledOnce())
    expect(body).toMatchObject({ label: '新标签' })
    expect(body).not.toHaveProperty('note')
  })

  it('retains an unknown write across reopening and mode changes, then retries its exact request', async () => {
    const { props, snapshot, key } = example()
    const calls: RequestInit[] = []
    vi.mocked(apiFetch).mockImplementation(async (_path, options) => {
      if (options?.method) {
        calls.push(options)
        if (calls.length === 1) throw new ApiError('显示资料尚待同步', 503, 'sync_pending')
        return saved(options)
      }
      return calls.length ? { ...snapshot, version: 'b'.repeat(64) } : snapshot
    })
    const first = render(<RelationshipEditor {...props} />)
    fireEvent.change(await editable('关系备注'), { target: { value: '作者保留的稿' } })
    fireEvent.click(screen.getByText('保存关系'))
    await screen.findByRole('alert')
    const original = readRelationshipDraft(key)!.attempt
    expect(screen.getByLabelText('关系备注')).toBeDisabled()
    first.unmount()
    render(<RelationshipEditor {...props} mode="delete" />)
    await screen.findByText('原修改请求尚待确认，输入已保留。请先重试确认原请求。')
    fireEvent.click(screen.getByText('重试确认'))
    await waitFor(() => expect(props.onDone).toHaveBeenCalledOnce())
    expect(calls).toHaveLength(2)
    expect(calls[1]).toEqual(calls[0])
    expect(calls[1].method).toBe('PUT')
    expect((calls[1].headers as Record<string, string>)['Idempotency-Key']).toBe(original!.id)
    expect(readRelationshipDraft(key)).toBeUndefined()
  })

  it('keeps a pending request usable when the new source read fails', async () => {
    const { props, snapshot } = example()
    let reads = 0, writes = 0
    vi.mocked(apiFetch).mockImplementation(async (_path, options) => {
      if (options?.method) {
        if (++writes === 1) throw new Error('响应丢失')
        return saved(options)
      }
      if (++reads > 1) throw new Error('来源暂不可读')
      return snapshot
    })
    const first = render(<RelationshipEditor {...props} />)
    fireEvent.change(await editable('关系标签'), { target: { value: '草稿' } })
    fireEvent.click(screen.getByText('保存关系'))
    await screen.findByRole('alert')
    first.unmount()
    render(<RelationshipEditor {...props} />)
    expect(await screen.findByRole('alert')).toHaveTextContent('来源暂不可读')
    expect(screen.getByText('重试确认')).not.toBeDisabled()
    fireEvent.click(screen.getByText('重试确认'))
    await waitFor(() => expect(props.onDone).toHaveBeenCalledOnce())
  })

  it('requires checking a new source version after a definite conflict while preserving text', async () => {
    const { props, snapshot, key } = example()
    const calls: RequestInit[] = []
    let reads = 0
    vi.mocked(apiFetch).mockImplementation(async (_path, options) => {
      if (options?.method) {
        calls.push(options)
        if (calls.length === 1) throw new ApiError('来源已修改', 409, 'source_changed')
        return saved(options)
      }
      return ++reads === 1 ? snapshot : { ...snapshot, version: 'b'.repeat(64) }
    })
    render(<RelationshipEditor {...props} />)
    fireEvent.change(await editable('关系备注'), { target: { value: '我的修改' } })
    fireEvent.click(screen.getByText('保存关系'))
    await screen.findByRole('alert')
    expect(readRelationshipDraft(key)!.attempt).toBeUndefined()
    fireEvent.click(screen.getByText('刷新来源'))
    await screen.findByText('已核对当前记录，保留输入')
    expect(screen.getByText('保存关系')).toBeDisabled()
    expect(screen.getByLabelText('关系备注')).toHaveValue('我的修改')
    fireEvent.click(screen.getByText('已核对当前记录，保留输入'))
    fireEvent.click(screen.getByText('保存关系'))
    await waitFor(() => expect(props.onDone).toHaveBeenCalledOnce())
    expect((calls[1].headers as Record<string, string>)['If-Match']).toBe('b'.repeat(64))
    expect(calls[1].headers).not.toEqual(calls[0].headers)
  })

  it('shares the draft and frozen request between two panels showing the same relation', async () => {
    const { props, snapshot, key } = example()
    const secondDone = vi.fn(), calls: RequestInit[] = []
    vi.mocked(apiFetch).mockImplementation(async (_path, options) => {
      if (options?.method) {
        calls.push(options)
        if (calls.length === 1) throw new Error('响应丢失')
        return saved(options)
      }
      return snapshot
    })
    render(<><RelationshipEditor {...props} /><RelationshipEditor {...props} onDone={secondDone} /></>)
    await waitFor(() => expect(screen.getAllByLabelText('关系备注')).toHaveLength(2))
    await waitFor(() => expect(screen.getAllByLabelText('关系备注')[0]).not.toBeDisabled())
    fireEvent.change(screen.getAllByLabelText('关系备注')[0], { target: { value: '共同草稿' } })
    expect(screen.getAllByLabelText('关系备注')[1]).toHaveValue('共同草稿')
    fireEvent.click(screen.getAllByText('保存关系')[0])
    await screen.findByRole('alert')
    screen.getAllByLabelText('关系备注').forEach(field => expect(field).toBeDisabled())
    const requestId = readRelationshipDraft(key)!.attempt!.id
    fireEvent.click(screen.getAllByText('重试确认')[1])
    await waitFor(() => expect(secondDone).toHaveBeenCalledOnce())
    expect(props.onDone).toHaveBeenCalledOnce()
    expect((calls[1].headers as Record<string, string>)['Idempotency-Key']).toBe(requestId)
    expect(calls[1].body).toBe(calls[0].body)
  })

  it('ignores a slow source response after the shared request was confirmed', async () => {
    const { props, snapshot, key } = example()
    const slow = deferred<unknown>()
    let reads = 0
    vi.mocked(apiFetch).mockImplementation((_path, options) => {
      if (options?.method) return Promise.resolve(saved(options)) as ReturnType<typeof apiFetch>
      return (++reads === 1 ? Promise.resolve(snapshot) : slow.promise) as ReturnType<typeof apiFetch>
    })
    render(<><RelationshipEditor {...props} /><RelationshipEditor {...props} /></>)
    await waitFor(() => expect(screen.getAllByLabelText('关系备注')).toHaveLength(2))
    await waitFor(() => expect(screen.getAllByLabelText('关系备注')[0]).not.toBeDisabled())
    fireEvent.change(screen.getAllByLabelText('关系备注')[0], { target: { value: '已确认的修改' } })
    fireEvent.click(screen.getAllByText('保存关系')[0])
    await waitFor(() => expect(props.onDone).toHaveBeenCalledTimes(2))
    expect(readRelationshipDraft(key)).toBeUndefined()
    await act(async () => slow.resolve(snapshot))
    expect(readRelationshipDraft(key)).toBeUndefined()
    expect(screen.queryByLabelText('关系备注')).toBeNull()
  })

  it('keeps a newer draft when an older unmounted response eventually confirms', async () => {
    const { props, snapshot, key } = example()
    const request = deferred<unknown>()
    let sent: RequestInit | undefined
    vi.mocked(apiFetch).mockImplementation((_path, options) => {
      if (options?.method) { sent = options; return request.promise as ReturnType<typeof apiFetch> }
      return Promise.resolve(snapshot) as ReturnType<typeof apiFetch>
    })
    const first = render(<RelationshipEditor {...props} />)
    fireEvent.change(await editable('关系备注'), { target: { value: '先发出的稿' } })
    fireEvent.click(screen.getByText('保存关系'))
    await waitFor(() => expect(sent).toBeDefined())
    first.unmount()
    const previous = readRelationshipDraft(key)!
    storeRelationshipDraft(key, { ...previous, attempt: undefined, fields: { ...previous.fields, note: '稍后的草稿' } })
    render(<RelationshipEditor {...props} />)
    await screen.findByLabelText('关系备注')
    await act(async () => request.resolve(saved(sent)))
    expect(screen.getByLabelText('关系备注')).toHaveValue('稍后的草稿')
    expect(readRelationshipDraft(key)!.fields.note).toBe('稍后的草稿')
    expect(props.onDone).not.toHaveBeenCalled()
  })

  it('does not let a late source read from another project replace the current form', async () => {
    const first = example(), second = example(), pending = deferred<unknown>()
    second.snapshot.fields.note = '另一部作品的关系'
    vi.mocked(apiFetch).mockImplementation(path => path.includes(first.props.projectId)
      ? pending.promise as ReturnType<typeof apiFetch> : Promise.resolve(second.snapshot) as ReturnType<typeof apiFetch>)
    const view = render(<RelationshipEditor {...first.props} />)
    view.rerender(<RelationshipEditor {...second.props} />)
    expect(await screen.findByLabelText('关系备注')).toHaveValue('另一部作品的关系')
    await act(async () => pending.resolve(first.snapshot))
    expect(screen.getByLabelText('关系备注')).toHaveValue('另一部作品的关系')
    expect(readRelationshipDraft(first.key)).toBeUndefined()
  })

  it.each(['accepted', 'wrong_request_id'])('does not count %s as a save confirmation', async mismatch => {
    const { props, snapshot, key } = example()
    vi.mocked(apiFetch).mockImplementation(async (_path, options) => options?.method
      ? { status: mismatch === 'accepted' ? 'accepted' : 'saved', request_id: 'unconfirmed' } : snapshot)
    render(<RelationshipEditor {...props} />)
    fireEvent.change(await editable('关系标签'), { target: { value: '未确认修改' } })
    fireEvent.click(screen.getByText('保存关系'))
    expect(await screen.findByRole('alert')).toHaveTextContent('尚未确认保存结果')
    expect(props.onDone).not.toHaveBeenCalled()
    expect(readRelationshipDraft(key)!.attempt).toBeDefined()
  })

  it('confirms deletion with the source version and retains it when the response is lost', async () => {
    const { props, snapshot, key } = example()
    const calls: RequestInit[] = []
    vi.mocked(apiFetch).mockImplementation(async (_path, options) => {
      if (options?.method) { calls.push(options); throw new Error('响应丢失') }
      return snapshot
    })
    render(<RelationshipEditor {...props} mode="delete" />)
    await waitFor(() => expect(screen.getByText('确认删除')).not.toBeDisabled())
    fireEvent.click(screen.getByText('确认删除'))
    await waitFor(() => expect(calls).toHaveLength(1))
    expect(calls[0].method).toBe('DELETE')
    expect((calls[0].headers as Record<string, string>)['If-Match']).toBe(snapshot.version)
    expect(JSON.parse(calls[0].body as string)).toEqual({ source: '陈川', target: '沈青', rel_type: '同伴' })
    await screen.findByRole('alert')
    expect(readRelationshipDraft(key)!.attempt!.method).toBe('DELETE')
    expect(props.onDone).not.toHaveBeenCalled()
  })

  it('keeps a draft in memory when browser storage is unavailable', async () => {
    const { props, snapshot, key } = example()
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('storage full') })
    vi.mocked(apiFetch).mockResolvedValue(snapshot)
    render(<RelationshipEditor {...props} />)
    fireEvent.change(await editable('关系备注'), { target: { value: '仍需保留的输入' } })
    expect(readRelationshipDraft(key)!.fields.note).toBe('仍需保留的输入')
  })
})
