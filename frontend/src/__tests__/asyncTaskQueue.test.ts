import { describe, it, expect } from 'vitest'
import {
  type AsyncTask,
  formatElapsed,
  hasActiveTask,
  isTerminal,
  mergeTasks,
  normalizeTask,
  parseTimestamp,
  statusLabel,
  taskElapsedSeconds,
  taskTypeLabel,
  upsertTask,
} from '@/components/workspace/asyncTaskQueue'

function task(overrides: Partial<AsyncTask> = {}): AsyncTask {
  return {
    taskId: 't1',
    taskType: 'chapter',
    status: 'running',
    charCount: null,
    progressText: '',
    errorMessage: '',
    chapterId: null,
    createdAt: null,
    updatedAt: null,
    ...overrides,
  }
}

describe('isTerminal / hasActiveTask', () => {
  it('treats completed/failed/cancelled/needs_review as terminal', () => {
    for (const s of ['completed', 'failed', 'cancelled', 'needs_review']) {
      expect(isTerminal(s)).toBe(true)
    }
    for (const s of ['pending', 'running', 'polishing']) {
      expect(isTerminal(s)).toBe(false)
    }
  })

  it('hasActiveTask is true only while some task is non-terminal', () => {
    expect(hasActiveTask([])).toBe(false)
    expect(hasActiveTask([task({ status: 'completed' })])).toBe(false)
    expect(
      hasActiveTask([task({ status: 'completed' }), task({ taskId: 't2', status: 'pending' })]),
    ).toBe(true)
  })
})

describe('normalizeTask', () => {
  it('parses the current (minimal) list-row shape', () => {
    expect(
      normalizeTask({
        task_id: 'abc',
        task_type: 'chapter',
        status: 'running',
        char_count: 1200,
        created_at: '2026-07-26 03:00:00+00:00',
      }),
    ).toEqual(
      task({
        taskId: 'abc',
        charCount: 1200,
        createdAt: '2026-07-26 03:00:00+00:00',
      }),
    )
  })

  it('parses the extended shape with progress/error/chapter_id/updated_at', () => {
    const t = normalizeTask({
      task_id: 'abc',
      task_type: 'chapter',
      status: 'failed',
      char_count: 0,
      progress_text: '正在写第一幕...',
      error_message: 'LLM timeout',
      chapter_id: 'ch9',
      created_at: '2026-07-26 03:00:00+00:00',
      updated_at: '2026-07-26 03:05:00+00:00',
    })
    expect(t?.progressText).toBe('正在写第一幕...')
    expect(t?.errorMessage).toBe('LLM timeout')
    expect(t?.chapterId).toBe('ch9')
    expect(t?.updatedAt).toBe('2026-07-26 03:05:00+00:00')
  })

  it('rejects rows without a task_id and non-objects', () => {
    expect(normalizeTask(null)).toBeNull()
    expect(normalizeTask('x')).toBeNull()
    expect(normalizeTask({ status: 'running' })).toBeNull()
  })

  it('defaults malformed fields instead of throwing', () => {
    const t = normalizeTask({ task_id: 'abc', char_count: 'nope', progress_text: 42 })
    expect(t).toEqual(task({ taskId: 'abc', taskType: '', status: 'pending' }))
  })
})

describe('mergeTasks / upsertTask', () => {
  it('preserves previously-known fields the minimal list shape omits', () => {
    const prev = [task({ chapterId: 'ch1', progressText: '写作中...', errorMessage: 'old' })]
    const incoming = [task({ status: 'completed', charCount: 3000 })]
    const merged = mergeTasks(prev, incoming)
    expect(merged).toHaveLength(1)
    expect(merged[0]).toMatchObject({
      status: 'completed',
      charCount: 3000,
      chapterId: 'ch1',
      progressText: '写作中...',
    })
  })

  it('keeps locally-known tasks missing from the (limited) server list', () => {
    const local = task({ taskId: 'just-submitted', status: 'pending', chapterId: 'ch1' })
    const merged = mergeTasks([local], [task({ taskId: 'server-1' })])
    expect(merged.map((t) => t.taskId)).toEqual(['just-submitted', 'server-1'])
  })

  it('upsertTask replaces by id and prepends unknown tasks', () => {
    const list = [task({ taskId: 'a' }), task({ taskId: 'b' })]
    const updated = upsertTask(list, task({ taskId: 'b', status: 'completed' }))
    expect(updated[1].status).toBe('completed')
    const grown = upsertTask(list, task({ taskId: 'c' }))
    expect(grown.map((t) => t.taskId)).toEqual(['c', 'a', 'b'])
  })
})

describe('timestamps / elapsed', () => {
  const created = '2026-07-26 03:00:00+00:00'
  const createdMs = Date.parse('2026-07-26T03:00:00+00:00')

  it('parseTimestamp handles the backend space-separated format', () => {
    expect(parseTimestamp(created)).toBe(createdMs)
    expect(parseTimestamp('garbage')).toBeNull()
    expect(parseTimestamp(null)).toBeNull()
  })

  it('active task: elapsed runs against now', () => {
    const t = task({ createdAt: created, status: 'running' })
    expect(taskElapsedSeconds(t, createdMs + 65_000)).toBe(65)
  })

  it('terminal task: elapsed uses updated_at, hidden when missing', () => {
    const done = task({
      status: 'completed',
      createdAt: created,
      updatedAt: '2026-07-26 03:02:30+00:00',
    })
    expect(taskElapsedSeconds(done, createdMs + 999_999_000)).toBe(150)
    expect(taskElapsedSeconds(task({ status: 'completed', createdAt: created }), createdMs)).toBeNull()
  })

  it('hides implausible values (negative / > 48h / missing created_at)', () => {
    const t = task({ createdAt: created })
    expect(taskElapsedSeconds(t, createdMs - 1000)).toBeNull()
    expect(taskElapsedSeconds(t, createdMs + 49 * 3600 * 1000)).toBeNull()
    expect(taskElapsedSeconds(task(), createdMs)).toBeNull()
  })

  it('formatElapsed renders m:ss and h:mm:ss', () => {
    expect(formatElapsed(5)).toBe('0:05')
    expect(formatElapsed(65)).toBe('1:05')
    expect(formatElapsed(3661)).toBe('1:01:01')
  })
})

describe('labels', () => {
  it('maps known statuses/types and falls back to the raw value', () => {
    expect(statusLabel('running')).toBe('生成中')
    expect(statusLabel('weird')).toBe('weird')
    expect(taskTypeLabel('chapter')).toBe('章节正文')
    expect(taskTypeLabel('outline_book')).toBe('全书大纲')
    expect(taskTypeLabel('')).toBe('生成任务')
  })
})
