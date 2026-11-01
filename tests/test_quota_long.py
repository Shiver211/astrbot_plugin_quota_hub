"""长窗口判定、字段级合并与新增文案变量的单元测试。"""

from __future__ import annotations

import dataclasses

import pytest
from core import quota
from core.models import (
    OUTCOME_BLOCK,
    WINDOW_LONG,
    WINDOW_SHORT,
    Decision,
    Override,
    Quota,
)
from core.quota import ScopeUsage

DAY = 86400.0


def _long_quota(**kwargs) -> Quota:
    """构造一份只关心长窗口的配额（短窗口不限）。"""
    base = {"call_limit": -1, "token_limit": -1, "window_hours": 5.0,
            "long_window_days": 7.0}
    base.update(kwargs)
    return Quota(**base)


# --- ScopeUsage ---------------------------------------------------------


def test_scope_usage_is_frozen_with_long_defaults():
    item = ScopeUsage("group", Quota(), 1, 2, 1000.0)
    assert (item.long_calls, item.long_tokens, item.long_end) == (0, 0, None)
    with pytest.raises(dataclasses.FrozenInstanceError):
        item.calls = 9  # type: ignore[misc]


# --- resolve_quota 新字段 ------------------------------------------------


def test_resolve_quota_merges_long_and_reset_fields():
    defaults = Quota(long_calls=100, long_tokens=200, long_window_days=7.0,
                     self_reset_short=1, self_reset_long=0)
    ov = Override("member", "g", "u", long_calls=5, long_window_days=30.0,
                  self_reset_long=2)
    merged = quota.resolve_quota(defaults, ov)
    assert merged.long_calls == 5  # 覆盖
    assert merged.long_tokens == 200  # None → 回落默认
    assert merged.long_window_days == 30.0
    assert merged.self_reset_short == 1
    assert merged.self_reset_long == 2


def test_resolve_quota_override_can_set_long_unlimited():
    defaults = Quota(long_calls=100, long_tokens=200)
    merged = quota.resolve_quota(defaults, Override("private", "", "u", long_calls=-1))
    assert merged.long_calls == -1


def test_resolve_quota_zero_reset_allowance_is_not_treated_as_none():
    defaults = Quota(self_reset_short=3)
    merged = quota.resolve_quota(defaults, Override("private", "", "u", self_reset_short=0))
    assert merged.self_reset_short == 0


# --- merge_fields --------------------------------------------------------


def test_merge_fields_without_existing_returns_new():
    new = Override("group", "g1", "", long_calls=10)
    assert quota.merge_fields(None, new, ["long_calls"]) is new


def test_merge_fields_copies_only_listed_fields():
    existing = Override("group", "g1", "", call_limit=5, token_limit=50,
                        long_calls=1, long_tokens=2, long_window_days=7.0,
                        self_reset_short=1, self_reset_long=1, enabled=False)
    new = Override("group", "g1", "", call_limit=99, long_calls=8, long_tokens=9,
                   long_window_days=30.0)
    merged = quota.merge_fields(existing, new, ("long_calls", "long_tokens",
                                                "long_window_days"))
    assert (merged.long_calls, merged.long_tokens, merged.long_window_days) == (8, 9, 30.0)
    assert merged.call_limit == 5  # 未列出 → 保留原值（不取 new 的 99）
    assert merged.token_limit == 50
    assert merged.self_reset_short == 1
    assert merged.enabled is False


def test_merge_fields_can_write_none_to_clear_a_field():
    existing = Override("private", "", "u", call_limit=5)
    merged = quota.merge_fields(existing, Override("private", "", "u"), ["call_limit"])
    assert merged.call_limit is None


def test_merge_fields_returns_new_object_and_keeps_existing_intact():
    existing = Override("private", "", "u", call_limit=5)
    merged = quota.merge_fields(existing, Override("private", "", "u", call_limit=7),
                                ["call_limit"])
    assert merged is not existing
    assert existing.call_limit == 5  # 原对象不被修改


# --- merge_override（语义不变）------------------------------------------


def test_merge_override_preserves_long_and_reset_fields():
    existing = Override("member", "g1", "u1", call_limit=5, token_limit=50,
                        window_hours=8.0, long_calls=100, long_tokens=200,
                        long_window_days=30.0, self_reset_short=2, self_reset_long=1)
    new = Override("member", "g1", "u1", call_limit=10, token_limit=100)
    merged = quota.merge_override(existing, new)
    assert merged.call_limit == 10 and merged.token_limit == 100
    assert merged.window_hours == 8.0
    assert merged.long_calls == 100 and merged.long_tokens == 200
    assert merged.long_window_days == 30.0
    assert (merged.self_reset_short, merged.self_reset_long) == (2, 1)


def test_merge_override_clears_limits_when_new_has_none():
    """``call_limit`` / ``token_limit`` 始终以 new 为准，即便是 None。"""
    existing = Override("private", "", "u", call_limit=5, token_limit=50)
    merged = quota.merge_override(existing, Override("private", "", "u"))
    assert merged.call_limit is None and merged.token_limit is None


