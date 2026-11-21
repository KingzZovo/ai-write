'use client'

/** Minimal i18n runtime (chunk-15).
 *
 * - Locale lives in a cookie (`ai-write-locale`) so SSR + client match after
 *   first render and it survives reloads.
 * - `useT()` returns a translator function. Missing keys fall back to the
 *   default-locale catalog and finally to the key itself.
 * - `useLocale()` returns `{ locale, setLocale }` for UI switchers.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react"
import {
  catalogs,
  DEFAULT_LOCALE,
  LOCALES,
  type Locale,
  type MessageKey,
} from "./messages"

const COOKIE_NAME = "ai-write-locale"
const COOKIE_MAX_AGE = 60 * 60 * 24 * 365 // 1 year

type I18nContextValue = {
  locale: Locale
  setLocale: (next: Locale) => void
  t: (key: MessageKey, fallback?: string) => string
}

const I18nContext = createContext<I18nContextValue | null>(null)

function readCookieLocale(): Locale {
  if (typeof document === "undefined") return DEFAULT_LOCALE
  const match = document.cookie.match(
    new RegExp("(?:^|; )" + COOKIE_NAME + "=([^;]*)")
  )
  const raw = match ? decodeURIComponent(match[1]) : ""
  return (LOCALES as readonly string[]).includes(raw)
    ? (raw as Locale)
    : DEFAULT_LOCALE
}

function writeCookieLocale(locale: Locale) {
  if (typeof document === "undefined") return
  document.cookie =
    COOKIE_NAME +
    "=" +
    encodeURIComponent(locale) +
    "; path=/; max-age=" +
    COOKIE_MAX_AGE +
    "; samesite=lax"
}

export function I18nProvider({ children }: { children: React.ReactNode }) {
  // Always render default locale on first paint so SSR markup is stable, then
  // hydrate to the cookie value in an effect.
  const [locale, setLocaleState] = useState<Locale>(DEFAULT_LOCALE)

  useEffect(() => {
    const initial = readCookieLocale()
    if (initial !== locale) setLocaleState(initial)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const setLocale = useCallback((next: Locale) => {
    setLocaleState(next)
    writeCookieLocale(next)
    if (typeof document !== "undefined") {
      document.documentElement.lang = next === "zh" ? "zh-CN" : "en"
    }
  }, [])

  const t = useCallback(
    (key: MessageKey, fallback?: string): string => {
      const active = catalogs[locale] as Record<string, string>
      const base = catalogs[DEFAULT_LOCALE] as Record<string, string>
      return active[key] ?? base[key] ?? fallback ?? key
    },
    [locale]
  )

  const value = useMemo(
    () => ({ locale, setLocale, t }),
    [locale, setLocale, t]
  )

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>
}

export function useI18n(): I18nContextValue {
  const ctx = useContext(I18nContext)
  if (!ctx) {
    throw new Error("useI18n must be used inside <I18nProvider>")
  }
  return ctx
}

export function useT() {
  return useI18n().t
}

export function useLocale() {
  const { locale, setLocale } = useI18n()
  return { locale, setLocale }
}
