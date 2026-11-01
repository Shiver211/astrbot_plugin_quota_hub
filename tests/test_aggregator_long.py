"""聚合器的重置标记与长窗口状态单元测试（显式时间戳，不依赖真实时钟）。"""

from __future__ import annotations

from core.aggregator import Aggregator
from core.models import ScopeKey, scopes_for

GROUP = ScopeKey("group", "g1", "")
MEMBER = ScopeKey("member", "g1", "u1")
PRIVATE = ScopeKey("private", "", "u1")

DAY = 86400.0


# --- 重置标记 -----------------------------------------------------------


def test_marker_defaults_to_zero_and_does_not_create_bucket():
    agg = Aggregator()
    assert agg.marker(PRIVATE) == 0.0
    agg.set_marker(PRIVATE, 1000.0)
    assert agg.marker(PRIVATE) == 1000.0
    assert agg.bucket_count() == 0


def test_set_marker_evicts_older_records_from_usage():
    agg = Aggregator()
    agg.record_call(scopes_for("", "u1"), now=1000)
    agg.record_call(scopes_for("", "u1"), now=2000)
    assert agg.usage(PRIVATE, 2000, 5)[0] == 2
    agg.set_marker(PRIVATE, 1500.0)
    # 标记之前的记录不再计入，标记之后的保留
    assert agg.usage(PRIVATE, 2000, 5) == (1, 0, 2000)


def test_record_at_marker_timestamp_is_kept():
    """驱逐条件是 ``ts < marker``，恰好等于标记的记录仍然计入。"""
    agg = Aggregator()
    agg.record_call(scopes_for("", "u1"), now=1500)
    agg.set_marker(PRIVATE, 1500.0)
    assert agg.usage(PRIVATE, 1500, 5)[0] == 1


def test_late_tokens_for_marked_records_are_not_counted():
    agg = Aggregator()
    eid = agg.record_call(scopes_for("", "u1"), now=1000)
    agg.set_marker(PRIVATE, 1500.0)
    agg.add_tokens(eid, 500)  # 迟到回填：记录已被标记驱逐
    assert agg.usage(PRIVATE, 1600, 5) == (0, 0, None)


def test_usage_cutoff_takes_max_of_window_and_marker():
    """标记早于窗口截止时，仍以窗口截止为准（标记不会把过期记录拉回来）。"""
    agg = Aggregator()
    agg.record_call(scopes_for("", "u1"), now=1000)
    agg.set_marker(PRIVATE, 500.0)
    assert agg.usage(PRIVATE, 1000 + 18001, 5) == (0, 0, None)


def test_add_events_skips_records_before_marker():
    agg = Aggregator()
    agg.set_marker(GROUP, 1500.0)
    agg.add_events([("g1", "u1", 1000, 40), ("g1", "u1", 2000, 10)])
    # group 有标记 → 只收 2000 那条；member 无标记 → 两条都收
    assert agg.usage(GROUP, 2000, 5) == (1, 10, 2000)
    assert agg.usage(MEMBER, 2000, 5) == (2, 50, 1000)


def test_rebuild_after_sweep_still_respects_marker():
    """桶被 sweep 删除后标记不丢失，rebuild 不会把标记前的记录加回来。"""
    agg = Aggregator()
    agg.record_call(scopes_for("", "u1"), now=1000)
    agg.set_marker(PRIVATE, 1500.0)
    agg.sweep(now=2000, max_window_hours=5)  # 桶已空 → 被删除
    assert agg.bucket_count() == 0
    agg.rebuild([("", "u1", 1000, 40), ("", "u1", 1800, 7)])
    assert agg.usage(PRIVATE, 2000, 5) == (1, 7, 1800)
    assert agg.marker(PRIVATE) == 1500.0


