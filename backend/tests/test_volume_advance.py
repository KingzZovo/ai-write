"""自动续卷:一卷跑完后必须有人起下一卷。

背景(实测 2026-11-27):项目约束「同一作品同时只能有一个进行中的整书任务」,
所以百万字长篇必须逐卷串行。此前续卷靠**外部**调度器每小时检查,而那个任务
`lastRunAt` 停在 11-21、`nextRunAt` 一直往后滚 —— 从未触发。结果卷 2 跑完
(100/100、27 万字)后链条静默停住,卷 3–5 一个字没写,日志里也没有任何错误。

修法:把续卷放进项目自己的 beat(`tasks.advance_volumes`,每 60s,走 recovery
队列),只在 `settings_json.auto_advance_volumes=true` 时生效。
"""
from types import SimpleNamespace as NS

import pytest

from app.services.volume_advance import _enabled, first_unfinished_volume


def _row(idx, total, done):
    return NS(volume_id=f'v{idx}', volume_idx=idx, total=total, done=done)


def test_picks_lowest_volume_with_missing_body():
    rows = [_row(1, 100, 100), _row(2, 100, 37), _row(3, 100, 0)]
    assert first_unfinished_volume(rows).volume_idx == 2


def test_returns_none_when_every_volume_is_complete():
    rows = [_row(1, 100, 100), _row(2, 100, 100)]
    assert first_unfinished_volume(rows) is None


def test_ignores_empty_volume_as_target():
    """没有章节行的卷不是"未完成",不能拿它当目标(会起一个 0 章的任务)。"""
    rows = [_row(1, 100, 100), _row(2, 0, 0), _row(3, 100, 0)]
    assert first_unfinished_volume(rows).volume_idx == 3


def test_partially_written_volume_is_resumed_before_later_ones():
    rows = [_row(1, 100, 100), _row(2, 100, 100), _row(3, 100, 12), _row(4, 100, 0)]
    assert first_unfinished_volume(rows).volume_idx == 3


@pytest.mark.parametrize('settings,expected', [
    ({'auto_advance_volumes': True}, True),
    ({'auto_advance_volumes': 'true'}, True),
    ({'auto_advance_volumes': 'TRUE'}, True),
    ({'auto_advance_volumes': False}, False),
    ({'auto_advance_volumes': 'no'}, False),
    ({}, False),
    (None, False),
    ('not-a-dict', False),
])
def test_switch_is_opt_in(settings, expected):
    """自动续卷会持续花钱调模型,必须显式打开。"""
    assert _enabled(settings) is expected


def test_task_is_scheduled_and_routed_to_recovery():
    """任务必须在 beat 里,且不能挤在会被长任务饿死的默认队列上。"""
    from app.tasks import celery_app

    assert 'tasks.advance_volumes' in celery_app.tasks
    schedule = celery_app.conf.beat_schedule
    assert any(entry['task'] == 'tasks.advance_volumes' for entry in schedule.values())
    assert celery_app.conf.task_routes['tasks.advance_volumes']['queue'] == 'recovery'