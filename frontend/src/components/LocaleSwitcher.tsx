'use client'

import { useLocale, useT } from "@/lib/i18n/I18nProvider"
import { LOCALES, type Locale } from "@/lib/i18n/messages"

export function LocaleSwitcher() {
  const { locale, setLocale } = useLocale()
  const t = useT()

  return (
    <label className="flex items-center gap-1 text-xs md:text-sm text-gray-600">
      <span className="hidden sm:inline">{t("locale.switch")}:</span>
      <select
        value={locale}
        onChange={(e) => setLocale(e.target.value as Locale)}
        aria-label={t("locale.switch")}
        className="bg-transparent border border-gray-200 rounded px-1 py-0.5 text-xs md:text-sm focus:outline-none focus:ring-1 focus:ring-brand-500"
      >
        {LOCALES.map((loc) => (
          <option key={loc} value={loc}>
            {t(("locale." + loc) as "locale.zh" | "locale.en")}
          </option>
        ))}
      </select>
    </label>
  )
}
