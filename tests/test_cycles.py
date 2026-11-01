"""周期状态机（长窗口 / 重置标记 / 重置次数）的单元测试。"""

from __future__ import annotations

import dataclasses

import pytest
from core import cycles
from core.cycles import CycleState, SelfResetRefusal
from core.models import WINDOW_LONG, WINDOW_SHORT
from core.windows import SECONDS_PER_DAY

T0 = 1_000_000.0
DAYS = 7.0
WEEK = DAYS * SECONDS_PER_DAY


def _running(**kwargs) -> CycleState:
    """一个进行中的周期：开窗于 T0，短 1 次、长 2 次重置额度。"""
    base = CycleState(
        window_start=T0,
        short_marker=0.0,
        short_allowance=1,
        long_allowance=2,
        closed_by_reset=False,
    )
    return dataclasses.replace(base, **kwargs)


# --- initial_state / cycle_end / is_active -------------------------------


def test_initial_state_has_no_cycle_and_full_allowance():
    state = cycles.initial_state(3, 1)
    assert state.window_start is None
    assert state.short_marker == 0.0
    assert state.short_allowance == 3
    assert state.long_allowance == 1
    assert state.closed_by_reset is False


def test_cycle_end_is_none_without_cycle():
    assert cycles.cycle_end(cycles.initial_state(1, 0), DAYS) is None


def test_cycle_end_uses_current_days_value():
    state = _running()
    assert cycles.cycle_end(state, DAYS) == T0 + WEEK
    # 配置改动即时影响到期时刻
    assert cycles.cycle_end(state, 1.0) == T0 + SECONDS_PER_DAY


def test_is_active_true_before_end_false_at_and_after_end():
    state = _running()
    assert cycles.is_active(state, T0, DAYS) is True
    assert cycles.is_active(state, T0 + WEEK - 1, DAYS) is True
    assert cycles.is_active(state, T0 + WEEK, DAYS) is False  # 到期时刻即不再进行中
    assert cycles.is_active(state, T0 + WEEK + 1, DAYS) is False


def test_is_active_false_without_cycle():
    assert cycles.is_active(cycles.initial_state(1, 0), T0, DAYS) is False


# --- expire_if_due -------------------------------------------------------


def test_expire_if_due_not_due_returns_same_object():
    state = _running(short_allowance=0)
    new, expired = cycles.expire_if_due(state, T0 + 10, DAYS, 1, 2)
    assert expired is False
    assert new is state  # 未到期不构造新对象


def test_expire_if_due_without_cycle_returns_same_object():
    state = cycles.initial_state(1, 0)
    new, expired = cycles.expire_if_due(state, T0 + 10 * WEEK, DAYS, 1, 0)
    assert expired is False
    assert new is state


def test_expire_if_due_refills_allowance_to_config_value():
    state = _running(short_allowance=0, long_allowance=0)
    new, expired = cycles.expire_if_due(state, T0 + WEEK, DAYS, 2, 1)
    assert expired is True
    assert new.window_start is None
    assert new.closed_by_reset is False
    assert new.short_allowance == 2
    assert new.long_allowance == 1
    assert state.short_allowance == 0  # 原对象未被修改


def test_expire_if_due_drops_granted_extra_allowance():
    """自然到期是「赋值为配置值」，发放过的额外次数因此被清掉。"""
    state = cycles.grant(_running(), 5, 5)
    assert (state.short_allowance, state.long_allowance) == (6, 7)
    new, expired = cycles.expire_if_due(state, T0 + WEEK, DAYS, 1, 0)
    assert expired is True
    assert (new.short_allowance, new.long_allowance) == (1, 0)


def test_expire_if_due_keeps_short_marker():
    state = _running(short_marker=T0 + 5)
    new, _ = cycles.expire_if_due(state, T0 + WEEK, DAYS, 1, 0)
    assert new.short_marker == T0 + 5


def test_expire_if_due_clears_closed_by_reset_flag():
    """自然到期后 closed_by_reset 必须归位，否则下个周期会误判为「重置关闭」。"""
    state = _running(closed_by_reset=True)
    new, expired = cycles.expire_if_due(state, T0 + WEEK, DAYS, 1, 0)
    assert expired is True
    assert new.closed_by_reset is False


