/** i18n message catalog for ai-write.
 *
 * Keep keys flat dot-notation so JSON merges and lint stay sane.
 * When adding a new key: add it to zh first (source of truth) then mirror
 * into en. The useT() hook will fall back to zh when a key is missing in en.
 *
 * Chunk 15: scaffolding.
 * Chunk 20: real English translations + settings/preferences keys.
 */

export const zh = {
  "app.name": "AI Write",
  "nav.workspace": "工作区",
  "nav.workbench": "工作台",
  "nav.home": "首页",
  "nav.knowledge": "知识库",
  "nav.styles": "写法",
  "nav.filterWords": "过滤词",
  "nav.prompts": "Prompt",
  "nav.llmRouting": "路由",
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
  "common.loading": "加载中...",
  "common.save": "保存",
  "common.saving": "保存中...",
  "common.cancel": "取消",
  "common.delete": "删除",
  "common.edit": "编辑",
  "common.test": "测试",
  "common.add": "新增",
  "common.refresh": "刷新",
  "common.close": "关闭",
  "settings.page.title": "模型设置",
  "settings.preferences.title": "偏好",
  "settings.preferences.language": "界面语言",
  "settings.preferences.languageHint": "切换后立即生效，并保存在浏览器 Cookie 中。",
  "settings.v05.title": "v0.5 变更",
  "settings.v05.body": "任务路由已下沉到每个 Prompt。请在 Prompt 注册表为每个 Prompt 独立指定端点、模型与温度。本页只管理端点本身。",
  "settings.v05.link": "Prompt 注册表",
  "settings.endpoints.loading": "正在加载模型配置...",
  "settings.error.loadFailed": "加载设置失败",
} as const

export type MessageKey = keyof typeof zh

export const en: Record<MessageKey, string> = {
  "app.name": "AI Write",
  "nav.workspace": "Workspace",
  "nav.workbench": "Workbench",
  "nav.home": "Home",
  "nav.knowledge": "Knowledge",
  "nav.styles": "Styles",
  "nav.filterWords": "Filter Words",
  "nav.prompts": "Prompts",
  "nav.llmRouting": "Routing",
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
  "common.loading": "Loading...",
  "common.save": "Save",
  "common.saving": "Saving...",
  "common.cancel": "Cancel",
  "common.delete": "Delete",
  "common.edit": "Edit",
  "common.test": "Test",
  "common.add": "Add",
  "common.refresh": "Refresh",
  "common.close": "Close",
  "settings.page.title": "Model settings",
  "settings.preferences.title": "Preferences",
  "settings.preferences.language": "Interface language",
  "settings.preferences.languageHint": "Takes effect immediately; stored in a browser cookie.",
  "settings.v05.title": "v0.5 change",
  "settings.v05.body": "Task routing now lives on each prompt. Configure endpoint, model and temperature per prompt in the prompt registry. This page only manages endpoints.",
  "settings.v05.link": "Prompt registry",
  "settings.endpoints.loading": "Loading model configuration...",
  "settings.error.loadFailed": "Failed to load settings",
}

export const catalogs = { zh, en } as const
export type Locale = keyof typeof catalogs
export const LOCALES: Locale[] = ["zh", "en"]
export const DEFAULT_LOCALE: Locale = "zh"
