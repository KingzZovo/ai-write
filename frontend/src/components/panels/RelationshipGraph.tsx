'use client'

import React, { useState, useEffect, useMemo } from 'react'
import { apiFetch } from '@/lib/api'

interface Character {
  id: string
  name: string
  profileJson: {
    identity?: string
    [key: string]: unknown
  }
}

interface Relationship {
  id: string
  sourceId: string
  targetId: string
  label: string
  type?: string
}

interface CharacterData {
  characters: Character[]
  relationships: Relationship[]
}

interface RelationshipGraphProps {
  projectId: string
}

const GRAPH_SIZE = 280
const CENTER = GRAPH_SIZE / 2
const RADIUS = 100
const NODE_RADIUS = 22

function getNodePositions(count: number): { x: number; y: number }[] {
  if (count === 0) return []
  if (count === 1) return [{ x: CENTER, y: CENTER }]

  return Array.from({ length: count }, (_, i) => {
    const angle = (2 * Math.PI * i) / count - Math.PI / 2
    return {
      x: CENTER + RADIUS * Math.cos(angle),
      y: CENTER + RADIUS * Math.sin(angle),
    }
  })
}

const NODE_COLORS = [
  '#3B82F6', '#8B5CF6', '#EC4899', '#F59E0B',
  '#10B981', '#EF4444', '#6366F1', '#14B8A6',
]

export function RelationshipGraph({ projectId }: RelationshipGraphProps) {
  const [characters, setCharacters] = useState<Character[]>([])
  const [relationships, setRelationships] = useState<Relationship[]>([])
  const [loading, setLoading] = useState(false)
  const [hoveredNode, setHoveredNode] = useState<string | null>(null)

  useEffect(() => {
    if (!projectId) return
    setLoading(true)

    async function fetchData() {
      try {
        // Try fetching characters and relationships
        const chars = await apiFetch<Character[] | CharacterData>(
          `/api/projects/${projectId}/characters`
        )

        if (Array.isArray(chars)) {
          setCharacters(chars)
          // Try to fetch relationships separately
          try {
            const rels = await apiFetch<Relationship[]>(
              `/api/projects/${projectId}/relationships`
            )
            setRelationships(rels)
          } catch {
            setRelationships([])
          }
        } else {
          // API returns { characters, relationships } together
          setCharacters(chars.characters || [])
          setRelationships(chars.relationships || [])
        }
      } catch {
        setCharacters([])
        setRelationships([])
      } finally {
        setLoading(false)
      }
    }

    fetchData()
  }, [projectId])

  const positions = useMemo(
    () => getNodePositions(characters.length),
    [characters.length]
  )

  const charIndexMap = useMemo(() => {
    const map = new Map<string, number>()
    characters.forEach((c, i) => map.set(c.id, i))
    return map
  }, [characters])

  if (loading) {
    return <p className="text-xs text-gray-400">Loading graph...</p>
  }

  if (characters.length === 0) {
    return (
      <p className="text-xs text-gray-400">
        No characters defined. Add characters in Settings to see the relationship graph.
      </p>
    )
  }

  return (
    <div className="space-y-2">
      <h3 className="text-sm font-semibold text-gray-900">Character Relations</h3>

      <div className="flex justify-center">
        <svg
          width={GRAPH_SIZE}
          height={GRAPH_SIZE}
          viewBox={`0 0 ${GRAPH_SIZE} ${GRAPH_SIZE}`}
          className="overflow-visible"
        >
          {/* Relationship lines */}
          {relationships.map((rel) => {
            const srcIdx = charIndexMap.get(rel.sourceId)
            const tgtIdx = charIndexMap.get(rel.targetId)
            if (srcIdx === undefined || tgtIdx === undefined) return null

            const src = positions[srcIdx]
            const tgt = positions[tgtIdx]
            const midX = (src.x + tgt.x) / 2
            const midY = (src.y + tgt.y) / 2

            const isHighlighted =
              hoveredNode === rel.sourceId || hoveredNode === rel.targetId

            return (
              <g key={rel.id}>
                <line
                  x1={src.x}
                  y1={src.y}
                  x2={tgt.x}
                  y2={tgt.y}
                  stroke={isHighlighted ? '#3B82F6' : '#D1D5DB'}
                  strokeWidth={isHighlighted ? 2 : 1}
                  strokeDasharray={isHighlighted ? undefined : '4 2'}
                />
                {rel.label && (
                  <text
                    x={midX}
                    y={midY - 4}
                    textAnchor="middle"
                    fontSize={9}
                    fill={isHighlighted ? '#1D4ED8' : '#9CA3AF'}
                    className="pointer-events-none select-none"
                  >
                    {rel.label}
                  </text>
                )}
              </g>
            )
          })}

          {/* Character nodes */}
          {characters.map((char, idx) => {
            const pos = positions[idx]
            const color = NODE_COLORS[idx % NODE_COLORS.length]
            const isHovered = hoveredNode === char.id

            return (
              <g
                key={char.id}
                onMouseEnter={() => setHoveredNode(char.id)}
                onMouseLeave={() => setHoveredNode(null)}
                className="cursor-pointer"
              >
                {/* Node circle */}
                <circle
                  cx={pos.x}
                  cy={pos.y}
                  r={isHovered ? NODE_RADIUS + 2 : NODE_RADIUS}
                  fill={color}
                  opacity={isHovered ? 1 : 0.85}
                  stroke={isHovered ? '#1E40AF' : 'white'}
                  strokeWidth={isHovered ? 2 : 1.5}
                />
                {/* Character name */}
                <text
                  x={pos.x}
                  y={pos.y + 1}
                  textAnchor="middle"
                  dominantBaseline="central"
                  fontSize={10}
                  fontWeight={600}
                  fill="white"
                  className="pointer-events-none select-none"
                >
                  {char.name.length > 4
                    ? char.name.slice(0, 4) + '...'
                    : char.name}
                </text>
                {/* Full name on hover - shown below node */}
                {isHovered && char.name.length > 4 && (
                  <text
                    x={pos.x}
                    y={pos.y + NODE_RADIUS + 12}
                    textAnchor="middle"
                    fontSize={10}
                    fill="#374151"
                    className="pointer-events-none"
                  >
                    {char.name}
                  </text>
                )}
              </g>
            )
          })}
        </svg>
      </div>

      {/* Legend showing count */}
      <div className="text-center text-[10px] text-gray-400">
        {characters.length} character{characters.length !== 1 ? 's' : ''}
        {relationships.length > 0 &&
          ` / ${relationships.length} relationship${relationships.length !== 1 ? 's' : ''}`}
      </div>
    </div>
  )
}