def test_expire_if_due_follows_shortened_days_config():
    state = _running()
    new, expired = cycles.expire_if_due(state, T0 + 2 * SECONDS_PER_DAY, 1.0, 1, 0)
    assert expired is True
    assert new.window_start is None


# --- open_cycle ----------------------------------------------------------


def test_open_cycle_sets_start_and_keeps_allowance():
    state = dataclasses.replace(
        cycles.initial_state(3, 2), short_allowance=0, long_allowance=1
    )
    new = cycles.open_cycle(state, T0)
    assert new.window_start == T0
    assert new.closed_by_reset is False
    assert new.short_allowance == 0  # open_cycle 不动次数
    assert new.long_allowance == 1


def test_cycle_closed_by_reset_does_not_refill_on_reopen():
    """由重置关闭的周期再开窗不补满，沿用剩余值。"""
    state = _running(short_allowance=1, long_allowance=1)
    after_reset = cycles.consume_allowance(
        cycles.apply_reset(state, WINDOW_LONG, T0 + 60), WINDOW_LONG
    )
    assert after_reset.closed_by_reset is True
    assert after_reset.long_allowance == 0
    # 关窗后即使时间已越过原到期时刻，也不会触发补满
    touched, expired = cycles.expire_if_due(after_reset, T0 + 2 * WEEK, DAYS, 1, 1)
    assert expired is False
    reopened = cycles.open_cycle(touched, T0 + 2 * WEEK)
    assert reopened.long_allowance == 0
    assert reopened.short_allowance == 1
    assert reopened.closed_by_reset is False


def test_open_cycle_preserves_short_marker():
    state = dataclasses.replace(cycles.initial_state(1, 0), short_marker=T0)
    assert cycles.open_cycle(state, T0 + 10).short_marker == T0


# --- apply_reset ---------------------------------------------------------


def test_apply_reset_short_only_writes_marker():
    state = _running()
    new = cycles.apply_reset(state, WINDOW_SHORT, T0 + 100)
    assert new.short_marker == T0 + 100
    assert new.window_start == T0  # 周期不受短重置影响
    assert new.closed_by_reset is False
    assert (new.short_allowance, new.long_allowance) == (1, 2)  # 不动次数


def test_apply_reset_long_closes_cycle_and_flags_it():
    state = _running()
    new = cycles.apply_reset(state, WINDOW_LONG, T0 + 100)
    assert new.window_start is None
    assert new.closed_by_reset is True
    assert new.short_marker == 0.0  # 长重置不写短标记
    assert (new.short_allowance, new.long_allowance) == (1, 2)


def test_apply_reset_does_not_mutate_original():
    state = _running()
    cycles.apply_reset(state, WINDOW_LONG, T0 + 100)
    assert state.window_start == T0
    assert state.closed_by_reset is False


def test_apply_reset_rejects_unknown_window():
    with pytest.raises(ValueError):
        cycles.apply_reset(_running(), "weekly", T0)


# --- consume_allowance / grant / allowance -------------------------------


def test_consume_allowance_decrements_only_target_window():
    state = _running()
    assert cycles.consume_allowance(state, WINDOW_SHORT) == _running(short_allowance=0)
    assert cycles.consume_allowance(state, WINDOW_LONG) == _running(long_allowance=1)


def test_consume_allowance_does_not_go_below_zero():
    state = _running(short_allowance=0)
    assert cycles.consume_allowance(state, WINDOW_SHORT).short_allowance == 0


def test_consume_allowance_rejects_unknown_window():
    with pytest.raises(ValueError):
        cycles.consume_allowance(_running(), "")


def test_grant_adds_to_both_windows_and_allows_zero():
    state = _running()
    assert cycles.grant(state, 2, 3) == _running(short_allowance=3, long_allowance=5)
    assert cycles.grant(state, 0, 0) == state
    assert state.short_allowance == 1  # 未被修改


def test_grant_rejects_negative_amounts():
    with pytest.raises(ValueError):
        cycles.grant(_running(), -1, 0)


