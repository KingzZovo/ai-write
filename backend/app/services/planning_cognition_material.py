"""Retained cognition originals and reproducible per-attempt material choices.

This validates provenance and input identity, not a character's psychology or
the literary quality of a plan. Original observations never become plot facts.
"""
from uuid import UUID
import re

from app.services.cognition_context import observations, observations_fit, render_observations
from app.services.context_errors import RequiredContextUnavailable
from app.services.entity_sources import digest

COGNITION_CHARS = 12000  # Version 1 selection budget; whole evidence units only.
RANKED_LIMIT = 64  # Matches the existing grouped cognition retriever.
HASH = re.compile(r'[0-9a-f]{64}')


class PlanningCognitionMaterialError(ValueError):
    pass


class PlanningCognitionUnavailable(RuntimeError):
    pass


def _hash(value):
    if not isinstance(value, str) or HASH.fullmatch(value) is None:
        raise ValueError('invalid cognition digest')


def _identity(value):
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError('invalid cognition identity')


def validate_cognition_source(source, project_id=None, character_context=None):
    """Check the retained shape, including witnesses for valid empty decisions."""
    try:
        if (not isinstance(source, dict) or set(source) != {'version', 'project_id', 'before_global_idx',
                'snapshot', 'snapshot_hash', 'ledger'} or type(source['version']) is not int or source['version'] != 1):
            raise ValueError('invalid cognition source')
        _identity(source['project_id']); _hash(source['snapshot_hash'])
        if project_id is not None and source['project_id'] != str(project_id):
            raise ValueError('cognition project changed')
        boundary = source['before_global_idx']
        if type(boundary) is not int or not 0 < boundary <= 2147483647:
            raise ValueError('invalid cognition boundary')
        snapshot = source['snapshot']
        if (not isinstance(snapshot, dict) or set(snapshot) != {'boundary', 'positions', 'sources', 'decisions', 'proofs'}
                or type(snapshot['boundary']) is not int or snapshot['boundary'] != boundary
                or any(not isinstance(snapshot[key], dict) for key in ('positions', 'sources', 'decisions', 'proofs'))
                or digest(snapshot) != source['snapshot_hash']):
            raise ValueError('invalid cognition witness')
        positions = snapshot['positions']
        if len(set(positions.values())) != len(positions):
            raise ValueError('ambiguous cognition position')
        for cid, index in positions.items():
            _identity(cid)
            if type(index) is not int or not 0 < index < boundary:
                raise ValueError('invalid cognition source position')
        for key in ('sources', 'decisions', 'proofs'):
            if set(snapshot[key]) != set(positions):
                raise ValueError('incomplete cognition coverage')
            for value in snapshot[key].values(): _hash(value)
        if character_context is not None:
            # 2026-11-17: 认知台账现在也纳入"有正文但状态非 completed"的章
            # (记录不可用时会被降级跳过),所以不能再要求 positions 与 completed 集合
            # 完全相等。改成两条硬约束:
            #   ① positions 里的每一章都必须属于本书(防串书/串卷);
            #   ② 所有 completed 章一个都不能少(防漏读已交付的章)。
            order = character_context['chapter_order']
            known = {row['id'] for row in order}
            required = {row['id'] for row in order if row['status'] == 'completed'}
            if (character_context['project_id'] != source['project_id']
                    or character_context['before_global_idx'] != boundary
                    or set(positions) - known or required - set(positions)):
                raise ValueError('cognition chronology changed')
        ledger = source['ledger']
        if not isinstance(ledger, dict):
            raise ValueError('invalid cognition ledger')
        from app.services.character_cognition import _validate_change_shape, COGNITION_KINDS
        for name, entry in ledger.items():
            if (not isinstance(entry, dict) or set(entry) != {'history'}
                    or not isinstance(entry['history'], list) or not entry['history']):
                raise ValueError('invalid cognition history')
            last = 0
            for item in entry['history']:
                if (not isinstance(item, dict) or set(item) != {'chapter_idx', 'kind', 'fact', 'source_chapter_id',
                        'source_hash', 'evidence', 'source_mode'} or item['kind'] not in COGNITION_KINDS
                        or item['source_mode'] not in {'auto', 'author'}):
                    raise ValueError('invalid cognition observation')
                _validate_change_shape([{'character': name, item['kind']: item['fact'], 'evidence': item['evidence']}])
                cid, index = item['source_chapter_id'], item['chapter_idx']
                # Identity/hash values must equal the already validated
                # chapter witnesses; do not parse them once per observation.
                if (type(index) is not int or index < last or positions.get(cid) != index
                        or snapshot['sources'].get(cid) != item['source_hash']):
                    raise ValueError('cognition observation has no current source')
                last = index
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise PlanningCognitionMaterialError('人物认知原件或来源无法核对，请保留原记录并检查此前正文与认知校订。') from exc
    return source