def test_sweep_drops_markers_older_than_window_cutoff():
    agg = Aggregator()
    agg.set_marker(PRIVATE, 1000.0)
    agg.sweep(now=1000 + 18000, max_window_hours=5)  # 标记恰在截止线上 → 保留
    assert agg.marker(PRIVATE) == 1000.0
    agg.sweep(now=1000 + 18001, max_window_hours=5)  # 标记早于截止线 → 丢弃
    assert agg.marker(PRIVATE) == 0.0


def test_reset_key_and_drops_clear_markers():
    agg = Aggregator()
    agg.set_marker(PRIVATE, 1000.0)
    agg.set_marker(GROUP, 1000.0)
    agg.set_marker(MEMBER, 1000.0)
    agg.reset_key(PRIVATE)
    assert agg.marker(PRIVATE) == 0.0
    agg.drop_group("g1")
    assert agg.marker(GROUP) == 0.0
    assert agg.marker(MEMBER) == 0.0


def test_drop_private_clears_marker():
    agg = Aggregator()
    agg.set_marker(PRIVATE, 1000.0)
    agg.drop_private("u1")
    assert agg.marker(PRIVATE) == 0.0


# --- 长窗口状态 ---------------------------------------------------------


def test_long_usage_without_state_is_empty():
    agg = Aggregator()
    assert agg.long_usage(PRIVATE) == (0, 0)
    assert agg.long_start(PRIVATE) is None


def test_open_long_starts_zeroed_cycle():
    agg = Aggregator()
    agg.open_long(PRIVATE, 1000.0)
    assert agg.long_start(PRIVATE) == 1000.0
    assert agg.long_usage(PRIVATE) == (0, 0)


def test_open_long_again_resets_counters():
    agg = Aggregator()
    agg.restore_long(PRIVATE, 1000.0, 5, 500)
    agg.open_long(PRIVATE, 9000.0)
    assert agg.long_usage(PRIVATE) == (0, 0)
    assert agg.long_start(PRIVATE) == 9000.0


def test_record_call_counts_only_keys_with_open_cycle():
    agg = Aggregator()
    agg.open_long(GROUP, 1000.0)
    agg.record_call(scopes_for("g1", "u1"), now=1000)
    assert agg.long_usage(GROUP) == (1, 0)
    assert agg.long_usage(MEMBER) == (0, 0)  # 未开窗 → 不建状态
    assert agg.long_start(MEMBER) is None


def test_group_call_feeds_both_long_states():
    agg = Aggregator()
    agg.open_long(GROUP, 1000.0)
    agg.open_long(MEMBER, 1000.0)
    eid = agg.record_call(scopes_for("g1", "u1"), now=1000)
    agg.add_tokens(eid, 80)
    assert agg.long_usage(GROUP) == (1, 80)
    assert agg.long_usage(MEMBER) == (1, 80)


def test_long_tokens_only_backfill_records_at_or_after_start():
    agg = Aggregator()
    eid_before = agg.record_call(scopes_for("", "u1"), now=900)
    agg.open_long(PRIVATE, 1000.0)
    eid_after = agg.record_call(scopes_for("", "u1"), now=1000)
    agg.add_tokens(eid_before, 40)  # 开窗前的调用 → 不计入长窗口
    agg.add_tokens(eid_after, 60)
    assert agg.long_usage(PRIVATE) == (1, 60)


def test_close_long_stops_backfill_and_clears_state():
    agg = Aggregator()
    agg.open_long(PRIVATE, 1000.0)
    eid = agg.record_call(scopes_for("", "u1"), now=1000)
    agg.close_long(PRIVATE)
    agg.add_tokens(eid, 50)
    assert agg.long_usage(PRIVATE) == (0, 0)
    assert agg.long_start(PRIVATE) is None


def test_close_long_unknown_key_is_noop():
    agg = Aggregator()
    agg.close_long(PRIVATE)  # 不应抛错
    assert agg.long_start(PRIVATE) is None


