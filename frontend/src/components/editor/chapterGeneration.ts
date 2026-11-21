import { routeGenerationEvent } from '@/lib/generationEvents'
import { ChapterDraftSession } from './chapterDraftSession'

type GenerationUI = {
  active: () => boolean
  phase: (text: string) => void
  warning: (text: string) => void
  error: (text: string) => void
  settled: () => void
  blocked: () => void
}

/** A stream owns one draft session. Events are never persistence receipts. */
export function createChapterGeneration(session: ChapterDraftSession, ui: GenerationUI) {
  const token = session.beginGeneration()
  if (token === null) return null
  let buffer = ''
  let finished = false
  let interrupted = false
  let rejected = false
  let cancelled = false
  let savedEvent = false
  const active = () => !finished && ui.active()
  const replace = (text: string) => { buffer = text; session.replaceGenerated(token, text) }

  const settle = () => {
    if (finished) return
    finished = true
    // This is the only handoff: read the actual saved body, retaining any
    // different candidate for the author. Never issue a client-side PUT.
    void session.finishGeneration(token, rejected ? 'needs_review' : interrupted || cancelled ? 'draft' : null)
    ui.settled()
  }

  return {
    chunk(text: string) {
      if (active()) replace(buffer + text)
    },
    event(event: Record<string, unknown>) {
      if (!active()) return
      const name = String(event.event ?? event.status ?? '')
      const routed = routeGenerationEvent(event)
      const deliveryNotice = ['saved', 'partial_saved', 'truncated', 'refusal', 'chapter_acceptance'].includes(name)
      if (deliveryNotice) {
        ui.phase('已收到处理提示，待核对保存稿')
        if (name === 'partial_saved' || name === 'truncated' || event.truncated === true) {
          ui.warning('这份片段可能不完整，本章尚未完成。结束后将核对保存稿，请检查剧情衔接和结尾。')
        } else if (name === 'refusal') ui.warning('模型可能返回了拒绝或无关内容，请核对正文及保存结果。')
        else if (name === 'chapter_acceptance') ui.warning('已收到流程处理结果，正文仍需作者审读。')
      } else {
        if (routed?.progress) ui.phase(routed.progress)
        if (routed?.warning) ui.warning(routed.warning)
      }
      if (routed?.error) ui.error(routed.error)
      if (routed?.interrupted || (routed?.savedAsDraft && name !== 'chapter_acceptance')) interrupted = true
      if (name === 'chapter_acceptance' && event.status !== 'completed') rejected = true
      if (name === 'fallback_restart' || name === 'revise_restart') {
        savedEvent = false
        interrupted = false
        rejected = false
        replace('')
        return
      }
      if (name === 'generation_blocked' || event.reason === 'outline_chain_incomplete') {
        ui.error(String(event.message ?? '大纲链路未完成，不能生成正文。'))
        ui.blocked()
        return
      }
      if (name === 'saved' || name === 'partial_saved') {
        savedEvent = true
        ui.phase('正在核对保存结果…')
        return
      }
      const replacement = typeof event.content_text === 'string' ? event.content_text
        : typeof event.final_text === 'string' ? event.final_text : null
      if (name === 'quality_failed') {
        rejected = true
        if (replacement !== null) replace(replacement)
        ui.error('这份生成稿仍需复核。已保留稿件，结束后将核对实际保存结果。')
      } else if ((name === 'quality_rewrite_done' || name === 'quality_warning') && replacement !== null) {
        replace(replacement)
      }
    },
    error(error: Error) {
      if (!active()) return
      if (buffer && !savedEvent) interrupted = true
      ui.error(`正文生成失败：${error.message}`)
    },
    done() {
      if (!active()) return
      if (cancelled) ui.warning('已停止接收生成内容，片段已保留；请核对保存结果后继续。')
      else if (buffer && !savedEvent) ui.warning('已保留收到的稿件，尚未取得保存确认，正在读取保存稿核对。')
      settle()
    },
    cancel() { cancelled = true },
    detach() {
      // Called explicitly before switching scope or unmounting. Late stream
      // callbacks then become no-ops; the old chapter keeps its own draft.
      cancelled = true
      settle()
    },
  }
}

export type ChapterGeneration = NonNullable<ReturnType<typeof createChapterGeneration>>