# --- hard_quota ----------------------------------------------------------


def test_hard_quota_keeps_long_and_reset_fields():
    q = Quota(10, -1, 5, fallback_call_limit=5, long_calls=7, long_tokens=8,
              long_window_days=30.0, self_reset_short=2, self_reset_long=3)
    hard = quota.hard_quota(q)
    assert hard.call_limit == 15
    assert (hard.long_calls, hard.long_tokens) == (7, 8)
    assert hard.long_window_days == 30.0
    assert (hard.self_reset_short, hard.self_reset_long) == (2, 3)


# --- evaluate_long -------------------------------------------------------


def test_evaluate_long_unlimited_never_blocks():
    q = _long_quota(long_calls=-1, long_tokens=-1)
    assert quota.evaluate_long(q, 10_000, 10_000) == (False, None, 0, 0)


def test_evaluate_long_calls_are_a_hard_limit():
    q = _long_quota(long_calls=3)
    assert quota.evaluate_long(q, 2, 0)[0] is False  # 第 3 次允许
    assert quota.evaluate_long(q, 3, 0) == (True, "calls", 3, 3)


def test_evaluate_long_zero_calls_blocks_immediately():
    assert quota.evaluate_long(_long_quota(long_calls=0), 0, 0) == (True, "calls", 0, 0)


def test_evaluate_long_tokens_are_a_soft_limit():
    q = _long_quota(long_tokens=100)
    assert quota.evaluate_long(q, 0, 99)[0] is False
    assert quota.evaluate_long(q, 0, 100) == (True, "tokens", 100, 100)


def test_evaluate_long_calls_checked_before_tokens():
    q = _long_quota(long_calls=1, long_tokens=100)
    assert quota.evaluate_long(q, 1, 999)[1] == "calls"


def test_evaluate_long_disabled_quota_never_blocks():
    q = _long_quota(long_calls=0, long_tokens=0, enabled=False)
    assert quota.evaluate_long(q, 100, 100)[0] is False


def test_evaluate_long_ignores_short_window_usage_fields():
    """长窗口只看长窗口上限，短窗口上限为 0 也不影响。"""
    q = Quota(call_limit=0, token_limit=0, long_calls=-1, long_tokens=-1)
    assert quota.evaluate_long(q, 5, 5)[0] is False


# --- decide_scopes：长窗口 ----------------------------------------------


def test_decide_scopes_long_block_carries_window_metadata():
    q = _long_quota(long_calls=2, long_window_days=7.0)
    item = ScopeUsage("private", q, 0, 0, None, long_calls=2, long_tokens=0,
                      long_end=1000.0 + 7 * DAY)
    d = quota.decide_scopes([item], 1000.0)
    assert d.blocked is True and d.outcome == OUTCOME_BLOCK
    assert d.window == WINDOW_LONG and d.window_days == 7.0
    assert d.window_hours == 5.0
    assert d.scope == "private" and d.metric == "calls"
    assert d.used == 2 and d.limit == 2
    assert d.reset_in == 7 * DAY


def test_decide_scopes_long_reset_in_is_zero_without_cycle():
    q = _long_quota(long_calls=1)
    item = ScopeUsage("private", q, 0, 0, None, long_calls=1, long_end=None)
    assert quota.decide_scopes([item], 1000.0).reset_in == 0.0


def test_decide_scopes_long_reset_in_never_negative():
    q = _long_quota(long_calls=1)
    item = ScopeUsage("private", q, 0, 0, None, long_calls=1, long_end=900.0)
    assert quota.decide_scopes([item], 1000.0).reset_in == 0.0


def test_decide_scopes_long_window_checked_before_short():
    """同一实体长短窗口都超限时，报告长窗口。"""
    q = Quota(call_limit=1, token_limit=-1, window_hours=5.0, long_calls=1,
              long_tokens=-1, long_window_days=7.0)
    item = ScopeUsage("private", q, 5, 0, 1000.0, long_calls=5,
                      long_end=1000.0 + 7 * DAY)
    d = quota.decide_scopes([item], 1000.0)
    assert d.window == WINDOW_LONG and d.limit == 1 and d.used == 5


def test_decide_scopes_long_of_later_scope_beats_short_of_earlier_scope():
    """长窗口整体先于短窗口：成员的长窗口超限压过群的短窗口超限。"""
    grp = Quota(call_limit=1, token_limit=-1, window_hours=5.0, long_calls=-1,
                long_tokens=-1)
    mem = _long_quota(long_calls=1, long_window_days=30.0)
    items = [
        ScopeUsage("group", grp, 5, 0, 1000.0),
        ScopeUsage("member", mem, 0, 0, None, long_calls=1,
                   long_end=1000.0 + 30 * DAY),
    ]
    d = quota.decide_scopes(items, 1000.0)
    assert d.scope == "member" and d.window == WINDOW_LONG
    assert d.window_days == 30.0


