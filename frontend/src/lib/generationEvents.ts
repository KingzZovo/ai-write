// PR-GEN-UX (2026-07-26): pure routing for chapter-generation SSE events.
// DesktopWorkspace feeds every non-text event through routeGenerationEvent to
// drive the progress strip and the truncated/refusal/save_failed banners.
// Payload shapes mirror backend app/api/generate.py.

export interface GenerationEventUpdate {
  /** New label for the progress strip (current phase). */
  progress?: string
  /** Amber warning banner: chapter persisted, but degraded / as draft. */
  warning?: string
  /** Red error banner: content was NOT persisted. */
  error?: string
  /** Chapter row was saved with status 'draft' (truncated / refusal). */
  savedAsDraft?: boolean
}

const num = (v: unknown): number | null =>
  typeof v === 'number' && Number.isFinite(v) ? v : null

export function routeGenerationEvent(
  evt: Record<string, unknown>,
): GenerationEventUpdate | null {
  const name = String(evt.event ?? evt.status ?? '')
  switch (name) {
    case 'scene': {
      // New backend event: {"event":"scene","scene_idx":N,"total":M,"title":"..."}
      // The current emitter always sends total:null (SceneBrief does not carry
      // the plan size), so idx must render standalone too.
      const idx = num(evt.scene_idx)
      const total = num(evt.total)
      const title =
        typeof evt.title === 'string' && evt.title.trim() ? `：${evt.title}` : ''
      if (idx === null || idx <= 0) return { progress: `写作中${title}` }
      return {
        progress: `写作中 · 场景 ${idx}${total !== null ? `/${total}` : ''}${title}`,
      }
    }
    case 'fallback_restart':
      return { progress: '场景流中断，回退整章重写...' }
    case 'quality_check':
      return { progress: evt.passed ? '质量检查通过' : '质量检查未通过，准备改写' }
    case 'quality_rewrite_start':
      return { progress: '质量改写中...' }
    case 'quality_rewrite_done':
      return { progress: '质量改写完成' }
    case 'logic_critic_done':
      return { progress: '逻辑审查完成' }
    case 'evaluating': {
      const round = num(evt.round)
      return { progress: round !== null ? `评分中（第 ${round} 轮）` : '评分中...' }
    }
    case 'scored': {
      const overall = num(evt.overall)
      const round = num(evt.round)
      if (overall === null) return { progress: '评分完成' }
      return {
        progress: `评分 ${overall.toFixed(1)}${round !== null ? `（第 ${round} 轮）` : ''}`,
      }
    }
    case 'revise_skipped': {
      const overall = num(evt.overall)
      return {
        progress:
          evt.reason === 'evaluation_parse_failed'
            ? '评分解析失败，跳过修订'
            : `评分达标${overall !== null ? `（${overall.toFixed(1)}）` : ''}，无需修订`,
      }
    }
    case 'targeted_revision': {
      const spans = num(evt.spans_revised)
      return { progress: `定点修订${spans !== null ? ` ${spans} 处` : ''}` }
    }
    case 'revising': {
      const round = num(evt.round)
      const mode = evt.mode === 'targeted' ? '定点' : '整章'
      return { progress: `修订第 ${round ?? '?'} 轮（${mode}）` }
    }
    case 'cascade_triggered':
      return { progress: '已触发级联修订任务' }
    case 'truncated':
      return {
        savedAsDraft: true,
        warning:
          '本章结尾疑似在句中被截断（生成中断或达到长度上限），已按草稿保存。请检查结尾，必要时重新生成。',
      }
    case 'refusal':
      return {
        savedAsDraft: true,
        warning:
          '模型疑似拒绝生成或输出了无关内容，本章已按草稿保存。请检查正文内容。',
      }
    case 'save_failed': {
      const reason =
        typeof evt.reason === 'string'
          ? evt.reason
          : typeof evt.error === 'string'
            ? evt.error
            : '未知原因'
      return {
        error: `章节保存失败（${reason}）。正文仍保留在编辑区，请手动复制备份后重试。`,
      }
    }
    case 'saved': {
      // Revise-loop saves carry truncated:true when the rewrite itself ended
      // mid-sentence; the chapter row is then persisted as draft.
      if (evt.truncated === true) {
        return {
          savedAsDraft: true,
          progress: '已保存（草稿）',
          warning: '修订结果疑似被截断，本章已按草稿保存。请检查结尾。',
        }
      }
      return { progress: '已保存' }
    }
    default:
      return null
  }
}
