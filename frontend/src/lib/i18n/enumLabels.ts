/** Chinese labels for backend enum values.
 *
 * These are raw enum codes returned by the API (status/severity/tier/actor/
 * action/task_type). They are NOT i18n message keys — the app defaults to
 * Chinese and these maps give each backend code a natural Chinese label.
 * Unknown codes fall back to the raw value so new backend enums never render
 * blank.
 */

function label(map: Record<string, string>, value: string | null | undefined): string {
  if (!value) return '—'
  return map[value] ?? value
}

// cascade_tasks.status + call-logs task status share the queue vocabulary.
const CASCADE_STATUS: Record<string, string> = {
  pending: '待处理',
  running: '运行中',
  done: '已完成',
  failed: '失败',
  skipped: '已跳过',
}
export const cascadeStatusLabel = (v: string | null | undefined) => label(CASCADE_STATUS, v)

const SEVERITY: Record<string, string> = {
  high: '高',
  critical: '严重',
}
export const severityLabel = (v: string | null | undefined) => label(SEVERITY, v)

const ENTITY_TYPE: Record<string, string> = {
  chapter: '章节',
  outline: '大纲',
  character: '角色',
  world_rule: '世界规则',
  relationship: '关系',
}
export const entityTypeLabel = (v: string | null | undefined) => label(ENTITY_TYPE, v)

// call-logs.status
const LOG_STATUS: Record<string, string> = {
  ok: '正常',
  error: '错误',
}
export const logStatusLabel = (v: string | null | undefined) => label(LOG_STATUS, v)

// task_type — shared by logs page + llm-routing matrix group headers.
const TASK_TYPE: Record<string, string> = {
  generation: '正文生成',
  polishing: '润色',
  outline_book: '全书大纲',
  outline_volume: '分卷大纲',
  outline_chapter: '章节大纲',
  evaluation: '评估',
  extraction: '抽取',
  summary: '摘要',
  rewrite: '改写',
}
export const taskTypeLabel = (v: string | null | undefined) => label(TASK_TYPE, v)

// changelog.actor_type
const ACTOR_TYPE: Record<string, string> = {
  user: '用户',
  agent: '智能体',
  critic: '评审',
  system: '系统',
}
export const actorTypeLabel = (v: string | null | undefined) => label(ACTOR_TYPE, v)

// changelog.action
const ACTION: Record<string, string> = {
  create: '创建',
  update: '更新',
  delete: '删除',
}
export const actionLabel = (v: string | null | undefined) => label(ACTION, v)

// llm tier
const TIER: Record<string, string> = {
  flagship: '旗舰',
  standard: '标准',
  small: '小型',
  distill: '蒸馏',
  embedding: '嵌入',
}
export const tierLabel = (v: string | null | undefined) => label(TIER, v)
