import type { VolumeOutlineReceipt, VolumeOutlineSource, VolumeOutlineWrite } from '@/lib/volumeOutlines'

export function volumeSource(projectId = 'p', volumeId = 'v', text = '原卷纲：先交钱，再带弟弟看腿。'): VolumeOutlineSource {
  return { schema: 1, project_id: projectId, volume_id: volumeId, volume_idx: 1, title: '归来',
    outline: { id: `outline-${volumeId}`, parent_id: null, content_json: { volume_id: volumeId, volume_idx: 1, raw_text: text },
      version: 2, is_confirmed: 1 }, selection: [{ id: `outline-${volumeId}`, hash: 'a'.repeat(64) }] }
}

export function volumeReceipt(input: VolumeOutlineWrite): VolumeOutlineReceipt {
  const source = structuredClone(input.expected_source)
  source.outline = { id: source.outline?.id ?? `outline-${source.volume_id}`, parent_id: source.outline?.parent_id ?? null,
    content_json: input.content_json ?? { ...source.outline?.content_json, raw_text: input.text, _author_text_revision: 'saved' },
    version: (source.outline?.version ?? 0) + 1, is_confirmed: 1 }
  source.selection = [{ id: source.outline.id, hash: 'b'.repeat(64) }]
  return { status: 'saved', request_id: input.request_id, source, changed: true, preserved_chapters: 3,
    candidate_task_id: input.candidate?.task_id ?? null, submitted_json: input.content_json ?? null,
    synced_chapters: 0, unmaterialized_chapters: 0 }
}