def test_restore_long_then_record_call_accumulates():
    agg = Aggregator()
    agg.restore_long(PRIVATE, 1000.0, calls=3, tokens=300)
    eid = agg.record_call(scopes_for("", "u1"), now=2000)
    agg.add_tokens(eid, 50)
    assert agg.long_usage(PRIVATE) == (4, 350)
    assert agg.long_start(PRIVATE) == 1000.0


def test_long_tokens_count_even_when_short_marker_evicted_record():
    """短窗口重置不影响长窗口累计：迟到 token 仍计入长窗口。"""
    agg = Aggregator()
    agg.open_long(PRIVATE, 1000.0)
    eid = agg.record_call(scopes_for("", "u1"), now=1000)
    agg.set_marker(PRIVATE, 1500.0)
    agg.add_tokens(eid, 50)
    assert agg.usage(PRIVATE, 1600, 5) == (0, 0, None)
    assert agg.long_usage(PRIVATE) == (1, 50)


def test_add_events_does_not_touch_long_counters():
    agg = Aggregator()
    agg.open_long(GROUP, 1000.0)
    agg.add_events([("g1", "u1", 1200, 40)])
    assert agg.long_usage(GROUP) == (0, 0)


def test_rebuild_keeps_long_state():
    agg = Aggregator()
    agg.restore_long(GROUP, 1000.0, 2, 200)
    agg.rebuild([("g1", "u1", 1200, 40)])
    assert agg.long_usage(GROUP) == (2, 200)
    assert agg.long_start(GROUP) == 1000.0


def test_sweep_keeps_long_state_even_when_bucket_removed():
    agg = Aggregator()
    agg.open_long(PRIVATE, 1000.0)
    agg.record_call(scopes_for("", "u1"), now=1000)
    agg.sweep(now=1000 + 10**6, max_window_hours=5)
    assert agg.bucket_count() == 0
    assert agg.long_usage(PRIVATE) == (1, 0)
    assert agg.long_start(PRIVATE) == 1000.0


def test_reset_key_and_drops_clear_long_state():
    agg = Aggregator()
    agg.open_long(PRIVATE, 1000.0)
    agg.open_long(GROUP, 1000.0)
    agg.open_long(MEMBER, 1000.0)
    agg.reset_key(PRIVATE)
    assert agg.long_start(PRIVATE) is None
    agg.drop_group("g1")
    assert agg.long_start(GROUP) is None
    assert agg.long_start(MEMBER) is None


def test_drop_private_clears_long_state():
    agg = Aggregator()
    agg.open_long(PRIVATE, 1000.0)
    agg.drop_private("u1")
    assert agg.long_start(PRIVATE) is None


def test_stale_pending_tokens_do_not_reach_long_window():
    agg = Aggregator()
    agg.open_long(PRIVATE, 1000.0)
    eid = agg.record_call(scopes_for("", "u1"), now=1000)
    agg.sweep(now=1000 + 700, max_window_hours=100)  # pending 超时清理
    agg.add_tokens(eid, 50)
    assert agg.long_usage(PRIVATE) == (1, 0)


def test_long_window_spans_days_of_records():
    agg = Aggregator()
    agg.open_long(PRIVATE, 1000.0)
    for i in range(3):
        eid = agg.record_call(scopes_for("", "u1"), now=1000 + i * DAY)
        agg.add_tokens(eid, 10)
    # 短窗口只剩最近一次（更早的已按 5 小时驱逐），长窗口仍累计三次
    assert agg.usage(PRIVATE, 1000 + 2 * DAY, 5)[0] == 1
    assert agg.usage(PRIVATE, 1000 + 3 * DAY, 5) == (0, 0, None)
    assert agg.long_usage(PRIVATE) == (3, 30)


def test_long_count_tracks_open_cycles():
    agg = Aggregator()
    assert agg.long_count() == 0
    agg.open_long(GROUP, 1000.0)
    agg.open_long(MEMBER, 1000.0)
    assert agg.long_count() == 2
    agg.close_long(MEMBER)
    assert agg.long_count() == 1
