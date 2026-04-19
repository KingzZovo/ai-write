'use client'

export function VolumeOutlineBlock({ data }: { data: Record<string, unknown> }) {
  if (typeof data.raw_text === 'string') {
    return (
      <pre
        className="whitespace-pre-wrap text-gray-700 leading-relaxed"
        style={{ fontFamily: "'Noto Serif SC', serif" }}
      >
        {data.raw_text}
      </pre>
    )
  }

  const coreConflict = typeof data.core_conflict === 'string' ? data.core_conflict : ''
  const emotionalArc = typeof data.emotional_arc === 'string' ? data.emotional_arc : ''
  const turningPoints = Array.isArray(data.turning_points)
    ? ((data.turning_points as unknown[]).filter((p) => typeof p === 'string') as string[])
    : []
  const newCharacters = Array.isArray(data.new_characters)
    ? (data.new_characters as Array<Record<string, unknown>>)
    : []
  const chapterSummaries = Array.isArray(data.chapter_summaries)
    ? (data.chapter_summaries as Array<Record<string, unknown>>)
    : []
  const transition = typeof data.transition_to_next === 'string' ? data.transition_to_next : ''

  return (
    <div className="space-y-3 text-gray-700">
      {coreConflict && (
        <div>
          <div className="text-xs font-semibold text-gray-500 mb-1">核心冲突</div>
          <div>{coreConflict}</div>
        </div>
      )}
      {turningPoints.length > 0 && (
        <div>
          <div className="text-xs font-semibold text-gray-500 mb-1">关键转折</div>
          <ul className="list-disc pl-5 space-y-0.5">
            {turningPoints.map((p, i) => (
              <li key={i}>{p}</li>
            ))}
          </ul>
        </div>
      )}
      {emotionalArc && (
        <div>
          <div className="text-xs font-semibold text-gray-500 mb-1">情感基调</div>
          <div>{emotionalArc}</div>
        </div>
      )}
      {newCharacters.length > 0 && (
        <div>
          <div className="text-xs font-semibold text-gray-500 mb-1">新登场角色</div>
          <ul className="list-disc pl-5 space-y-0.5">
            {newCharacters.map((c, i) => {
              const name = typeof c.name === 'string' ? c.name : '未命名'
              const identity = typeof c.identity === 'string' ? c.identity : ''
              const role = typeof c.role === 'string' ? c.role : ''
              return (
                <li key={i}>
                  <span className="font-medium">{name}</span>
                  {identity && <span className="text-gray-500">（{identity}）</span>}
                  {role && <span className="text-gray-500">：{role}</span>}
                </li>
              )
            })}
          </ul>
        </div>
      )}
      {chapterSummaries.length > 0 && (
        <div>
          <div className="text-xs font-semibold text-gray-500 mb-1">章节摘要</div>
          <ol className="list-decimal pl-5 space-y-1">
            {chapterSummaries.map((cs, i) => {
              const title = typeof cs.title === 'string' ? cs.title : `第${i + 1}章`
              const summary = typeof cs.summary === 'string' ? cs.summary : ''
              return (
                <li key={i}>
                  <span className="font-medium">{title}</span>
                  {summary && <span className="text-gray-500"> — {summary}</span>}
                </li>
              )
            })}
          </ol>
        </div>
      )}
      {transition && (
        <div>
          <div className="text-xs font-semibold text-gray-500 mb-1">衔接下一卷</div>
          <div>{transition}</div>
        </div>
      )}
    </div>
  )
}
