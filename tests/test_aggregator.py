"""内存滚动窗口聚合器的单元测试（用显式时间戳，不依赖真实时钟）。"""

from __future__ import annotations

from core.aggregator import Aggregator
from core.models import ScopeKey, scopes_for

GROUP = ScopeKey("group", "g1", "")
MEMBER = ScopeKey("member", "g1", "u1")
PRIVATE = ScopeKey("private", "", "u1")


def test_record_call_populates_group_and_member_not_private():
    agg = Aggregator()
    agg.record_call(scopes_for("g1", "u1"), now=1000)
    assert agg.usage(GROUP, 1000, 5) == (1, 0, 1000)
    assert agg.usage(MEMBER, 1000, 5) == (1, 0, 1000)
    assert agg.usage(PRIVATE, 1000, 5) == (0, 0, None)


def test_private_message_only_touches_private_scope():
    agg = Aggregator()
    agg.record_call(scopes_for("", "u1"), now=1000)
    assert agg.usage(PRIVATE, 1000, 5) == (1, 0, 1000)
    assert agg.usage(GROUP, 1000, 5) == (0, 0, None)


def test_usage_unknown_key_is_empty_and_does_not_create_bucket():
    agg = Aggregator()
    assert agg.usage(GROUP, 1000, 5) == (0, 0, None)
    assert agg.bucket_count() == 0


def test_add_tokens_updates_all_recorded_scopes():
    agg = Aggregator()
    eid = agg.record_call(scopes_for("g1", "u1"), now=1000)
    agg.add_tokens(eid, 50)
    assert agg.usage(GROUP, 1000, 5) == (1, 50, 1000)
    assert agg.usage(MEMBER, 1000, 5) == (1, 50, 1000)


def test_add_tokens_unknown_eid_is_noop():
    agg = Aggregator()
    agg.add_tokens(99999, 50)  # 不应抛错
    assert agg.bucket_count() == 0


def test_rolling_eviction_drops_aged_records():
    agg = Aggregator()
    agg.record_call(scopes_for("g1", "u1"), now=1000)
    # 5 小时窗口 = 18000 秒；超出后归零
    assert agg.usage(GROUP, 1000 + 18001, 5) == (0, 0, None)
    # 恰好在窗口边界仍保留
    assert agg.usage(MEMBER, 1000 + 18000, 5)[0] == 1


def test_window_shrink_evicts_more():
    agg = Aggregator()
    agg.record_call(scopes_for("", "u1"), now=1000)
    agg.record_call(scopes_for("", "u1"), now=1000 + 3600)
    assert agg.usage(PRIVATE, 1000 + 3600, 5)[0] == 2
    # 0.5 小时窗口只剩最近一次
    assert agg.usage(PRIVATE, 1000 + 3600, 0.5)[0] == 1


def test_reset_key_clears_bucket():
    agg = Aggregator()
    agg.record_call(scopes_for("", "u1"), now=1000)
    agg.reset_key(PRIVATE)
    assert agg.usage(PRIVATE, 1000, 5) == (0, 0, None)


def test_drop_group_removes_group_and_members_only():
    agg = Aggregator()
    agg.record_call(scopes_for("g1", "u1"), now=1000)
    agg.record_call(scopes_for("g1", "u2"), now=1000)
    agg.record_call(scopes_for("g2", "u3"), now=1000)
    agg.record_call(scopes_for("", "u1"), now=1000)
    agg.drop_group("g1")
    assert agg.usage(GROUP, 1000, 5) == (0, 0, None)
    assert agg.usage(MEMBER, 1000, 5) == (0, 0, None)
    assert agg.usage(ScopeKey("member", "g1", "u2"), 1000, 5) == (0, 0, None)
    assert agg.usage(ScopeKey("group", "g2", ""), 1000, 5)[0] == 1
    assert agg.usage(PRIVATE, 1000, 5)[0] == 1


def test_add_events_rebuilds_scopes_with_tokens():
    agg = Aggregator()
    agg.add_events([("g1", "u1", 1000, 40), ("", "u2", 1001, 10)])
    assert agg.usage(GROUP, 1100, 5) == (1, 40, 1000)
    assert agg.usage(MEMBER, 1100, 5) == (1, 40, 1000)
    assert agg.usage(ScopeKey("private", "", "u2"), 1100, 5) == (1, 10, 1001)


def test_sweep_removes_empty_buckets():
    agg = Aggregator()
    agg.record_call(scopes_for("g1", "u1"), now=1000)
    agg.sweep(now=1000 + 10**9, max_window_hours=5)
    assert agg.bucket_count() == 0


def test_sweep_purges_stale_pending_so_late_tokens_are_dropped():
    agg = Aggregator()
    eid = agg.record_call(scopes_for("", "u1"), now=1000)
    # 窗口很大 → 桶保留；但 pending 超时（>600s）被清理
    agg.sweep(now=1000 + 700, max_window_hours=100)
    agg.add_tokens(eid, 50)
    assert agg.usage(PRIVATE, 1700, 100)[1] == 0
    assert agg.pending_count() == 0


def test_rebuild_restores_evicted_usage():
    """窗口调大后 rebuild 应回填此前按小窗口驱逐掉的用量。"""
    agg = Aggregator()
    agg.add_events([("g1", "u1", 1000, 40)])
    # 以 1h 窗口查询触发驱逐（记录早于 cutoff），随后调大窗口也查不回来
    assert agg.usage(GROUP, 1000 + 7200, 1) == (0, 0, None)
    assert agg.usage(GROUP, 1000 + 7200, 5) == (0, 0, None)
    agg.rebuild([("g1", "u1", 1000, 40)])
    assert agg.usage(GROUP, 1000 + 7200, 5) == (1, 40, 1000)
    assert agg.usage(MEMBER, 1000 + 7200, 5) == (1, 40, 1000)
