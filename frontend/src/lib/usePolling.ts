import { useEffect, useRef } from 'react'

/**
 * Run `fn` every `intervalMs` while `active` is true.
 * - cleans up on unmount / when `active` flips false
 * - transient errors do NOT stop polling; call stop() from fn on terminal states
 */
export function usePolling(
  fn: (stop: () => void) => void | Promise<void>,
  intervalMs: number,
  active: boolean,
) {
  const fnRef = useRef(fn)
  // Keep the latest callback without restarting the interval (latest-ref
  // pattern; assigned in an effect to satisfy react-hooks/refs).
  useEffect(() => {
    fnRef.current = fn
  })

  useEffect(() => {
    if (!active) return
    let stopped = false
    const id = setInterval(() => {
      if (stopped) return
      const stop = () => { stopped = true; clearInterval(id) }
      void Promise.resolve(fnRef.current(stop)).catch(() => { /* transient; keep polling */ })
    }, intervalMs)
    return () => { stopped = true; clearInterval(id) }
  }, [intervalMs, active])
}
