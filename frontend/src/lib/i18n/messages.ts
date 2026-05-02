/** i18n message catalog for ai-write (chunk-15).
 *
 * Keep keys flat dot-notation so JSON merges and lint stay sane.
 * When adding a new key: add it to zh first (source of truth) then mirror
 * into en. The useT() hook will fall back to zh when a key is missing in en.
 */

export const zh = {
  "app.name": "AI Write",
  "nav.workspace": "工作区",
  "nav.knowledge": "知识库",
  "nav.styles": "写法",
  "nav.filterWords": "过滤词",
  "nav.prompts": "Prompt",
  "nav.settings": "设置",
  "auth.logout": "登出",
  "locale.switch": "语言",
  "locale.zh": "中文",
  "locale.en": "English",
  "workspace.tab.sidebar": "目录",
  "workspace.tab.editor": "编辑",
  "workspace.tab.panel": "工具",
  "workspace.sidebar.collapse": "折叠侧栏",
  "workspace.sidebar.expand": "展开侧栏",
  "workspace.panel.collapse": "折叠工具栏",
  "workspace.panel.expand": "展开工具栏",
} as const

export type MessageKey = keyof typeof zh

export const en: Record<MessageKey, string> = {
  "app.name": "AI Write",
  "nav.workspace": "Workspace",
  "nav.knowledge": "Knowledge",
  "nav.styles": "Styles",
  "nav.filterWords": "Filter Words",
  "nav.prompts": "Prompts",
  "nav.settings": "Settings",
  "auth.logout": "Log out",
  "locale.switch": "Language",
  "locale.zh": "中文",
  "locale.en": "English",
  "workspace.tab.sidebar": "Outline",
  "workspace.tab.editor": "Editor",
  "workspace.tab.panel": "Tools",
  "workspace.sidebar.collapse": "Collapse sidebar",
  "workspace.sidebar.expand": "Expand sidebar",
  "workspace.panel.collapse": "Collapse tools",
  "workspace.panel.expand": "Expand tools",
}

export const catalogs = { zh, en } as const
export type Locale = keyof typeof catalogs
export const LOCALES: Locale[] = ["zh", "en"]
export const DEFAULT_LOCALE: Locale = "zh"
