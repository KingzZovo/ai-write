import { apiFetch } from '@/lib/api'
import type { Chapter } from '@/stores/projectStore'

export async function moveAdjacentChapter(projectId: string, chapter: Chapter, neighbor: Chapter) {
  const versions = [chapter.order_version, neighbor.order_version]
  if (versions.some(version => typeof version !== 'string' || !/^[0-9a-f]{64}$/.test(version))) {
    throw new Error('章节顺序资料尚未更新，请重新读取章节后再移动。')
  }
  const result = await apiFetch<{ chapter_id: string; neighbor_id: string; chapter_idx: number; neighbor_chapter_idx: number }>(
    `/api/projects/${projectId}/chapters/${chapter.id}/move`, {
      method: 'POST', body: JSON.stringify({ neighbor_id: neighbor.id,
        expected_version: chapter.order_version, expected_neighbor_version: neighbor.order_version }),
    })
  if (result?.chapter_id !== chapter.id || result.neighbor_id !== neighbor.id ||
      result.chapter_idx !== (neighbor.chapter_idx ?? neighbor.chapterIdx) ||
      result.neighbor_chapter_idx !== (chapter.chapter_idx ?? chapter.chapterIdx)) {
    throw new Error('移动结果尚未确认，请重新读取章节并核对顺序。')
  }
}

export function chapterPositionLabel(chapter: Chapter) {
  return Number.isSafeInteger(chapter.global_idx) && chapter.global_idx! > 0
    ? `第${chapter.global_idx}章` : `卷内第${chapter.chapter_idx ?? chapter.chapterIdx}章`
}