def test_allowance_reads_target_window():
    state = _running()
    assert cycles.allowance(state, WINDOW_SHORT) == 1
    assert cycles.allowance(state, WINDOW_LONG) == 2
    with pytest.raises(ValueError):
        cycles.allowance(state, "nope")


# --- check_self_reset ----------------------------------------------------


def test_check_self_reset_allows_short_with_usage_and_allowance():
    state = _running()
    assert (
        cycles.check_self_reset(
            state, WINDOW_SHORT, limited=True, has_short_usage=True, days=DAYS, now=T0
        )
        is None
    )


def test_check_self_reset_allows_long_with_active_cycle():
    state = _running()
    assert (
        cycles.check_self_reset(
            state, WINDOW_LONG, limited=True, has_short_usage=False, days=DAYS, now=T0
        )
        is None
    )


def test_check_self_reset_not_limited_takes_priority():
    """不受限流约束优先返回，即使还有别的问题。"""
    state = _running(short_allowance=0)
    assert (
        cycles.check_self_reset(
            state, WINDOW_SHORT, limited=False, has_short_usage=False, days=DAYS, now=T0
        )
        is SelfResetRefusal.NOT_LIMITED
    )


def test_check_self_reset_short_no_usage_before_allowance():
    state = _running(short_allowance=0)
    assert (
        cycles.check_self_reset(
            state, WINDOW_SHORT, limited=True, has_short_usage=False, days=DAYS, now=T0
        )
        is SelfResetRefusal.NO_USAGE
    )


def test_check_self_reset_long_ignores_short_usage_flag():
    """长窗口不看短窗口用量，只看周期是否进行中。"""
    state = _running()
    assert (
        cycles.check_self_reset(
            state, WINDOW_LONG, limited=True, has_short_usage=False, days=DAYS, now=T0
        )
        is None
    )


def test_check_self_reset_long_without_cycle_before_allowance():
    state = dataclasses.replace(cycles.initial_state(1, 0), long_allowance=0)
    assert (
        cycles.check_self_reset(
            state, WINDOW_LONG, limited=True, has_short_usage=True, days=DAYS, now=T0
        )
        is SelfResetRefusal.NO_CYCLE
    )


def test_check_self_reset_long_expired_cycle_is_no_cycle():
    state = _running()
    assert (
        cycles.check_self_reset(
            state,
            WINDOW_LONG,
            limited=True,
            has_short_usage=True,
            days=DAYS,
            now=T0 + WEEK,
        )
        is SelfResetRefusal.NO_CYCLE
    )


def test_check_self_reset_no_allowance_last():
    state = _running(short_allowance=0, long_allowance=0)
    assert (
        cycles.check_self_reset(
            state, WINDOW_SHORT, limited=True, has_short_usage=True, days=DAYS, now=T0
        )
        is SelfResetRefusal.NO_ALLOWANCE
    )
    assert (
        cycles.check_self_reset(
            state, WINDOW_LONG, limited=True, has_short_usage=True, days=DAYS, now=T0
        )
        is SelfResetRefusal.NO_ALLOWANCE
    )


def test_check_self_reset_short_does_not_require_cycle():
    """短窗口自助重置与周期无关。"""
    state = dataclasses.replace(cycles.initial_state(1, 0))
    assert (
        cycles.check_self_reset(
            state, WINDOW_SHORT, limited=True, has_short_usage=True, days=DAYS, now=T0
        )
        is None
    )


def test_check_self_reset_rejects_unknown_window():
    with pytest.raises(ValueError):
        cycles.check_self_reset(
            _running(), "x", limited=True, has_short_usage=True, days=DAYS, now=T0
        )


def test_refusal_messages_cover_all_reasons_in_chinese():
    for reason in SelfResetRefusal:
        message = cycles.REFUSAL_MESSAGES[reason]
        assert message and not message.isascii()  # 中文提示
    assert "申请" in cycles.REFUSAL_MESSAGES[SelfResetRefusal.NO_ALLOWANCE]


def test_refusal_enum_is_str_backed():
    assert SelfResetRefusal.NO_CYCLE == "no_cycle"
    assert isinstance(SelfResetRefusal.NO_CYCLE, str)
