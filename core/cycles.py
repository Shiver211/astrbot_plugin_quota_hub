"""周期（长窗口）、重置标记与重置次数的纯状态机。

数据类骨架由集成方预先定义；状态转移函数见本模块下方（由实现者补充）。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum

from .models import VALID_WINDOWS, WINDOW_SHORT
from .windows import SECONDS_PER_DAY

# 重置发起者类型
ACTOR_SELF = "self"
ACTOR_ADMIN = "admin"
ACTOR_DASHBOARD = "dashboard"
# 触顶时由系统代替用户消耗一次重置次数（自动重置），前置条件与自助重置完全一致。
ACTOR_AUTO = "auto"


@dataclass(frozen=True)
class CycleState:
    """某实体的周期状态（内存与 entity_state 表一一对应）。

    - ``window_start``：长窗口开窗时刻；``None`` 表示当前没有进行中的周期。
    - ``short_marker``：短窗口重置标记，早于它的用量不计入短窗口；``0`` 表示无标记。
    - ``short_allowance`` / ``long_allowance``：本周期剩余的自助重置次数。
    - ``closed_by_reset``：上一周期由重置结束。**仅审计展示，不参与补满判定**：
      补满只发生在 ``expire_if_due`` 的自然到期分支，``open_cycle`` 从不补满，
      因此「重置关闭的周期再开窗不补满」是由开窗路径本身保证的。
    """

    window_start: float | None = None
    short_marker: float = 0.0
    short_allowance: int = 0
    long_allowance: int = 0
    closed_by_reset: bool = False


@dataclass(frozen=True)
class ResetLogEntry:
    """一次重置的审计记录。``consumed`` 表示是否消耗了自助重置次数。"""

    ts: float
    scope: str
    group_id: str
    user_id: str
    window: str
    actor: str
    actor_id: str = ""
    consumed: bool = False
    id: int = 0


def _check_window(window: str) -> str:
    """校验窗口名，非法时抛 ``ValueError``（调用方应先做用户输入归一化）。"""
    if window not in VALID_WINDOWS:
        raise ValueError(f"未知窗口：{window!r}")
    return window


def initial_state(self_reset_short: int, self_reset_long: int) -> CycleState:
    """首次出现的实体：没有周期，重置次数初始即为配置值。"""
    return CycleState(
        window_start=None,
        short_marker=0.0,
        short_allowance=self_reset_short,
        long_allowance=self_reset_long,
        closed_by_reset=False,
    )


def cycle_end(state: CycleState, days: float) -> float | None:
    """当前周期的到期时刻；无周期返回 ``None``。

    ``days`` 取实体**当前**解析出的长窗口天数，配置改动即时影响到期时刻。
    """
    if state.window_start is None:
        return None
    return state.window_start + days * SECONDS_PER_DAY


def is_active(state: CycleState, now: float, days: float) -> bool:
    """是否有进行中的周期（到期时刻当刻起即不再进行中）。"""
    end = cycle_end(state, days)
    return end is not None and now < end


def expire_if_due(
    state: CycleState,
    now: float,
    days: float,
    self_reset_short: int,
    self_reset_long: int,
) -> tuple[CycleState, bool]:
    """惰性推进：周期自然到期则关窗并把重置次数补满到配置值。

    返回 ``(新状态, 是否刚刚到期)``；未到期时原样返回传入对象（不构造新对象）。
    「补满 = 赋值为配置值」，因此本周期发放（grant）的额外次数在此自然清空。
    """
    end = cycle_end(state, days)
    if end is None or now < end:
        return state, False
    expired = replace(
        state,
        window_start=None,
        closed_by_reset=False,
        short_allowance=self_reset_short,
        long_allowance=self_reset_long,
    )
    return expired, True


def open_cycle(state: CycleState, now: float) -> CycleState:
    """开窗：由首次放行并计入的调用触发。不改动重置次数。

    由重置关闭的周期再开窗同样走这里，因此**不补满**、沿用剩余值；补满只在
    ``expire_if_due`` 的自然到期分支发生，与 ``closed_by_reset`` 标志无关。
    """
    return replace(state, window_start=now, closed_by_reset=False)


def apply_reset(state: CycleState, window: str, now: float) -> CycleState:
    """执行一次重置（不区分发起者，也不消耗次数）。

    - 短窗口：只写重置标记，之后只统计标记之后的用量；周期不受影响。
    - 长窗口：关闭当前周期并记 ``closed_by_reset``。该标志**仅供审计展示**，
      不参与补满判定——不补满是因为开窗路径 ``open_cycle`` 本就不补满。
    """
    _check_window(window)
    if window == WINDOW_SHORT:
        return replace(state, short_marker=now)
    return replace(state, window_start=None, closed_by_reset=True)


def consume_allowance(state: CycleState, window: str) -> CycleState:
    """扣减一次对应窗口的自助重置次数（调用方需先用 ``check_self_reset`` 校验）。"""
    _check_window(window)
    if window == WINDOW_SHORT:
        return replace(state, short_allowance=max(0, state.short_allowance - 1))
    return replace(state, long_allowance=max(0, state.long_allowance - 1))


def grant(state: CycleState, short: int, long: int) -> CycleState:
    """发放：给当前周期各自追加重置次数（允许 0，不允许负数）。"""
    if short < 0 or long < 0:
        raise ValueError("发放次数不能为负数")
    return replace(
        state,
        short_allowance=state.short_allowance + short,
        long_allowance=state.long_allowance + long,
    )


def allowance(state: CycleState, window: str) -> int:
    """读取对应窗口的剩余自助重置次数。"""
    _check_window(window)
    return state.short_allowance if window == WINDOW_SHORT else state.long_allowance


class SelfResetRefusal(str, Enum):
    """自助重置被拒的原因（不满足前置条件时不消耗次数）。"""

    NOT_LIMITED = "not_limited"
    NO_USAGE = "no_usage"
    NO_CYCLE = "no_cycle"
    NO_ALLOWANCE = "no_allowance"


REFUSAL_MESSAGES: dict[SelfResetRefusal, str] = {
    SelfResetRefusal.NOT_LIMITED: "当前没有生效的配额限制，无需重置",
    SelfResetRefusal.NO_USAGE: "当前短窗口内没有用量，无需重置",
    SelfResetRefusal.NO_CYCLE: "当前没有进行中的周期，无需重置",
    SelfResetRefusal.NO_ALLOWANCE: "本周期的自助重置次数已用完，可向管理员申请",
}


def check_self_reset(
    state: CycleState,
    window: str,
    *,
    limited: bool,
    has_short_usage: bool,
    days: float,
    now: float,
) -> SelfResetRefusal | None:
    """校验自助重置的前置条件，允许时返回 ``None``。

    判定顺序固定：是否受限流约束 → 短窗口是否有用量 / 长窗口是否有周期 → 是否还有次数。
    ``limited`` 由调用方给出（受监控 / ``enabled`` 且对应窗口至少一项配额不是 -1）。
    """
    _check_window(window)
    if not limited:
        return SelfResetRefusal.NOT_LIMITED
    if window == WINDOW_SHORT:
        if not has_short_usage:
            return SelfResetRefusal.NO_USAGE
    elif not is_active(state, now, days):
        return SelfResetRefusal.NO_CYCLE
    if allowance(state, window) <= 0:
        return SelfResetRefusal.NO_ALLOWANCE
    return None
