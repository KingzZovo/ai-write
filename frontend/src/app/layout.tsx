import type { Metadata, Viewport } from "next";
import { Suspense } from "react";
import { Navbar } from "@/components/Navbar";
import { I18nProvider } from "@/lib/i18n/I18nProvider";
import { ErrorBoundary } from "@/components/ErrorBoundary";
import "./globals.css";

export const metadata: Metadata = {
  title: "AI Write",
  description: "AI-Powered Novel Writing Platform",
};

// chunk-16: set viewport so mobile browsers render at the correct width and
// honor the iOS/Android safe-area insets exposed via env(safe-area-inset-*).
export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
  themeColor: "#ffffff",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="zh-CN" className="h-full">
      <body className="min-h-full">
        <I18nProvider>
          <ErrorBoundary>
          <Suspense fallback={null}>
            <Navbar />
          </Suspense>
          {children}
          </ErrorBoundary>
        </I18nProvider>
      </body>
    </html>
  );
}
