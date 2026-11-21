// PR-GEN-UX (2026-07-26): pure routing for chapter-generation SSE events.
// DesktopWorkspace feeds every non-text event through routeGenerationEvent to
// drive the progress strip and the truncated/refusal/save_failed banners.
// Payload shapes mirror backend app/api/generate.py.

export interface GenerationEventUpdate {
  /** New label for the progress strip (current phase). */
  progress?: string
  /** Amber warning banner; persistence is confirmed only by save events. */
  warning?: string
  /** Red error banner: content was NOT persisted. */
  error?: string
  /** Chapter row was saved with status 'draft' (truncated / refusal). */
  savedAsDraft?: boolean
  /** Generation is incomplete; this does not confirm persistence. */
  interrupted?: boolean
}

const num = (v: unknown): number | null =>
  typeof v === 'number' && Number.isFinite(v) ? v : null

const evaluationFailureLabel = (reason: unknown): string => {
  switch (reason) {
    case 'call_failed': return '评审模型调用失败'
    case 'timeout': return '评审超时'
    case 'empty_input': return '正文为空，无法评审'
    case 'parse_failed': return '评分解析失败'
    case 'incomplete_response': return '评审响应未完整结束'
    default: return '评审未完成'
  }
}

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
      return { progress: '尚未收到正文，改用整章生成...' }
    case 'generation_interrupted':
      return {
        interrupted: true,
        progress: '生成中断，等待草稿保存确认',
        warning: '生成已中断，当前片段不完整，请复核；保存结果以随后提示为准。',
      }
    case 'partial_saved':
      return {
        savedAsDraft: true,
        progress: '片段已按草稿保存',
        warning: '片段已按草稿保存，本章尚未完成。请核对剧情衔接和结尾后继续写作。',
      }
    case 'quality_check':
      return { progress: evt.passed ? '输出检查通过，内容质量待评审' : '输出检查发现问题，正在处理' }
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
      if (evt.evaluation_failure) return {
        progress: `${evaluationFailureLabel(evt.evaluation_failure)}，未获得有效评分`,
      }
      const overall = num(evt.overall)
      const round = num(evt.round)
      if (overall === null) return { progress: '评分完成' }
      return {
        progress: `评分 ${overall.toFixed(1)}${round !== null ? `（第 ${round} 轮）` : ''}`,
      }
    }
    case 'revise_skipped': {
      if (evt.reason === 'targeted_revision_unapplied') {
        return {
          progress: '局部修订未完成，已保留当前候选',
          warning: '未能完成可靠的局部修订，请审阅正文和评审意见。',
        }
      }
      if (typeof evt.reason === 'string' && evt.reason.startsWith('evaluation_')) {
        return { progress: `${evaluationFailureLabel(evt.reason.slice('evaluation_'.length))}，跳过修订` }
      }
      const overall = num(evt.overall)
      return {
        progress:
          evt.reason === 'score_above_threshold'
            ? `评分达标${overall !== null ? `（${overall.toFixed(1)}）` : ''}，无需修订`
            : '自动修订已停止，请核查评审结果',
      }
    }
    case 'chapter_acceptance':
      return evt.status === 'completed'
        ? { progress: '章节验收通过' }
        : {
            progress: '已保存，待审阅',
            savedAsDraft: true,
            warning: '本章尚未通过验收，正文已保留。请核查评审结果和关键剧情。',
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
    case 'revise_error':
      return {
        progress: '修订中断，等待后续处理结果',
        warning: '本轮修订未完成，请核对当前正文和后续保存提示，不要将片段视为定稿。',
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
