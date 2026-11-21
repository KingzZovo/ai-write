"""回归:迁移记账的判定逻辑(纯函数,不连库)。

要证的四件事:
1. 有多个 head 就**拒绝**判断(先修分支),绝不猜;
2. head 没登记核对痕迹时**拒绝**自动判断(交给人);
3. schema 痕迹齐全 + 记账落后/缺失 → `stamp`(只补记账);
4. 痕迹缺失 → `upgrade`(真的没应用,要跑 DDL)。
"""
from __future__ import annotations

from scripts.ops.alembic_sync import SCHEMA_PROBES, decide


def test_single_head_is_required():
    v = decide(heads=["a1", "a2"], current=None, probes_ok=True)
    assert v.action == "fix_heads" and "2 个 head" in v.reason


def test_no_head_is_refused():
    assert decide(heads=[], current=None, probes_ok=None).action == "fix_heads"


def test_unregistered_head_is_manual():
    v = decide(heads=["zzz"], current=None, probes_ok=None)
    assert v.action == "manual"
    assert "没有登记核对痕迹" in v.reason


def test_probes_ok_and_missing_bookkeeping_wants_stamp():
    v = decide(heads=["a1001959"], current=None, probes_ok=True)
    assert v.action == "stamp"
    assert v.command("a1001959") == "alembic stamp a1001959"


def test_probes_ok_and_current_equals_head_is_noop():
    assert decide(heads=["a1001959"], current="a1001959", probes_ok=True).action == "noop"


def test_probes_missing_wants_upgrade():
    v = decide(heads=["a1001959"], current="a1001958", probes_ok=False)
    assert v.action == "upgrade"
    assert v.command("a1001959") == "alembic upgrade a1001959"


def test_current_head_has_a_registered_probe():
    """head 如果没有核对痕迹,工具会永远拒绝自动判断 —— 所以新迁移必须补一行。"""
    from scripts.ops.alembic_sync import _script_directory

    head = _script_directory().get_heads()[0]
    assert head in SCHEMA_PROBES, f"新 head {head} 需要登记 SCHEMA_PROBES"