def cognition_reference(source):
    validate_cognition_source(source)
    return dict(version=1, project_id=source['project_id'], before_global_idx=source['before_global_idx'],
        source_hash=digest(source), snapshot_hash=source['snapshot_hash'], max_chars=COGNITION_CHARS)


def validate_cognition_reference(value):
    if (not isinstance(value, dict) or set(value) != {'version', 'project_id', 'before_global_idx',
            'source_hash', 'snapshot_hash', 'max_chars'} or type(value['version']) is not int or value['version'] != 1
            or type(value['before_global_idx']) is not int or not 0 < value['before_global_idx'] <= 2147483647
            or type(value['max_chars']) is not int or value['max_chars'] != COGNITION_CHARS):
        raise ValueError('invalid cognition reference')
    _identity(value['project_id']); _hash(value['source_hash']); _hash(value['snapshot_hash'])
    return value


def selection_receipt(source, query, selection):
    verify = CognitionSelectionVerifier(source)
    value = dict(version=1, reference_hash=digest(verify.reference), query_hash=digest(query),
        space_id=selection.space_id, ranked_keys=list(selection.ranked_keys), text=selection.text)
    return verify(value, query)


def validate_cognition_selection(value, reference, query, *, source=None):
    """Reproduce the actual selected text without an embedding/model call."""
    if source is not None:
        return CognitionSelectionVerifier(source, reference)(value, query)
    validate_cognition_reference(reference)
    if (not isinstance(value, dict) or set(value) != {'version', 'reference_hash', 'query_hash', 'space_id', 'ranked_keys', 'text'}
            or type(value['version']) is not int or value['version'] != 1
            or value['reference_hash'] != digest(reference) or value['query_hash'] != digest(query)
            or not isinstance(value['text'], str) or len(value['text']) > reference['max_chars']
            or not isinstance(value['ranked_keys'], list) or len(value['ranked_keys']) > RANKED_LIMIT):
        raise ValueError('invalid cognition selection')
    for key in value['ranked_keys']: _hash(key)
    if len(set(value['ranked_keys'])) != len(value['ranked_keys']):
        raise ValueError('duplicate cognition rank')
    if value['space_id'] is None:
        if value['ranked_keys']:
            raise ValueError('unexpected cognition rank')
    else:
        _hash(value['space_id'])
    return value


class CognitionSelectionVerifier:
    """One synchronous validation pass over immutable observation values.

    Never retained between requests or awaits. Every caller reconstructs this
    from its full source, including the current payload and empty witnesses.
    """
    def __init__(self, source, reference=None):
        self.reference = cognition_reference(source)
        if reference is not None:
            validate_cognition_reference(reference)
            if self.reference != reference:
                raise ValueError('cognition source changed')
        self.events = tuple(observations(source['ledger']))
        self.keys = frozenset(event.key for event in self.events)
        self.fits = observations_fit(self.events, self.reference['max_chars'])

    def __call__(self, value, query):
        validate_cognition_selection(value, self.reference, query)
        if value['space_id'] is None and not self.fits:
            raise ValueError('cognition retrieval omitted')
        if not set(value['ranked_keys']).issubset(self.keys):
            raise ValueError('cognition rank from another source')
        try:
            expected = render_observations(self.events, self.reference['max_chars'],
                context_text=query, ranked_keys=value['ranked_keys'])
        except RequiredContextUnavailable as exc:
            # A persisted choice that cannot be reproduced is an invalid
            # record, not a fresh retrieval failure that can escape review.
            raise ValueError('cognition selected text unavailable') from exc
        if value['text'] != expected:
            raise ValueError('cognition selected text changed')
        return value


def append_cognition_material(prompt, text):
    guidance = ('【本轮人物认知依据】\n以下记录只说明相应前章里人物当时的认知；读者知道不等于人物知道。'
        '怀疑、误信保留主观限定，较早未知不能盖过后章获知。未列出不表示人物不知。'
        '本弧可以通过可信的获知过程改变认知，合理省略仍允许，不必逐条复述旧事；本轮计划与失败试稿不能反写前章事实。')
    return prompt + '\n\n' + guidance + '\n' + (text or '前章有效认知记录为空；不能推定人物没有其他所知。')


def render_cognition_archive(source):
    validate_cognition_source(source)
    records = sorted(observations(source['ledger']), key=lambda e: (e.chapter, e.order))
    text = '\n'.join(event.text for event in records) or '没有已记录的认知变化；不能据此推定人物不知道。'
    return ('本次固定的完整认知原件。每轮按当前任务选用完整记录，实际送入模型的内容见该轮输入。'
        '以下只说明各章当时的认知，读者获知、人物获知、未知、误信和怀疑须分别理解。\n' + text)
