// v1.2.0 / chunk-26 -- Browser-side Sentry shim.
//
// Goals:
//  * Zero hard dependency on @sentry/browser at build time. If the package is
//    not installed (current default), this file is a silent no-op so `next
//    build` keeps working with no extra deps.
//  * If NEXT_PUBLIC_SENTRY_DSN is unset, do nothing -- mirrors the backend
//    init_sentry() contract.
//  * If both DSN + @sentry/browser are present, initialize and forward
//    unhandled errors / promise rejections / Next.js route navigation errors.
//  * Never throw. Failure to wire Sentry must not break the app.

export type SentryShimStatus = "disabled" | "missing-sdk" | "initialized" | "error";

let _status: SentryShimStatus = "disabled";

export function getSentryStatus(): SentryShimStatus {
  return _status;
}

export async function initClientSentry(): Promise<SentryShimStatus> {
  if (typeof window === "undefined") {
    // Server / build-time -- nothing to do here.
    return _status;
  }

  const dsn = (process.env.NEXT_PUBLIC_SENTRY_DSN || "").trim();
  if (!dsn) {
    _status = "disabled";
    return _status;
  }

  let SentryModule: any = null;
  try {
    // Dynamic import so the bundler does not hard-require @sentry/browser.
    // If the package is absent, the import throws and we silently skip.
    SentryModule = await import(/* webpackIgnore: true */ "@sentry/browser").catch(
      () => null,
    );
  } catch {
    SentryModule = null;
  }
  if (!SentryModule || typeof SentryModule.init !== "function") {
    _status = "missing-sdk";
    return _status;
  }

  try {
    SentryModule.init({
      dsn,
      environment: process.env.NEXT_PUBLIC_SENTRY_ENV || "dev",
      release:
        process.env.NEXT_PUBLIC_GIT_TAG ||
        process.env.NEXT_PUBLIC_GIT_SHA ||
        "unknown",
      tracesSampleRate: Number(
        process.env.NEXT_PUBLIC_SENTRY_TRACES_SAMPLE_RATE || "0.05",
      ),
      // Strip query strings -- they can carry tokens. Sentry already
      // scrubs common keys, but we are conservative.
      beforeSend(event: any) {
        try {
          if (event && event.request && typeof event.request.url === "string") {
            event.request.url = event.request.url.split("?")[0];
          }
          if (event && event.request && event.request.headers) {
            const h = event.request.headers as Record<string, string>;
            for (const k of Object.keys(h)) {
              if (/authorization|cookie|api[-_]?key|token/i.test(k)) {
                h[k] = "***";
              }
            }
          }
        } catch {
          /* never block capture */
        }
        return event;
      },
    });

    // Forward unhandled errors + promise rejections explicitly. Sentry's
    // browser integration covers these by default, but we register listeners
    // so route-level error boundaries can opt in too.
    window.addEventListener("error", (ev) => {
      try {
        SentryModule.captureException(ev.error || new Error(ev.message));
      } catch {
        /* ignore */
      }
    });
    window.addEventListener("unhandledrejection", (ev) => {
      try {
        SentryModule.captureException(
          (ev as PromiseRejectionEvent).reason || new Error("unhandledrejection"),
        );
      } catch {
        /* ignore */
      }
    });

    _status = "initialized";
    return _status;
  } catch {
    _status = "error";
    return _status;
  }
}

// Auto-init on module evaluation when running in the browser.
if (typeof window !== "undefined") {
  // Fire-and-forget; the promise result is exposed via getSentryStatus().
  void initClientSentry();
}
