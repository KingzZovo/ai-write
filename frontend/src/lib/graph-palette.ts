// chunk-22: shared color palette for relationship-graph data viz.
//
// Tailwind v4 tokens live in frontend/src/app/globals.css (@theme block) and
// are the canonical source. SVG / Canvas APIs (ForceGraph2D, <circle fill=>,
// ctx.fillStyle) cannot use `var(--color-*)` directly at draw time without
// a DOM read, so we mirror the hex values here and document them as a
// deliberate data-viz mirror of the design tokens. Keep this file and
// globals.css in sync.
//
// Business UI components must consume colors via Tailwind token utilities
// (bg-brand-500, text-text-muted, shadow-card, rounded-card) and never via
// hex literals. This module is the single approved exception.

// Sentiment colors -- mirror --color-success-500 / --color-danger-500 /
// slate-500 (neutral) from the design-token palette.
export const SENTIMENT_POSITIVE = '#10b981' // success-500
export const SENTIMENT_POSITIVE_ALT = '#22c55e' // green-500 used by global graph
export const SENTIMENT_NEGATIVE = '#ef4444' // danger-500
export const SENTIMENT_NEUTRAL = '#64748b' // slate-500
export const SENTIMENT_NEUTRAL_ALT = '#9ca3af' // gray-400 for panel variant

// Brand highlights -- mirror --color-brand-* / blue shades for selection
// and hover states on nodes/edges.
export const NODE_FILL_PRIMARY = '#60a5fa' // blue-400 (light node)
export const NODE_FILL_BRAND = '#3b82f6' // blue-500 (panel highlight)
export const NODE_STROKE_BRAND_DEEP = '#1d4ed8' // blue-700 (selected label)
export const NODE_STROKE_BRAND_DARKER = '#1e40af' // blue-800 (hover ring)

// Surface / text colors -- mirror --surface-inverted / --text-inverted /
// slate-200 (text on dark bg).
export const GRAPH_BG_DARK = '#0f172a' // slate-900 (canvas bg)
export const GRAPH_TEXT_ON_DARK = '#e2e8f0' // slate-200 (label on dark canvas)
export const GRAPH_LABEL_ON_LIGHT = '#374151' // gray-700 (hover label on light panel)

// Character-node rotating palette -- 8 distinct hues for up to 8 main chars.
// Mirror of --color-brand + complementary hues.
export const NODE_COLOR_PALETTE: ReadonlyArray<string> = [
  '#3b82f6', // blue-500
  '#8b5cf6', // violet-500
  '#ec4899', // pink-500
  '#f59e0b', // warning-500
  '#10b981', // success-500
  '#ef4444', // danger-500
  '#6366f1', // brand-500
  '#14b8a6', // teal-500
]
