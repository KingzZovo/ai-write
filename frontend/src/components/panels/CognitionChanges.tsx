import { cognitionFact, cognitionKind, cognitionKindLabels, validCognitionChange } from '@/lib/cognitionTypes'
import type { CognitionChange } from '@/lib/cognitionTypes'

export function CognitionChanges({ changes, emptyLabel = '本次提取已结束，未提取出明确的认知变化；这不表示人物什么都不知道。' }: {
  changes: CognitionChange[]; emptyLabel?: string
}) {
  if (!Array.isArray(changes) || !changes.every(validCognitionChange)) {
    return <p role="alert" className="text-red-700">认知条目不完整，请刷新后核对。</p>
  }
  if (!changes.length) return <p>{emptyLabel}</p>
  return <div className="space-y-3">
    {changes.map((change, index) => <div key={index} className="space-y-1">
      <p><strong>{change.character === '__reader__' ? '读者' : change.character}</strong> · {cognitionKindLabels[cognitionKind(change)]}：{cognitionFact(change)}</p>
      <blockquote className="whitespace-pre-wrap break-words border-l-2 border-stone-300 pl-3 text-gray-600">{change.evidence}</blockquote>
    </div>)}
  </div>
}