def test_decide_scopes_group_long_wins_over_member_long():
    grp = _long_quota(long_calls=1)
    mem = _long_quota(long_calls=1)
    items = [
        ScopeUsage("group", grp, 0, 0, None, long_calls=9, long_end=2000.0),
        ScopeUsage("member", mem, 0, 0, None, long_calls=9, long_end=2000.0),
    ]
    assert quota.decide_scopes(items, 1000.0).scope == "group"


def test_decide_scopes_long_block_ignores_fallback():
    """长窗口耗尽永远拦截，即便回退配置有效。"""
    q = _long_quota(long_calls=1, fallback_provider_id="p2", fallback_call_limit=100,
                    fallback_token_limit=100)
    item = ScopeUsage("member", q, 0, 0, None, long_calls=1, long_end=2000.0)
    d = quota.decide_scopes([item], 1000.0)
    assert d.blocked is True and d.outcome == OUTCOME_BLOCK
    assert d.fallback_provider_id == ""


def test_decide_scopes_long_block_preempts_short_fallback():
    q = Quota(call_limit=1, token_limit=-1, window_hours=5.0,
              fallback_provider_id="p2", fallback_call_limit=10,
              long_calls=1, long_tokens=-1, long_window_days=7.0)
    item = ScopeUsage("member", q, 1, 0, 1000.0, long_calls=1, long_end=2000.0)
    d = quota.decide_scopes([item], 1000.0)
    assert d.blocked is True and d.window == WINDOW_LONG


def test_decide_scopes_long_unlimited_falls_through_to_short():
    q = Quota(call_limit=1, token_limit=-1, window_hours=5.0, long_calls=-1,
              long_tokens=-1)
    item = ScopeUsage("member", q, 1, 0, 1000.0, long_calls=999, long_tokens=999,
                      long_end=2000.0)
    d = quota.decide_scopes([item], 1000.0)
    assert d.blocked is True and d.window == WINDOW_SHORT


def test_decide_scopes_disabled_scope_skips_long_check():
    q = _long_quota(long_calls=0, enabled=False)
    item = ScopeUsage("group", q, 0, 0, None, long_calls=5, long_end=2000.0)
    assert quota.decide_scopes([item], 1000.0).blocked is False


def test_decide_scopes_long_tokens_block():
    q = _long_quota(long_tokens=500, long_window_days=1.0)
    item = ScopeUsage("private", q, 0, 0, None, long_tokens=500, long_end=2000.0)
    d = quota.decide_scopes([item], 1000.0)
    assert d.blocked and d.metric == "tokens" and d.used == 500 and d.limit == 500
    assert d.window_days == 1.0


def test_decide_scopes_clear_result_defaults_to_short_window():
    item = ScopeUsage("private", _long_quota(long_calls=10), 0, 0, None, long_calls=1,
                      long_end=2000.0)
    d = quota.decide_scopes([item], 1000.0)
    assert d.blocked is False and d.window == WINDOW_SHORT


# --- format_block_message：新增变量 -------------------------------------


def test_format_block_message_short_window_label():
    d = Decision(blocked=True, scope="member", metric="calls", used=3, limit=3,
                 window_hours=5.0, reset_in=60, window=WINDOW_SHORT)
    msg = quota.format_block_message("{window_label} {window_days}", d)
    assert msg.startswith("短窗口(5小时)")


def test_format_block_message_long_window_label_and_days():
    d = Decision(blocked=True, scope="private", metric="calls", used=3, limit=3,
                 window_hours=5.0, reset_in=DAY, window=WINDOW_LONG, window_days=7.0)
    msg = quota.format_block_message("{window_label} {window_days} {reset_in}", d)
    # format_duration 的最大单位是天（≥24 小时进位），一天渲染为 1天
    assert msg == "长窗口(7天) 7 1天"


def test_format_block_message_keeps_fractional_days():
    d = Decision(blocked=True, window=WINDOW_LONG, window_days=1.5, window_hours=2.5)
    msg = quota.format_block_message("{window_label}|{window_days}|{window_hours}", d)
    assert msg == "长窗口(1.5天)|1.5|2.5"


def test_format_block_message_fallback_text_includes_window_label():
    d = Decision(blocked=True, scope="group", metric="tokens", used=5, limit=5,
                 window=WINDOW_LONG, window_days=7.0)
    msg = quota.format_block_message("{nonexistent_var}", d)
    assert "长窗口(7天)" in msg
    assert "5/5" in msg


def test_default_style_template_renders_with_long_decision():
    template = ("⚠️ 已达使用上限（{scope_label} {window_label} {metric_label}："
                "{used}/{limit}），约 {reset_in} 后恢复。")
    d = Decision(blocked=True, scope="member", metric="calls", used=10, limit=10,
                 window=WINDOW_LONG, window_days=7.0, reset_in=3600)
    msg = quota.format_block_message(template, d)
    assert "你在本群 长窗口(7天) 调用次数：10/10" in msg
    assert "约 1小时 后恢复" in msg
