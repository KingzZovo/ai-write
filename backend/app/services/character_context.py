"""Character cards retain source-verified state fields without prose rewriting."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import json
from typing import Any

from app.services.character_locations import LOCATION_FIELDS, latest_location_records, latest_location_state_records


POWER_FIELDS = ('power_level', '能力等级', '实力')
MENTAL_FIELDS = ('mental_state', '情绪')


def _source_index(source: dict[str, Any]) -> int:
    index = source.get('global_idx')
    if type(index) is not int or index <= 0:
        raise ValueError('character_state_source_chapter_unavailable')
    return index


def _source_receipt(source: dict[str, Any]) -> dict[str, Any]:
    if (not isinstance(source, dict) or not source.get('source_chapter_id')
            or not isinstance(source.get('source_hash'), str) or not source['source_hash']):
        raise ValueError('character_state_source_unavailable')
    return dict(global_idx=_source_index(source), source_chapter_id=str(source['source_chapter_id']),
                source_hash=source['source_hash'])


@dataclass
class CharacterCard:
    """Profile references plus the latest verified value of each exact field.

    Scalar attributes remain available to existing callers. The prompt uses
    complete chapter fields and their individual receipts, including null,
    false, zero, arrays and objects. General state is not a synonym for mood.
    """

    name: str
    location: str = ''
    power_level: str = ''
    relationships: dict[str, str] = field(default_factory=dict)
    mental_state: str = ''
    recent_actions: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    chapter_state: dict[str, Any] = field(default_factory=dict)
    chapter_state_sources: dict[str, dict[str, Any]] = field(default_factory=dict)
    location_source: dict[str, Any] | None = None
    chapter_locations: list[dict[str, Any]] = field(default_factory=list)
    chapter_location_states: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.location_source is not None:
            self.location_source = _source_receipt(self.location_source)
            if self.location:
                self.apply_locations([dict(location=self.location, **self.location_source)])

    def _latest_field(self, keys: tuple[str, ...]) -> str | None:
        candidates = [(_source_index(self.chapter_state_sources[key]), -order, key)
                      for order, key in enumerate(keys) if key in self.chapter_state]
        return max(candidates)[2] if candidates else None

    def apply_chapter_state(self, status: dict[str, Any], sources: dict[str, dict[str, Any]]) -> None:
        """Merge verified stores by source chapter; an older replica cannot win.

        Inputs come from verified_character_states / verified_world_snapshot.
        This method preserves their provenance, it does not verify raw data.
        """
        if not isinstance(status, dict) or not isinstance(sources, dict):
            raise ValueError('invalid_character_state')
        json.dumps(status, ensure_ascii=False, allow_nan=False)
        values, receipts = deepcopy(self.chapter_state), deepcopy(self.chapter_state_sources)
        for key, value in status.items():
            if not isinstance(key, str) or not key.strip():
                raise ValueError('invalid_character_state_field')
            source = _source_receipt(sources.get(key))
            index = source['global_idx']
            if key in receipts and index < _source_index(receipts[key]):
                continue
            values[key] = deepcopy(value)
            receipts[key] = source
        self.chapter_state, self.chapter_state_sources = values, receipts

        for attribute, keys in (('power_level', POWER_FIELDS), ('mental_state', MENTAL_FIELDS)):
            key = self._latest_field(keys)
            if key is not None:
                value = self.chapter_state[key]
                setattr(self, attribute, value.strip() if isinstance(value, str) else '')

        self._refresh_location()

    def apply_locations(self, rows: list[dict[str, Any]]) -> None:
        """Merge verified stores without choosing among same-chapter places."""
        if any(row.get('name', self.name) != self.name for row in rows):
            raise ValueError('character_location_owner_changed')
        self.chapter_locations = latest_location_records(
            [*self.chapter_locations, *(dict(row, name=self.name) for row in rows)])
        self._refresh_location()

    def apply_location_states(self, rows: list[dict[str, Any]]) -> None:
        """Retain independent verified position fields before replica merging."""
        if any(row.get('name', self.name) != self.name for row in rows):
            raise ValueError('character_location_owner_changed')
        self.chapter_location_states = latest_location_state_records(
            [*self.chapter_location_states, *(dict(row, name=self.name) for row in rows)])
        self._refresh_location()

    def _location_observations(self) -> list[tuple[Any, dict[str, Any]]]:
        states = [(self.chapter_state[key], self.chapter_state_sources[key])
                  for key in LOCATION_FIELDS if key in self.chapter_state]
        states.extend((row['value'], row) for row in self.chapter_location_states)
        records = [(row['location'], row) for row in self.chapter_locations]
        latest = max((_source_index(source) for _, source in [*states, *records]), default=0)
        return [(value, source) for value, source in [*states, *records] if _source_index(source) == latest]

    def _refresh_location(self) -> None:
        current = self._location_observations()
        if not current:
            return  # Keep the explicitly labelled profile reference.
        value, source = current[0]
        encoded = json.dumps(value, sort_keys=True, ensure_ascii=False)
        same = all(json.dumps(item, sort_keys=True, ensure_ascii=False) == encoded for item, _ in current)
        # Consumers of the scalar shortcut must not mistake a storage/list
        # preference for an established position. Full observations remain.
        self.location = value.strip() if same and isinstance(value, str) else ''
        self.location_source = _source_receipt(source)

    def to_prompt(self) -> str:
        parts = [f'[{self.name}]']
        parts.append('姓名用于确认身份；可按语境使用有来源的别名与称谓，不得擅自改名或混同人物。')
        if self.aliases:
            parts.append('档案登记称呼（可能重名，须结合关系判断）:' + '、'.join(self.aliases))
        location_key = self._latest_field(LOCATION_FIELDS)
        # Do not repeat the same location both as a shortcut and a raw field.
        state_location = self.chapter_state.get(location_key)
        location_in_state = (location_key is not None and
                             self.chapter_state_sources[location_key] == self.location_source
                             and isinstance(state_location, str) and state_location.strip() == self.location)
        if self.location and not location_in_state and not self.chapter_locations:
            label = f'位置[CH-{_source_index(self.location_source)}]' if self.location_source else '档案位置'
            parts.append(f'{label}:{self.location}')
        if self.power_level and self._latest_field(POWER_FIELDS) is None:
            parts.append(f'档案实力:{self.power_level}')
        if self.relationships:
            rels = ', '.join(f'{key}:{value}' for key, value in self.relationships.items())
            parts.append(f'档案关系参考:{rels}')
        if self.mental_state and self._latest_field(MENTAL_FIELDS) is None:
            parts.append(f'档案心理:{self.mental_state}')
        if self.recent_actions:
            parts.append(f'档案近期记录:{"; ".join(self.recent_actions[-3:])}')

        # A group contains only fields from this exact receipt, never an
        # inherited whole-character snapshot attributed to the latest chapter.
        groups: dict[tuple[int, str, str], dict[str, Any]] = {}
        for key, value in self.chapter_state.items():
            source = self.chapter_state_sources[key]
            group = (_source_index(source), str(source['source_chapter_id']), source['source_hash'])
            groups.setdefault(group, {})[key] = value
        lines = [' | '.join(parts)]
        for row in self.chapter_locations:
            lines.append(f"  位置[CH-{row['global_idx']}]:{row['location']}")
        current = self._location_observations()
        if len({json.dumps(value, sort_keys=True, ensure_ascii=False) for value, _ in current}) > 1:
            lines.append('  同章位置记录未标明先后，不能按列表顺序决定当前位置；须结合原章动作与行程核对。')
        for (index, _, _), values in sorted(groups.items()):
            lines.append(f'  正文状态[CH-{index}]：' + json.dumps(values, ensure_ascii=False, allow_nan=False))
        for row in self.chapter_location_states:
            key = row['field']
            if (self.chapter_state_sources.get(key) == _source_receipt(row)
                    and json.dumps(self.chapter_state.get(key), sort_keys=True, ensure_ascii=False) ==
                        json.dumps(row['value'], sort_keys=True, ensure_ascii=False)):
                continue  # The exact observation is already in the state group.
            lines.append(f"  正文位置字段[CH-{row['global_idx']}]：" +
                         json.dumps({key: row['value']}, ensure_ascii=False, allow_nan=False))
        return '\n'.join(lines)


def registered_alias_candidates(cards: list[CharacterCard]) -> dict[str, list[str]]:
    """Recall all registered owners without assigning an alias to one person.

    Exact character names take precedence over another card's alias. These
    candidates help select context; they do not rewrite names in prose facts.
    """
    names = {card.name for card in cards}
    candidates: dict[str, set[str]] = {}
    for card in cards:
        for alias in card.aliases:
            if alias and alias not in names:
                candidates.setdefault(alias, set()).add(card.name)
    return {alias: sorted(owners) for alias, owners in sorted(candidates.items())}
