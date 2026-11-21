type SavedOutline = { id: string; level: string; content_json: Record<string, unknown>;
  version?: number; is_confirmed?: number | boolean; created_at?: string | null }
type SavedVolume = { id: string; volume_idx: number }

function createdAt(value?: string | null) {
  if (!value) return 0
  const millis = Date.parse(value)
  const fraction = /\.(\d+)(?:Z|[+-]\d{2}:\d{2})$/.exec(value)?.[1] ?? ''
  return Number.isFinite(millis) ? millis * 1000 + Number(fraction.padEnd(6, '0').slice(3, 6)) : 0
}

/** Same ownership and confirmed/version/time/ID order as the saved-source API. */
export function selectVolumeOutlines<T extends SavedOutline>(outlines: T[], volumes: SavedVolume[]): Record<string, T> {
  const byId = new Map(volumes.map((volume) => [volume.id, volume]))
  const byIndex = new Map<string, SavedVolume[]>()
  for (const volume of volumes) {
    const key = String(volume.volume_idx)
    byIndex.set(key, [...(byIndex.get(key) ?? []), volume])
  }
  const sorted = outlines.filter((outline) => outline.level === 'volume').sort((a, b) =>
    Number(Boolean(b.is_confirmed)) - Number(Boolean(a.is_confirmed)) || (b.version ?? 0) - (a.version ?? 0) ||
    createdAt(b.created_at) - createdAt(a.created_at) || (a.id === b.id ? 0 : a.id < b.id ? 1 : -1))
  const selected: Record<string, T> = {}
  for (const outline of sorted) {
    const data = outline.content_json ?? {}
    const volume = data.volume_id !== null && data.volume_id !== undefined
      ? byId.get(String(data.volume_id)) : byIndex.get(String(data.volume_idx))?.[0]
    if (!volume || byIndex.get(String(volume.volume_idx))?.length !== 1 || selected[volume.id]) continue
    selected[volume.id] = outline
  }
  return selected
}
